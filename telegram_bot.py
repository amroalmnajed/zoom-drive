import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests

import config
from models import Recording, ApprovalRequest, ApprovalDecision, BatchRequest
from utils import human_duration, human_size

log = logging.getLogger("zoom_to_drive")

class TelegramBot:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.offset = 0
        self._deferred: list[dict] = []   # undo taps that arrived mid-run
        self._queue: list[tuple] = []     # interpreted commands waiting to be returned
        self._batch_state: Optional[BatchRequest] = None  # Track batch selection state
        self._flush_old_updates()

    def _flush_old_updates(self) -> None:
        try:
            resp = requests.get(f"{self.base_url}/getUpdates", params={"offset": -1, "timeout": 1}, timeout=5)
            if resp.status_code == 200:
                results = resp.json().get("result", [])
                if results:
                    self.offset = results[-1]["update_id"] + 1
        except Exception:
            pass

    def set_webhook(self, webhook_url: str, secret_token: Optional[str] = None) -> bool:
        """Register webhook URL with Telegram.

        Args:
            webhook_url: Full webhook URL
            secret_token: Optional secret token for security

        Returns:
            True if successful, False otherwise
        """
        try:
            payload = {"url": webhook_url}
            if secret_token:
                payload["secret_token"] = secret_token
            resp = requests.post(f"{self.base_url}/setWebhook", json=payload, timeout=15)
            if resp.status_code == 200:
                result = resp.json()
                if result.get("ok"):
                    log.info("Webhook set successfully: %s", webhook_url)
                    return True
                else:
                    log.error("Failed to set webhook: %s", result.get("description"))
                    return False
            else:
                log.error("Webhook set request failed: %s", resp.text)
                return False
        except Exception as exc:
            log.error("Error setting webhook: %s", exc)
            return False

    def delete_webhook(self) -> bool:
        """Delete webhook and return to polling mode.

        Returns:
            True if successful, False otherwise
        """
        try:
            resp = requests.post(f"{self.base_url}/deleteWebhook", timeout=15)
            if resp.status_code == 200:
                result = resp.json()
                if result.get("ok"):
                    log.info("Webhook deleted successfully")
                    return True
                else:
                    log.error("Failed to delete webhook: %s", result.get("description"))
                    return False
            else:
                log.error("Webhook delete request failed: %s", resp.text)
                return False
        except Exception as exc:
            log.error("Error deleting webhook: %s", exc)
            return False

    def process_update(self, update: dict) -> Optional[tuple[str, object]]:
        """Process a Telegram update (from webhook or polling).

        Args:
            update: Telegram update object

        Returns:
            (command, arg) tuple or None
        """
        return self._interpret(update)

    def send_message(self, text: str, reply_markup: Optional[dict] = None) -> Optional[int]:
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        try:
            resp = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=15)
            if resp.status_code == 200:
                return resp.json()["result"]["message_id"]
        except Exception as exc:
            log.error("Telegram send error: %s", exc)
        return None

    def edit_message(self, message_id: int, text: str) -> None:
        payload = {"chat_id": self.chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        try:
            requests.post(f"{self.base_url}/editMessageText", json=payload, timeout=10)
        except Exception:
            pass

    def send_document(self, file_data: bytes, filename: str) -> Optional[int]:
        """Send a document file via Telegram.

        Args:
            file_data: Binary data of the file to send
            filename: Name of the file to send

        Returns:
            Message ID if successful, None otherwise
        """
        import io
        try:
            files = {"document": (filename, io.BytesIO(file_data))}
            payload = {"chat_id": self.chat_id}
            resp = requests.post(f"{self.base_url}/sendDocument", data=payload, files=files, timeout=60)
            if resp.status_code == 200:
                return resp.json()["result"]["message_id"]
        except Exception as exc:
            log.error("Telegram send document error: %s", exc)
        return None

    def get_document_file(self, file_id: str) -> Optional[bytes]:
        """Download a document file from Telegram.

        Args:
            file_id: Telegram file ID

        Returns:
            File data as bytes, or None if failed
        """
        try:
            # Get file path
            resp = requests.get(f"{self.base_url}/getFile", params={"file_id": file_id}, timeout=15)
            if resp.status_code != 200:
                log.error("Failed to get file info: %s", resp.text)
                return None

            file_path = resp.json().get("result", {}).get("file_path")
            if not file_path:
                log.error("No file path in response")
                return None

            # Download the file
            download_url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
            resp = requests.get(download_url, timeout=60)
            if resp.status_code == 200:
                return resp.content
            else:
                log.error("Failed to download file: %s", resp.text)
                return None
        except Exception as exc:
            log.error("Telegram get document error: %s", exc)
            return None

    def send_photo(self, photo_data: bytes, caption: str = "") -> Optional[int]:
        """Send a photo via Telegram.

        Args:
            photo_data: Binary data of the photo to send
            caption: Optional caption for the photo

        Returns:
            Message ID if successful, None otherwise
        """
        import io
        try:
            files = {"photo": (f"thumbnail.jpg", io.BytesIO(photo_data))}
            payload = {"chat_id": self.chat_id}
            if caption:
                payload["caption"] = caption
            resp = requests.post(f"{self.base_url}/sendPhoto", data=payload, files=files, timeout=60)
            if resp.status_code == 200:
                return resp.json()["result"]["message_id"]
        except Exception as exc:
            log.error("Telegram send photo error: %s", exc)
        return None

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            requests.post(f"{self.base_url}/answerCallbackQuery", json={"callback_query_id": callback_id, "text": text}, timeout=5)
        except Exception:
            pass

    def _stash_stray(self, u: dict) -> None:
        """An Undo tap that arrives while a run is waiting for something else."""
        cb = u.get("callback_query")
        if not cb or str(cb.get("from", {}).get("id")) != self.chat_id:
            return
        if str(cb.get("data", "")).startswith("undo"):
            self._deferred.append(u)
            self.answer_callback(cb["id"], "Got it - I'll handle this when the current run is done.")

    def get_batch_selection(self, recordings: list[Recording]) -> Optional[BatchRequest]:
        """Display recordings with checkboxes for batch selection.

        Args:
            recordings: List of pending recordings to display

        Returns:
            BatchRequest with selected recordings and action, or None if cancelled
        """
        from typing import Optional

        # Limit batch size to prevent message overflow
        max_batch = config.MAX_BATCH_SIZE if hasattr(config, 'MAX_BATCH_SIZE') else 10
        display_recordings = recordings[:max_batch]

        # Initialize batch state if not exists (must be before building keyboard)
        if self._batch_state is None:
            self._batch_state = BatchRequest(recordings=display_recordings, selected_keys=set())

        lines = [
            f"📋 <b>Batch Selection</b> ({len(display_recordings)} recordings)",
            "",
            "Select recordings to process, then choose an action:",
        ]

        # Build inline keyboard with checkboxes
        keyboard = []
        for i, rec in enumerate(display_recordings):
            key = rec.key
            checkbox = "☐" if key not in self._batch_state.selected_keys else "☑"
            topic_short = rec.topic[:30] + "..." if len(rec.topic) > 30 else rec.topic
            keyboard.append([{"text": f"{checkbox} {i+1}. {topic_short}", "callback_data": f"batch_toggle:{key}"}])

        # Add action buttons
        keyboard.append([
            {"text": "✅ Upload Selected", "callback_data": "batch_action:upload"},
            {"text": "⏭ Skip Selected", "callback_data": "batch_action:skip"},
        ])
        keyboard.append([
            {"text": "🛑 Cancel", "callback_data": "batch_action:cancel"},
        ])

        msg_id = self.send_message("\n".join(lines), reply_markup={"inline_keyboard": keyboard})
        print(f"[Telegram] Batch selection sent. Waiting for selection...")

        # Add timeout mechanism (5 minutes max)
        start_time = time.time()
        timeout_seconds = 300  # 5 minutes

        while True:
            # Check timeout
            if time.time() - start_time > timeout_seconds:
                log.warning("Batch selection timed out after %d seconds", timeout_seconds)
                self._batch_state = None
                if msg_id:
                    self.edit_message(msg_id, "⏰ <b>Batch selection timed out.</b> Please try again.")
                return None

            try:
                resp = requests.get(f"{self.base_url}/getUpdates", params={"offset": self.offset, "timeout": 30}, timeout=35)
                if resp.status_code != 200:
                    log.warning("Telegram API returned status %d, retrying...", resp.status_code)
                    time.sleep(2)
                    continue
                updates = resp.json().get("result", [])
                for u in updates:
                    self.offset = u["update_id"] + 1
                    if "callback_query" in u:
                        cb = u["callback_query"]
                        if str(cb.get("from", {}).get("id")) != self.chat_id:
                            continue
                        data = cb.get("data", "")
                        self.answer_callback(cb["id"], "Processing...")

                        if data.startswith("batch_toggle:"):
                            if self._batch_state is None:
                                log.warning("batch_toggle received but _batch_state is None")
                                self.answer_callback(cb["id"], "Error: Batch state lost. Please try /batch again.")
                                continue
                            key = data.split(":")[1]
                            if key in self._batch_state.selected_keys:
                                self._batch_state.selected_keys.remove(key)
                            else:
                                self._batch_state.selected_keys.add(key)
                            # Update the message with new checkboxes
                            self._update_batch_keyboard(msg_id, display_recordings)
                            continue

                        if data == "batch_action:upload":
                            if self._batch_state is None:
                                log.warning("batch_action:upload received but _batch_state is None")
                                self.answer_callback(cb["id"], "Error: Batch state lost. Please try /batch again.")
                                continue
                            if not self._batch_state.selected_keys:
                                self.answer_callback(cb["id"], "No recordings selected!")
                                continue
                            self._batch_state.action = "upload"
                            if msg_id: self.edit_message(msg_id, f"✅ <b>Batch upload selected:</b> {len(self._batch_state.selected_keys)} recordings")
                            return self._batch_state

                        if data == "batch_action:skip":
                            if self._batch_state is None:
                                log.warning("batch_action:skip received but _batch_state is None")
                                self.answer_callback(cb["id"], "Error: Batch state lost. Please try /batch again.")
                                continue
                            if not self._batch_state.selected_keys:
                                self.answer_callback(cb["id"], "No recordings selected!")
                                continue
                            self._batch_state.action = "skip"
                            if msg_id: self.edit_message(msg_id, f"⏭ <b>Batch skip selected:</b> {len(self._batch_state.selected_keys)} recordings")
                            return self._batch_state

                        if data == "batch_action:cancel":
                            self._batch_state = None
                            if msg_id: self.edit_message(msg_id, "🛑 <b>Batch selection cancelled.</b>")
                            return None

                        self._stash_stray(u)
            except requests.exceptions.Timeout:
                log.warning("Telegram polling timeout, retrying...")
                continue
            except requests.exceptions.RequestException as exc:
                log.error("Telegram polling error: %s", exc)
                time.sleep(2)
            except Exception as exc:
                log.error("Unexpected error in batch selection: %s", exc)
                time.sleep(2)

    def _update_batch_keyboard(self, msg_id: int, recordings: list[Recording]) -> None:
        """Update the batch selection keyboard with current checkbox states."""
        if self._batch_state is None:
            log.warning("_update_batch_keyboard called with None _batch_state")
            return

        keyboard = []
        for i, rec in enumerate(recordings):
            key = rec.key
            checkbox = "☑" if key in self._batch_state.selected_keys else "☐"
            topic_short = rec.topic[:30] + "..." if len(rec.topic) > 30 else rec.topic
            keyboard.append([{"text": f"{checkbox} {i+1}. {topic_short}", "callback_data": f"batch_toggle:{key}"}])

        keyboard.append([
            {"text": "✅ Upload Selected", "callback_data": "batch_action:upload"},
            {"text": "⏭ Skip Selected", "callback_data": "batch_action:skip"},
        ])
        keyboard.append([
            {"text": "🛑 Cancel", "callback_data": "batch_action:cancel"},
        ])

        try:
            payload = {"chat_id": self.chat_id, "message_id": msg_id, "reply_markup": json.dumps({"inline_keyboard": keyboard})}
            requests.post(f"{self.base_url}/editMessageReplyMarkup", json=payload, timeout=10)
        except Exception as exc:
            log.error("Failed to update batch keyboard: %s", exc)

    def get_user_decision(self, request: ApprovalRequest) -> ApprovalDecision:
        rec = request.recording
        folders = config.FOLDERS
        sug = folders[request.suggested_key]
        start = rec.local_start()
        start_str = f"{start:%Y-%m-%d %H:%M}" if start else "unknown"
        why = f'matched "{request.matched_keyword}"' if request.matched_keyword else "default folder"

        lines = [
            f"📹 <b>New Recording ({request.index}/{request.total})</b>",
            "",
            f"<b>Title:</b> {rec.topic}",
            f"<b>Host:</b> {rec.host_email}",
            f"<b>Started:</b> {start_str}",
            f"<b>Duration:</b> {human_duration(rec.duration_min)} | <b>Size:</b> {human_size(rec.file_size)}",
        ]

        # Add participants if available
        if rec.participants and len(rec.participants) > 0:
            participant_list = ", ".join(rec.participants[:5])
            if len(rec.participants) > 5:
                participant_list += f" (+{len(rec.participants) - 5} more)"
            lines.append(f"<b>Participants:</b> {participant_list}")

        # Add video metadata if available
        if rec.resolution or rec.bitrate:
            metadata_parts = []
            if rec.resolution:
                metadata_parts.append(f"Resolution: {rec.resolution}")
            if rec.bitrate:
                metadata_parts.append(f"Bitrate: {rec.bitrate}")
            lines.append(f"<b>Video:</b> {', '.join(metadata_parts)}")

        lines.append("")
        lines.append(f"<b>Suggested Folder:</b> [{request.suggested_key}] {sug['name']} ({why})")
        msg_text = "\n".join(lines)

        keyboard = [
            [{"text": f"✅ Accept [{sug['name']}]", "callback_data": f"ok:{request.suggested_key}"}]
        ]
        folder_buttons = []
        for k, f in folders.items():
            folder_buttons.append({"text": f"{k}) {f['code']}", "callback_data": f"f:{k}"})
        keyboard.append(folder_buttons)
        keyboard.append([
            {"text": "⏭ Skip", "callback_data": "skip"},
            {"text": "🛑 Quit", "callback_data": "quit"}
        ])

        msg_id = self.send_message(msg_text, reply_markup={"inline_keyboard": keyboard})
        print(f"\n[Telegram] Approval request sent for: {rec.topic}. Waiting for input on Telegram...")

        while True:
            try:
                resp = requests.get(f"{self.base_url}/getUpdates", params={"offset": self.offset, "timeout": 30}, timeout=35)
                if resp.status_code != 200:
                    time.sleep(2)
                    continue
                updates = resp.json().get("result", [])
                for u in updates:
                    self.offset = u["update_id"] + 1
                    if "callback_query" in u:
                        cb = u["callback_query"]
                        if str(cb.get("from", {}).get("id")) != self.chat_id:
                            continue
                        data = cb.get("data", "")
                        self.answer_callback(cb["id"], "Processing selection...")

                        if data == "skip":
                            if msg_id: self.edit_message(msg_id, f"⏭ <b>Skipped:</b> {rec.topic}")
                            return ApprovalDecision("skip")
                        if data == "quit":
                            if msg_id: self.edit_message(msg_id, "🛑 <b>Stopped execution.</b>")
                            return ApprovalDecision("quit")
                        if data.startswith("ok:") or data.startswith("f:"):
                            f_key = data.split(":")[1]
                            chosen = folders[f_key]['name']
                            if msg_id: self.edit_message(msg_id, f"✅ <b>Approved for:</b> {chosen} | {rec.topic}")
                            return ApprovalDecision("upload", f_key)
                        self._stash_stray(u)
            except Exception as exc:
                log.warning("Telegram polling retry: %s", exc)
                time.sleep(2)

    def get_session_number(self, rec: Recording, folder: dict, auto_s: int) -> int:
        code = folder.get("code", "Course")
        if rec.start_time:
            d_str = rec.start_time.astimezone(timezone.utc).strftime("%Y%m%d")
        else:
            d_str = datetime.now(timezone.utc).strftime("%Y%m%d")

        lines = [
            f"📁 <b>Target Folder:</b> {folder['name']} ({code})",
            f"<b>Auto-detected Session:</b> S{auto_s}",
            f"<b>Filename Preview:</b> <code>GMT{d_str}_{code}_S{auto_s}.mp4</code>",
            "",
            f"Click <b>Use S{auto_s}</b> or reply with a custom number (e.g. 26)."
        ]
        text = "\n".join(lines)

        keyboard = [[{"text": f"✅ Use S{auto_s}", "callback_data": f"s:{auto_s}"}]]
        msg_id = self.send_message(text, reply_markup={"inline_keyboard": keyboard})
        print(f"[Telegram] Session prompt sent (Auto: S{auto_s}). Waiting for input on Telegram...")

        while True:
            try:
                resp = requests.get(f"{self.base_url}/getUpdates", params={"offset": self.offset, "timeout": 30}, timeout=35)
                if resp.status_code != 200:
                    time.sleep(2)
                    continue
                updates = resp.json().get("result", [])
                for u in updates:
                    self.offset = u["update_id"] + 1
                    if "callback_query" in u:
                        cb = u["callback_query"]
                        if str(cb.get("from", {}).get("id")) != self.chat_id:
                            continue
                        data = cb.get("data", "")
                        if data.startswith("s:"):
                            s_num = int(data.split(":")[1])
                            self.answer_callback(cb["id"], f"Session S{s_num} confirmed!")
                            if msg_id: self.edit_message(msg_id, f"🎯 <b>Confirmed Session:</b> S{s_num}")
                            return s_num
                        self._stash_stray(u)
                    elif "message" in u:
                        msg = u["message"]
                        if str(msg.get("from", {}).get("id")) != self.chat_id:
                            continue
                        txt = msg.get("text", "").strip().lower()
                        if txt.startswith("s"):
                            txt = txt[1:]
                        if txt.isdigit():
                            s_num = int(txt)
                            if s_num < 1 or s_num > 999:
                                self.send_message(f"❌ Invalid session number: {s_num}. Please enter a number between 1 and 999.")
                                continue
                            if msg_id: self.edit_message(msg_id, f"🎯 <b>Custom Session:</b> S{s_num}")
                            return s_num
            except Exception as exc:
                log.warning("Telegram polling retry: %s", exc)
                time.sleep(2)

    def _interpret(self, u: dict) -> Optional[tuple[str, object]]:
        """Turn one Telegram update into (command, arg), or None to ignore it."""
        cb = u.get("callback_query")
        if cb:
            if str(cb.get("from", {}).get("id")) != self.chat_id:
                return None
            data = str(cb.get("data", ""))
            for prefix, cmd in (("undoyes:", "undo_yes"), ("undono:", "undo_no"), ("undo:", "undo_ask")):
                if data.startswith(prefix):
                    self.answer_callback(cb["id"])
                    return cmd, data[len(prefix):]
            # Handle batch-related callbacks in main.py, not here
            if data.startswith("batch_"):
                return None  # Let main.py handle these
            return None

        msg = u.get("message") or u.get("edited_message")
        if not msg or str(msg.get("chat", {}).get("id")) != self.chat_id:
            return None

        # Check for document upload (restore command)
        if "document" in msg:
            document = msg["document"]
            file_id = document.get("file_id")
            file_name = document.get("file_name", "unknown.zip")
            self.send_message(f"📄 <b>Document received:</b> {file_name}\n\nAttempting to restore...")
            return "restore", (file_id, file_name)

        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return None
        parts = text.split()
        cmd = parts[0].lstrip("/").split("@")[0].lower()

        if cmd == "run":
            days = config.LOOKBACK_DAYS
            dry_run = False
            if len(parts) > 1:
                if parts[1].isdigit():
                    days = int(parts[1])
                elif parts[1] == "--dry":
                    dry_run = True
                    if len(parts) > 2 and parts[2].isdigit():
                        days = int(parts[2])

            if dry_run:
                self.send_message(f"🧪 <b>Starting dry-run</b> — looking back <b>{days} day(s)</b> (no uploads)")
                return "run_dry", days
            else:
                self.send_message(f"▶️ <b>Starting run</b> — looking back <b>{days} day(s)</b>...")
                return "run", days
        if cmd in ("status", "reset", "help", "backup", "stats"):
            return cmd, 0
        if cmd == "undo":
            return "undo_list", ""
        if cmd == "batch":
            return "batch", ""
        self.send_message(
            f"❓ Unknown command <code>{cmd}</code>.\n"
            "Try /run, /run 30, /run --dry, /undo, /batch, /status, /stats, /reset, /backup, or /help."
        )
        return None

    def listen_for_command(self) -> tuple[str, object]:
        """
        Block until the authorised user sends a recognised command or taps an
        Undo button.  Returns (command, arg):

            ("run", days)            /run [N]
            ("run_dry", days)        /run --dry [N]
            ("status" | "reset" | "help", 0)
            ("undo_list", "")        /undo          -> list recent uploads
            ("undo_ask", id)         tapped "Undo this upload" -> ask to confirm
            ("undo_yes", id)         confirmed
            ("undo_no", id)          cancelled
            ("restore", (file_id, filename))  document upload for restore
        """
        self.send_message(
            "🤖 <b>Bot is idle and listening.</b>\n\n"
            "Send a command to get started:\n"
            "• /run — fetch last 7 days\n"
            "• /run 30 — fetch last 30 days\n"
            "• /run --dry — dry-run (no uploads)\n"
            "• /batch — select multiple recordings for batch upload/skip\n"
            "• /undo — undo a recent upload + docx link\n"
            "• /status — show processed recordings\n"
            "• /stats — detailed statistics\n"
            "• /reset — clear processed history\n"
            "• /backup — download state backup\n"
            "• /restore — upload backup file to restore state\n"
            "• /help — show this message"
        )
        log.debug("Bot idle — waiting for /run command on Telegram.")

        while True:
            while self._deferred:                      # taps that came in mid-run
                result = self._interpret(self._deferred.pop(0))
                if result:
                    self._queue.append(result)
            if self._queue:
                return self._queue.pop(0)
            try:
                resp = requests.get(
                    f"{self.base_url}/getUpdates",
                    params={"offset": self.offset, "timeout": 30},
                    timeout=35,
                )
                if resp.status_code != 200:
                    time.sleep(2)
                    continue
                for u in resp.json().get("result", []):
                    self.offset = u["update_id"] + 1
                    result = self._interpret(u)
                    if result:
                        self._queue.append(result)   # never drop the rest of a batch
            except Exception as exc:
                log.warning("Bot idle poll error: %s", exc)
                time.sleep(2)
