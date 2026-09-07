"""Safety-boundary tests; all GUI subprocesses are mocked."""

import contextlib
import io
import json
from pathlib import Path
import plistlib
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zlib

import wechat_send as sender


def tiny_png():
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff\xff"))
            + chunk(b"IEND", b""))


class WeChatSendTests(unittest.TestCase):
    def test_png_validation_rejects_corruption_and_non_images(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "picture.png"
            for content in [b"", b"not png", tiny_png()[:-4], tiny_png()[:-5] + b"wrong"]:
                with self.subTest(content=content), patch.object(sender, "wxmac_command") as command:
                    path.write_bytes(content)
                    with self.assertRaises(sender.SendError) as caught:
                        sender.execute(images=[path])
                    self.assertEqual(caught.exception.code, 65)
                    command.assert_not_called()
            path.write_bytes(tiny_png())
            self.assertEqual(sender.validate_png(path), str(path.resolve()))

    def test_two_images_use_one_current_chat_send(self):
        process = MagicMock(returncode=0)
        process.communicate.return_value = ('{"ok":true}', "")
        with patch.object(sender.subprocess, "Popen", return_value=process) as popen:
            sender.start_send(["wxmac"], images=["/tmp/one.png", "/tmp/two.png"])
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["wxmac", "send-file", "/tmp/one.png", "/tmp/two.png"])

    def test_image_send_failure_is_ambiguous(self):
        process = MagicMock(returncode=1)
        process.communicate.return_value = ('{"ok":false}', "")
        with patch.object(sender.subprocess, "Popen", return_value=process):
            with self.assertRaises(sender.SendError) as caught:
                sender.start_send(["wxmac"], images=["/tmp/one.png", "/tmp/two.png"])
        self.assertEqual(caught.exception.code, 70)

    def test_images_require_same_recipient_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "picture.png"
            path.write_bytes(tiny_png())
            with patch.object(sender, "wxmac_command", return_value=["wxmac"]), \
                 patch.object(sender, "ready"), \
                 patch.object(sender, "command_json", side_effect=[{"ok": True}, {"chat": "其他人"}]), \
                 patch.object(sender, "start_send") as send:
                with self.assertRaises(sender.SendError) as caught:
                    sender.execute(images=[path])
                self.assertEqual(caught.exception.code, 65)
                send.assert_not_called()

    def test_image_success_only_claims_action_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "picture.png"
            path.write_bytes(tiny_png())
            with patch.object(sender, "wxmac_command", return_value=["wxmac"]), \
                 patch.object(sender, "ready"), patch.object(sender, "unlocked"), \
                 patch.object(sender, "command_json"), patch.object(sender, "verify_chat") as verify, \
                 patch.object(sender, "start_send") as send:
                result = sender.execute(images=[path, path])
                verify.assert_called_once()
                send.assert_called_once_with(["wxmac"], images=[str(path.resolve()), str(path.resolve())])
                self.assertEqual(result["status"], "send_action_completed")
                self.assertEqual(result["image_count"], 2)
                self.assertFalse(result["delivery_confirmed"])

    def test_lock_plist_variants(self):
        for document, safe in [
            ({"IOConsoleLocked": False}, True),
            ([{"IOConsoleLocked": True}], False),
            ([{"IOConsoleUsers": [{"CGSSessionScreenIsLocked": False}]}], True),
            ({}, False),
        ]:
            with self.subTest(document=document):
                result = subprocess.CompletedProcess([], 0, plistlib.dumps(document))
                with patch.object(sender.subprocess, "run", return_value=result):
                    if safe:
                        sender.unlocked()
                    else:
                        with self.assertRaises(sender.SendError) as caught:
                            sender.unlocked()
                        self.assertEqual(caught.exception.code, 75)

    def test_recording_required_even_when_doctor_reports_ok(self):
        checks = [{"id": key, "ok": True} for key in
                  ["accessibility", "wechat_running", "wechat_window", "window_frame"]]
        with patch.object(sender, "unlocked"), patch.object(
            sender, "command_json", return_value={"ok": True, "checks": checks}
        ):
            with self.assertRaises(sender.SendError):
                sender.ready(["wxmac"])

    def test_recipient_mismatch_never_sends(self):
        with tempfile.TemporaryDirectory() as directory:
            message = Path(directory) / "message.txt"
            message.write_text("授权正文", encoding="utf-8")
            with patch.object(sender, "wxmac_command", return_value=["wxmac"]), \
                 patch.object(sender, "ready"), \
                 patch.object(sender, "command_json", side_effect=[{"ok": True}, {"chat": "其他人"}]), \
                 patch.object(sender, "start_send") as send:
                with self.assertRaises(sender.SendError) as caught:
                    sender.execute(message)
                self.assertEqual(caught.exception.code, 65)
                send.assert_not_called()

    def test_check_never_navigates(self):
        with patch.object(sender, "wxmac_command", return_value=["wxmac"]), \
             patch.object(sender, "ready"), patch.object(sender, "command_json") as command:
            self.assertEqual(sender.execute(None, True)["status"], "ready")
            command.assert_not_called()

    def test_failed_or_timed_out_started_send_is_not_retryable(self):
        for failure in [False, True]:
            with self.subTest(timeout=failure):
                process = MagicMock(returncode=1)
                process.communicate.side_effect = (
                    [subprocess.TimeoutExpired("wxmac", 60), ("", "")]
                    if failure else [(json.dumps({"ok": False}), ""), ("", "")]
                )
                with patch.object(sender.subprocess, "Popen", return_value=process):
                    with self.assertRaises(sender.SendError) as caught:
                        sender.start_send(["wxmac"], "正文")
                self.assertEqual(caught.exception.code, 70)

    def test_send_targets_current_chat_with_stdin(self):
        process = MagicMock(returncode=0)
        process.communicate.return_value = ('{"ok":true}', "")
        with patch.object(sender.subprocess, "Popen", return_value=process) as popen:
            sender.start_send(["wxmac"], "正文\n第二行")
        self.assertEqual(popen.call_args.args[0], ["wxmac", "send", "--stdin"])
        process.communicate.assert_called_once_with("正文\n第二行", timeout=60)

    def test_ocr_failure_after_success_is_not_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            message = Path(directory) / "message.txt"
            message.write_text("授权正文", encoding="utf-8")
            with patch.object(sender, "wxmac_command", return_value=["wxmac"]), \
                 patch.object(sender, "ready"), patch.object(sender, "unlocked"), \
                 patch.object(sender, "verify_chat"), patch.object(sender, "start_send"), \
                 patch.object(sender, "command_json", side_effect=[{"ok": True}, RuntimeError("OCR failed")]):
                result = sender.execute(message)
                self.assertTrue(result["ok"])
                self.assertFalse(result["full_text_visible"])
                self.assertFalse(result["delivery_confirmed"])

    def test_process_creation_failure_is_safe_to_retry(self):
        with patch.object(sender, "execute", side_effect=FileNotFoundError), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sender.main(["message.txt"]), 75)


if __name__ == "__main__":
    unittest.main()
