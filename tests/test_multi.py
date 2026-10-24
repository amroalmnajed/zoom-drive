"""Unit tests for Google Docs export and /multi recording selection."""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from models import Recording, ZoomError
import docx_updater
import config
import multi_upload
import telegram_bot
from telegram_bot import TelegramBot


def _rec(file_id: str, topic: str, day: int) -> Recording:
    start = datetime(2026, 10, day, 9, 0, tzinfo=timezone.utc)
    return Recording(
        meeting_id="m",
        meeting_uuid="u",
        topic=topic,
        host_email="host@example.com",
        start_time=start,
        duration_min=45,
        file_id=file_id,
        file_size=1_000_000,
        download_url="https://zoom.example/dl",
        recording_type="shared_screen_with_speaker_view",
    )


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.edited: list[tuple] = []
        self._multi_state = None
        self.chat_id = "1"
        self.base_url = "http://example"

    def send_message(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return len(self.sent)

    def edit_message(self, message_id, text):
        self.edited.append((message_id, text))

    def get_session_number(self, rec, folder, auto_s):
        return auto_s


class FakeZoom:
    def __init__(self, recordings=None, error=None):
        self._recordings = recordings or []
        self._error = error
        self.last_days = None

    def iter_recordings(self, days_back, fetch_participants=False):
        self.last_days = days_back
        if self._error:
            raise self._error
        return list(self._recordings)


class TestGoogleDocsExport(unittest.TestCase):
    def test_export_omits_supports_all_drives(self):
        calls = {}

        class Drive:
            def files(self):
                return self

            def get(self, **kw):
                return types.SimpleNamespace(execute=lambda: {
                    "mimeType": "application/vnd.google-apps.document"
                })

            def export(self, **kw):
                calls["export"] = kw
                if "supportsAllDrives" in kw:
                    raise TypeError("Got an unexpected keyword argument supportsAllDrives")
                return types.SimpleNamespace(data=b"exported-docx")

        class Down:
            def __init__(self, fh, req):
                self.fh, self.req = fh, req

            def next_chunk(self):
                self.fh.write(self.req.data)
                return None, True

        with patch("googleapiclient.http.MediaIoBaseDownload", Down):
            buf = docx_updater._download(Drive(), "FILE1")
        self.assertEqual(buf.getvalue(), b"exported-docx")
        self.assertNotIn("supportsAllDrives", calls["export"])
        self.assertEqual(calls["export"]["mimeType"], docx_updater.DOCX_MIME)

    def test_binary_download_keeps_supports_all_drives(self):
        calls = {}

        class Drive:
            def files(self):
                return self

            def get(self, **kw):
                return types.SimpleNamespace(execute=lambda: {
                    "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                })

            def get_media(self, **kw):
                calls["get_media"] = kw
                return types.SimpleNamespace(data=b"binary-docx")

        class Down:
            def __init__(self, fh, req):
                self.fh, self.req = fh, req

            def next_chunk(self):
                self.fh.write(self.req.data)
                return None, True

        with patch("googleapiclient.http.MediaIoBaseDownload", Down):
            buf = docx_updater._download(Drive(), "FILE1")
        self.assertEqual(buf.getvalue(), b"binary-docx")
        self.assertTrue(calls["get_media"].get("supportsAllDrives"))


class TestMultiRecordingSelection(unittest.TestCase):
    def setUp(self):
        self.recs = [_rec("a", "Crash Class", 12), _rec("b", "Main Revision", 11)]
        self.zoom = FakeZoom(self.recs)
        self.bot = FakeBot()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config.STATE_FILE = Path(tmp.name) / "processed.json"
        config.UNDO_FILE = Path(tmp.name) / "undo.json"

    def test_handle_multi_lists_recordings_not_auto_pick(self):
        with patch.object(multi_upload, "ZoomClient", return_value=self.zoom):
            multi_upload._handle_multi(self.bot, dry_run=True, days=7)
        self.assertEqual(self.zoom.last_days, 7)
        state = self.bot._multi_state
        self.assertEqual(state["phase"], "recording")
        self.assertIsNone(state["selected_rec"])
        self.assertEqual(len(state["recordings"]), 2)
        text, markup = self.bot.sent[-1]
        self.assertIn("Select a recording", text)
        self.assertIn("Crash Class", text)
        self.assertIn("Main Revision", text)
        callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
        self.assertIn("multi_rec:0", callbacks)
        self.assertIn("multi_rec:1", callbacks)
        self.assertNotIn("multi_action:upload", callbacks)

    def test_older_recording_can_be_selected(self):
        with patch.object(multi_upload, "ZoomClient", return_value=self.zoom):
            multi_upload._handle_multi(self.bot, dry_run=True, days=7)
        multi_upload._handle_multi_rec_select(self.bot, "1")
        state = self.bot._multi_state
        self.assertEqual(state["phase"], "folder")
        self.assertEqual(state["selected_rec"].topic, "Main Revision")
        self.assertEqual(state["selected_rec"].file_id, "b")
        folder_msg, markup = self.bot.sent[-1]
        self.assertIn("Select folders", folder_msg)
        callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
        self.assertIn("multi_action:upload", callbacks)

    def test_toggle_ignored_during_recording_phase(self):
        with patch.object(multi_upload, "ZoomClient", return_value=self.zoom):
            multi_upload._handle_multi(self.bot, dry_run=True)
        multi_upload._handle_multi_toggle(self.bot, "1")
        self.assertEqual(self.bot._multi_state["phase"], "recording")
        self.assertEqual(self.bot._multi_state["selected_folders"], set())
        self.assertIn("select a recording first", self.bot.sent[-1][0].lower())

    def test_upload_uses_selected_recording(self):
        with patch.object(multi_upload, "ZoomClient", return_value=self.zoom):
            multi_upload._handle_multi(self.bot, dry_run=True, days=7)
        multi_upload._handle_multi_rec_select(self.bot, "1")
        first_key = next(iter(config.FOLDERS))
        self.bot._multi_state["selected_folders"].add(first_key)
        with patch.object(multi_upload, "ZoomClient", return_value=self.zoom), \
             patch.object(multi_upload, "get_next_session_number", return_value=9), \
             patch.object(multi_upload, "upload_recording") as upload:
            multi_upload._handle_multi_upload(self.bot)
        self.assertIsNone(self.bot._multi_state)
        dry_texts = [t for t, _ in self.bot.sent if "[dry-run]" in t]
        self.assertTrue(dry_texts)
        upload.assert_not_called()

    def test_upload_before_recording_selection_is_blocked(self):
        with patch.object(multi_upload, "ZoomClient", return_value=self.zoom):
            multi_upload._handle_multi(self.bot, dry_run=True)
        multi_upload._handle_multi_upload(self.bot)
        self.assertEqual(self.bot._multi_state["phase"], "recording")
        self.assertIn("select a recording first", self.bot.sent[-1][0].lower())

    def test_zoom_error_is_reported(self):
        zoom = FakeZoom(error=ZoomError("boom"))
        with patch.object(multi_upload, "ZoomClient", return_value=zoom):
            multi_upload._handle_multi(self.bot, days=7)
        self.assertIsNone(self.bot._multi_state)
        self.assertIn("Zoom error", self.bot.sent[-1][0])

    def test_invalid_rec_index(self):
        with patch.object(multi_upload, "ZoomClient", return_value=self.zoom):
            multi_upload._handle_multi(self.bot, dry_run=True)
        multi_upload._handle_multi_rec_select(self.bot, "99")
        self.assertEqual(self.bot._multi_state["phase"], "recording")
        self.assertIn("Invalid recording", self.bot.sent[-1][0])


class TestMultiCallbackRouting(unittest.TestCase):
    def test_interpret_multi_rec_and_days(self):
        bot = TelegramBot.__new__(TelegramBot)
        bot.chat_id = "42"
        bot.answer_callback = MagicMock()
        cb = {
            "callback_query": {
                "id": "cb1",
                "from": {"id": "42"},
                "data": "multi_rec:3",
            }
        }
        self.assertEqual(bot._interpret(cb), ("multi_rec_select", "3"))
        msg = {
            "message": {
                "chat": {"id": "42"},
                "text": "/multi 14",
            }
        }
        self.assertEqual(bot._interpret(msg), ("multi", 14))
        default = {
            "message": {
                "chat": {"id": "42"},
                "text": "/multi",
            }
        }
        cmd, days = bot._interpret(default)
        self.assertEqual(cmd, "multi")
        self.assertEqual(days, telegram_bot.config.LOOKBACK_DAYS)


if __name__ == "__main__":
    unittest.main()
