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


def doctor_report(*missing):
    return {"ok": not missing,
            "checks": [{"id": key, "ok": key not in missing} for key in sorted(sender.REQUIRED_CHECKS)]}


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

    def test_closed_window_is_reopened_once_before_giving_up(self):
        for first, second, healed in [
            (doctor_report("wechat_window", "window_frame"), doctor_report(), True),
            (doctor_report("wechat_window"), doctor_report("wechat_window"), False),
        ]:
            with self.subTest(healed=healed), \
                 patch.object(sender, "REOPEN_SETTLE_SECONDS", 0), \
                 patch.object(sender, "unlocked"), \
                 patch.object(sender.subprocess, "run") as osascript, \
                 patch.object(sender, "command_json", side_effect=[first, second]) as doctor:
                if healed:
                    sender.ready(["wxmac"])
                else:
                    with self.assertRaises(sender.SendError) as caught:
                        sender.ready(["wxmac"])
                    self.assertEqual(caught.exception.code, 75)
                    self.assertIn("wechat_window", str(caught.exception))
                osascript.assert_called_once()
                self.assertEqual(osascript.call_args.args[0][0], "/usr/bin/osascript")
                self.assertEqual(doctor.call_count, 2)

    def test_stopped_wechat_or_missing_permission_is_never_reopened(self):
        for report in [doctor_report("wechat_running", "wechat_window", "window_frame"),
                       doctor_report("accessibility", "wechat_window"),
                       doctor_report("screen_recording")]:
            with self.subTest(report=report), \
                 patch.object(sender, "unlocked"), \
                 patch.object(sender.subprocess, "run") as osascript, \
                 patch.object(sender, "command_json", return_value=report) as doctor:
                with self.assertRaises(sender.SendError) as caught:
                    sender.ready(["wxmac"])
                self.assertEqual(caught.exception.code, 75)
                osascript.assert_not_called()
                self.assertEqual(doctor.call_count, 1)

    def test_reopen_failure_only_falls_through_to_the_second_check(self):
        with patch.object(sender, "REOPEN_SETTLE_SECONDS", 0), patch.object(sender, "unlocked"), \
             patch.object(sender.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("osascript", 15)) as osascript, \
             patch.object(sender, "command_json",
                          side_effect=[doctor_report("wechat_window"), doctor_report()]):
            sender.ready(["wxmac"])
            osascript.assert_called_once()

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
