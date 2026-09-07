#!/usr/bin/env python3
"""Send authorized text or PNG images through the local Mac WeChat UI.

Exit 75 means no send process was started and retry is safe. Exit 70 means
the send process started but its outcome is uncertain: never retry blindly.
Exit 65 means an invalid input or a chat mismatch; correct it before retrying.
Exit 0 means wxmac completed its send action, not recipient delivery.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib


class SendError(Exception):
    def __init__(self, message, code=75):
        super().__init__(message)
        self.code = code


def wxmac_command():
    executable = shutil.which("wxmac")
    if not executable:
        candidate = Path.home() / ".local/bin/wxmac"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            executable = str(candidate)
    if not executable:
        raise SendError("wxmac is unavailable")
    return [executable]


def unlocked():
    """Fail closed if the macOS lock state cannot be established."""
    result = subprocess.run(
        ["/usr/sbin/ioreg", "-n", "Root", "-d1", "-a"],
        capture_output=True, timeout=10,
    )
    if result.returncode:
        raise SendError("cannot read screen lock state")
    try:
        root = plistlib.loads(result.stdout)
    except Exception:
        raise SendError("cannot parse screen lock state") from None
    roots = root if isinstance(root, list) else [root]
    states = []
    for item in roots:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("IOConsoleLocked"), bool):
            states.append(item["IOConsoleLocked"])
        else:
            for session in item.get("IOConsoleUsers", []):
                if isinstance(session, dict) and isinstance(session.get("CGSSessionScreenIsLocked"), bool):
                    states.append(session["CGSSessionScreenIsLocked"])
    if not states or any(states):
        raise SendError("screen is locked or its lock state is unknown")


def command_json(command, *arguments, allow_failure=False):
    result = subprocess.run(
        command + list(arguments), capture_output=True, text=True, timeout=45,
    )
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise SendError("wxmac returned invalid JSON") from None
    if not isinstance(data, dict) or (not allow_failure and (result.returncode or data.get("ok") is not True)):
        raise SendError("wxmac command failed before sending")
    return data


def ready(command):
    unlocked()
    data = command_json(command, "doctor", allow_failure=True)
    checks = data.get("checks", [])
    if not isinstance(checks, list):
        raise SendError("wxmac doctor returned invalid checks")
    passed = {check.get("id") for check in checks
              if isinstance(check, dict) and check.get("ok") is True}
    # Recording is optional in wxmac, but mandatory here to verify the recipient.
    missing = {"accessibility", "screen_recording", "wechat_running", "wechat_window", "window_frame"} - passed
    if missing:
        raise SendError("WeChat readiness checks failed: " + ", ".join(sorted(missing)))
    if data.get("ok") is not True:
        raise SendError("wxmac doctor reported an additional readiness failure")


def verify_chat(command, expected):
    data = command_json(command, "current-chat")
    if data.get("chat") != expected:
        raise SendError("current chat does not exactly match STORY_WECHAT_CHAT; no message sent", 65)


def start_send(command, message=None, images=None):
    # Popen failing means no process started. After Popen succeeds, all failures
    # are ambiguous even if wxmac claims it failed before pressing Enter.
    process = subprocess.Popen(
        command + (["send-file", *images] if images else ["send", "--stdin"]), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        stdout, _ = process.communicate(message, timeout=60)
        data = json.loads(stdout)
        if process.returncode or not isinstance(data, dict) or data.get("ok") is not True:
            raise ValueError("unsuccessful send")
    except BaseException:
        try:
            process.kill()
            process.communicate(timeout=5)
        finally:
            raise SendError("send process started; outcome uncertain; inspect WeChat before any retry", 70) from None


def validate_png(image_path):
    """Check complete PNG chunks, CRCs and compressed image data before UI use."""
    path = Path(image_path).expanduser().resolve()
    if not path.is_file() or not 0 < path.stat().st_size <= 50_000_000:
        raise SendError("image must be a nonempty PNG file under 50 MB", 65)
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SendError("image is not PNG", 65)
    offset, header, compressed, ended = 8, None, bytearray(), False
    try:
        while offset < len(data):
            length = struct.unpack_from(">I", data, offset)[0]
            kind = data[offset + 4:offset + 8]
            payload = data[offset + 8:offset + 8 + length]
            crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
            if zlib.crc32(kind + payload) != crc:
                raise ValueError("invalid CRC")
            if header is None:
                if kind != b"IHDR" or length != 13:
                    raise ValueError("missing PNG header")
                header = struct.unpack(">IIBBBBB", payload)
            elif kind == b"IHDR":
                raise ValueError("duplicate PNG header")
            if kind == b"IDAT":
                compressed.extend(payload)
            offset += length + 12
            if kind == b"IEND":
                ended = length == 0 and offset == len(data)
                break
        if not header or not ended or not compressed:
            raise ValueError("incomplete PNG")
        width, height, depth, color, compression, filtering, interlace = header
        allowed = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
        if (not width or not height or depth not in allowed.get(color, ())
                or compression or filtering or interlace not in (0, 1)):
            raise ValueError("invalid PNG header")
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(compressed, 100_000_001)
        if not pixels or len(pixels) > 100_000_000 or not decoder.eof or decoder.unused_data:
            raise ValueError("invalid or oversized PNG data")
        if interlace == 0:
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
            row_size = (width * channels * depth + 7) // 8 + 1
            if len(pixels) != row_size * height or any(pixels[i] > 4 for i in range(0, len(pixels), row_size)):
                raise ValueError("invalid PNG scanlines")
    except (ValueError, struct.error, zlib.error, OverflowError):
        raise SendError("PNG image is corrupt or unsupported", 65) from None
    return str(path)


def visible_message(command, expected, message):
    """A full own-message OCR match is evidence of visibility, not delivery."""
    compact = lambda value: "".join(value.split())
    try:
        unlocked()
        data = command_json(command, "messages", "--limit", "20")
        return data.get("chat") == expected and any(
            item.get("type") == "self"
            and compact(item.get("content", "")) == compact(message)
            for item in data.get("messages", []) if isinstance(item, dict)
        )
    except Exception:
        return False


def execute(message_path=None, check_only=False, images=None):
    # Validate every attachment before any navigation or send is attempted.
    image_paths = [validate_png(path) for path in images] if images else None
    command = wxmac_command()
    # This lock serializes this helper's invocations across projects. Other GUI
    # automation and direct wxmac callers must not run concurrently.
    lock_path = Path(tempfile.gettempdir()) / f"story-wechat-{os.getuid()}.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SendError("another WeChat send/check is in progress") from None
        ready(command)
        if check_only:
            return {"ok": True, "status": "ready", "sent": False}
        message = None
        if not image_paths:
            message = Path(message_path).read_text(encoding="utf-8").rstrip("\n")
            if not message.strip():
                raise SendError("message file is empty", 65)
        expected = os.environ.get("STORY_WECHAT_CHAT", "王士沛Ronald")
        if not expected.strip():
            raise SendError("STORY_WECHAT_CHAT is empty", 65)
        command_json(command, "chat-with", expected)
        verify_chat(command, expected)
        unlocked()
        if image_paths:
            start_send(command, images=image_paths)
            return {
                "ok": True, "status": "send_action_completed", "chat": expected,
                "image_count": len(image_paths), "delivery_confirmed": False,
            }
        start_send(command, message)
        # Do not convert a post-send verification failure into retryable failure.
        visible = visible_message(command, expected, message)
        return {
            "ok": True, "status": "send_action_completed", "chat": expected,
            "full_text_visible": visible, "delivery_confirmed": False,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message_file", nargs="?")
    parser.add_argument("--check", action="store_true", help="check readiness without changing chats or sending")
    parser.add_argument("--images", nargs="+", metavar="PNG", help="send all PNGs in one send-file action")
    args = parser.parse_args(argv)
    if sum(bool(value) for value in (args.check, args.message_file, args.images)) != 1:
        parser.error("choose exactly one: MESSAGE_FILE, --images PNG..., or --check")
    try:
        result = execute(args.message_file, args.check, args.images)
    except SendError as error:
        result = {"ok": False, "error": str(error), "retry_safe": error.code == 75}
        print(json.dumps(result, ensure_ascii=False))
        return error.code
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        # All subprocess errors after send starts are handled by start_send.
        print(json.dumps({"ok": False, "error": type(error).__name__, "retry_safe": True}))
        return 75
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
