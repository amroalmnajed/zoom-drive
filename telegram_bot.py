import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests

import config
from models import Recording, ApprovalRequest, ApprovalDecision
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
            "",
            f"<b>Suggested Folder:</b> [{request.suggested_key}] {sug['name']} ({why})"
        ]
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
            return None

        msg = u.get("message") or u.get("edited_message")
        if not msg or str(msg.get("chat", {}).get("id")) != self.chat_id:
            return None
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return None
        parts = text.split()
        cmd = parts[0].lstrip("/").split("@")[0].lower()

        if cmd == "run":
            days = config.LOOKBACK_DAYS
            if len(parts) > 1 and parts[1].isdigit():
                days = int(parts[1])
            self.send_message(f"▶️ <b>Starting run</b> — looking back <b>{days} day(s)</b>...")
            return "run", days
        if cmd in ("status", "reset", "help"):
            return cmd, 0
        if cmd == "undo":
            return "undo_list", ""
        self.send_message(
            f"❓ Unknown command <code>{cmd}</code>.\n"
            "Try /run, /run 30, /undo, /status, /reset, or /help."
        )
        return None

    def listen_for_command(self) -> tuple[str, object]:
        """
        Block until the authorised user sends a recognised command or taps an
        Undo button.  Returns (command, arg):

            ("run", days)            /run [N]
            ("status" | "reset" | "help", 0)
            ("undo_list", "")        /undo          -> list recent uploads
            ("undo_ask", id)         tapped "Undo this upload" -> ask to confirm
            ("undo_yes", id)         confirmed
            ("undo_no", id)          cancelled
        """
        self.send_message(
            "🤖 <b>Bot is idle and listening.</b>\n\n"
            "Send a command to get started:\n"
            "• /run — fetch last 7 days\n"
            "• /run 30 — fetch last 30 days\n"
            "• /undo — undo a recent upload + docx link\n"
            "• /status — show processed recordings\n"
            "• /reset — clear processed history\n"
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
