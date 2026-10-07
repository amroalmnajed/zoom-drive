import sys

main_code = '''#!/usr/bin/env python3
"""
main.py - Fetch Zoom cloud recordings, ask for confirmation via Telegram bot inline buttons,
confirm session number, then stream the video directly from Zoom into Google Drive.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Literal, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httplib2
httplib2.REDIRECT_CODES = {301, 302, 303, 307}

import requests
import urllib3
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaUpload

import config

log = logging.getLogger("zoom_to_drive")

def setup_logging() -> None:
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(fmt)
    file_handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    log.addHandler(console)
    log.addHandler(file_handler)

def backoff_sleep(attempt: int, retry_after: Optional[float] = None) -> None:
    if retry_after and retry_after > 0:
        delay = min(retry_after, 300)
    else:
        delay = min(config.BACKOFF_MAX_SECONDS, config.BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
    delay += random.uniform(0, 1)
    log.warning("Retrying in %.1fs (attempt %d)", delay, attempt)
    time.sleep(delay)

def human_size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return "unknown size"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"

def human_duration(minutes: Optional[float]) -> str:
    if minutes is None:
        return "unknown"
    total = int(round(minutes))
    hours, mins = divmod(total, 60)
    return f"{hours}h {mins:02d}m" if hours else f"{mins}m"

def parse_zoom_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

class ZoomError(RuntimeError):
    pass

@dataclass
class Recording:
    meeting_id: str
    meeting_uuid: str
    topic: str
    host_email: str
    start_time: Optional[datetime]
    duration_min: Optional[float]
    file_id: str
    file_size: Optional[int]
    download_url: str
    recording_type: str

    @property
    def key(self) -> str:
        return self.file_id or f"{self.meeting_uuid}:{self.recording_type}"

    def local_start(self) -> Optional[datetime]:
        if not self.start_time:
            return None
        return self.start_time.astimezone(ZoneInfo(config.LOCAL_TIMEZONE))

class ZoomClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    def _fetch_token(self) -> None:
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                resp = self._session.post(
                    config.ZOOM_TOKEN_URL,
                    params={"grant_type": "account_credentials", "account_id": config.ZOOM_ACCOUNT_ID},
                    auth=(config.ZOOM_CLIENT_ID, config.ZOOM_CLIENT_SECRET),
                    timeout=30,
                )
            except requests.RequestException as exc:
                if attempt >= config.MAX_RETRIES:
                    raise ZoomError(f"Network error while requesting Zoom token: {exc}") from exc
                backoff_sleep(attempt)
                continue

            if resp.status_code == 200:
                data = resp.json()
                self._token = data["access_token"]
                self._expires_at = time.time() + int(data.get("expires_in", 3600))
                return
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= config.MAX_RETRIES:
                    raise ZoomError(f"Zoom token endpoint failed (HTTP {resp.status_code}).")
                backoff_sleep(attempt)
                continue
            raise ZoomError(f"Zoom rejected credentials (HTTP {resp.status_code}): {resp.text[:300]}.")

    def _get_token(self) -> str:
        if not self._token or time.time() >= self._expires_at - 300:
            self._fetch_token()
        assert self._token is not None
        return self._token

    def _request(
        self, method: str, url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None, stream: bool = False
    ) -> requests.Response:
        attempt = 0
        refreshed_after_401 = False
        while True:
            attempt += 1
            hdrs = {"Authorization": f"Bearer {self._get_token()}"}
            hdrs.update(headers or {})
            try:
                resp = self._session.request(
                    method, url, params=params, headers=hdrs, stream=stream, timeout=(10, config.HTTP_TIMEOUT_SECONDS)
                )
            except requests.RequestException as exc:
                if attempt >= config.MAX_RETRIES:
                    raise ZoomError(f"Network error talking to Zoom: {exc}") from exc
                backoff_sleep(attempt)
                continue

            if resp.status_code < 400:
                return resp

            if resp.status_code == 401 and not refreshed_after_401:
                resp.close()
                self._token = None
                refreshed_after_401 = True
                attempt -= 1
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                resp.close()
                if attempt >= config.MAX_RETRIES:
                    raise ZoomError(f"Zoom API failed (HTTP {resp.status_code}) for {url}")
                backoff_sleep(attempt)
                continue

            body = resp.text[:500]
            resp.close()
            raise ZoomError(f"Zoom API error HTTP {resp.status_code} for {url}: {body}")

    def iter_recordings(self, days_back: int) -> Iterator[Recording]:
        today = datetime.now(timezone.utc).date()
        window_start = today - timedelta(days=days_back)
        seen: set[str] = set()
        while window_start <= today:
            window_end = min(window_start + timedelta(days=29), today)
            yield from self._iter_window(window_start.isoformat(), window_end.isoformat(), seen)
            window_start = window_end + timedelta(days=1)

    def _iter_window(self, date_from: str, date_to: str, seen: set[str]) -> Iterator[Recording]:
        url = f"{config.ZOOM_API_BASE}/users/{quote(config.ZOOM_USER_ID, safe='@')}/recordings"
        params: dict = {"from": date_from, "to": date_to, "page_size": 300}
        while True:
            data = self._request("GET", url, params=params).json()
            for meeting in data.get("meetings") or []:
                rec = self._meeting_to_recording(meeting)
                if rec and rec.key not in seen:
                    seen.add(rec.key)
                    yield rec
            token = data.get("next_page_token")
            if not token:
                return
            params["next_page_token"] = token

    @staticmethod
    def _meeting_to_recording(meeting: dict) -> Optional[Recording]:
        topic = (meeting.get("topic") or "").strip() or "Untitled Meeting"
        meeting_id = str(meeting.get("id") or "unknown")

        files = [
            f for f in (meeting.get("recording_files") or [])
            if str(f.get("file_type", "")).upper() == "MP4"
            and str(f.get("status", "completed")).lower() == "completed"
            and f.get("download_url")
        ]
        if not files:
            return None

        primary = None
        for preferred in config.PREFERRED_RECORDING_TYPES:
            primary = next((f for f in files if f.get("recording_type") == preferred), None)
            if primary:
                break
        if primary is None:
            primary = max(files, key=lambda f: f.get("file_size") or 0)

        duration: Optional[float] = None
        rec_start = parse_zoom_time(primary.get("recording_start"))
        rec_end = parse_zoom_time(primary.get("recording_end"))
        if rec_start and rec_end and rec_end > rec_start:
            duration = (rec_end - rec_start).total_seconds() / 60
        elif meeting.get("duration"):
            duration = float(meeting["duration"])

        return Recording(
            meeting_id=meeting_id,
            meeting_uuid=str(meeting.get("uuid") or ""),
            topic=topic,
            host_email=(meeting.get("host_email") or "unknown host"),
            start_time=parse_zoom_time(meeting.get("start_time")) or rec_start,
            duration_min=duration,
            file_id=str(primary.get("id") or ""),
            file_size=int(primary["file_size"]) if primary.get("file_size") else None,
            download_url=primary["download_url"],
            recording_type=str(primary.get("recording_type") or "video"),
        )

    def open_download(self, url: str, start: int = 0) -> requests.Response:
        token = self._get_token()
        sep = "&" if "?" in url else "?"
        download_url = f"{url}{sep}access_token={token}"
        headers = {"Range": f"bytes={start}-"} if start > 0 else {}
        try:
            resp = requests.get(
                download_url, headers=headers, stream=True, timeout=(10, config.HTTP_TIMEOUT_SECONDS), allow_redirects=True
            )
        except requests.RequestException as exc:
            raise ZoomError(f"Failed to start Zoom download: {exc}") from exc
        if resp.status_code >= 400:
            body = resp.text[:300]
            resp.close()
            raise ZoomError(f"Zoom download error HTTP {resp.status_code}: {body}")
        if start > 0 and resp.status_code != 206:
            resp.close()
            raise ZoomError("Zoom did not honour Range request.")
        return resp

    def get_content_length(self, url: str) -> int:
        resp = self.open_download(url)
        try:
            length = resp.headers.get("Content-Length")
        finally:
            resp.close()
        if not length:
            raise ZoomError("Cannot determine recording size.")
        return int(length)

def _build_rules() -> list[tuple[str, str, re.Pattern]]:
    rules: list[tuple[str, str, re.Pattern]] = []
    seen: set[tuple[str, str]] = set()
    for key, folder in config.FOLDERS.items():
        for kw in folder["keywords"]:
            kw_clean = kw.strip()
            if not kw_clean or (key, kw_clean.lower()) in seen:
                continue
            seen.add((key, kw_clean.lower()))
            left = r"(?<![A-Za-z0-9])" if kw_clean[0].isalnum() else ""
            right = r"(?![A-Za-z0-9])" if kw_clean[-1].isalnum() else ""
            pattern = re.compile(left + re.escape(kw_clean) + right, re.IGNORECASE)
            rules.append((key, kw_clean, pattern))
    rules.sort(key=lambda r: -len(r[1]))
    return rules

_RULES = _build_rules()

def categorize(title: str) -> tuple[str, Optional[str]]:
    for key, kw, pattern in _RULES:
        if pattern.search(title):
            return key, kw
    return config.DEFAULT_FOLDER_KEY, None

class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read %s (%s); starting empty.", path, exc)

    def should_hide(self, key: str) -> bool:
        entry = self._data.get(key)
        if not entry:
            return False
        if entry.get("status") == "uploaded":
            return True
        return entry.get("status") == "skipped" and config.REMEMBER_SKIPPED

    def mark(self, key: str, status: str, **info) -> None:
        self._data[key] = {"status": status, "at": datetime.now(timezone.utc).isoformat(), **info}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

def _load_google_credentials():
    scopes = config.GOOGLE_SCOPES
    if config.GOOGLE_AUTH_MODE == "service_account":
        return service_account.Credentials.from_service_account_file(
            str(config.GOOGLE_SERVICE_ACCOUNT_FILE), scopes=scopes
        )

    token_path = config.GOOGLE_OAUTH_TOKEN_FILE
    creds = None
    if token_path.exists():
        try:
            creds = UserCredentials.from_authorized_user_file(str(token_path), scopes)
        except ValueError:
            pass

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleAuthRequest())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except RefreshError:
            pass

    flow = InstalledAppFlow.from_client_secrets_file(str(config.GOOGLE_OAUTH_CLIENT_FILE), scopes)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds

def get_drive_service():
    creds = _load_google_credentials()
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=config.HTTP_TIMEOUT_SECONDS))
    return build("drive", "v3", http=http, cache_discovery=False)

def preflight_check_folders(drive) -> None:
    for key, folder in config.FOLDERS.items():
        try:
            meta = drive.files().get(
                fileId=folder["id"], fields="id,name,mimeType,trashed", supportsAllDrives=True
            ).execute(num_retries=3)
            if meta.get("mimeType") != "application/vnd.google-apps.folder" or meta.get("trashed"):
                print(f"  ! Folder {key} ({folder['name']}): ID is not a live folder.")
        except HttpError as exc:
            print(f"  ! Folder {key} ({folder['name']}): cannot access it (HTTP {exc.resp.status}).")

def drive_file_exists(drive, folder_id: str, name: str) -> Optional[str]:
    safe = name.replace("\\\\", "\\\\\\\\").replace("'", "\\\\'")
    query = f"name = '{safe}' and '{folder_id}' in parents and trashed = false"
    result = drive.files().list(
        q=query, fields="files(id,name)", pageSize=1, supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute(num_retries=3)
    files = result.get("files", [])
    return files[0]["id"] if files else None

def get_next_session_number(drive, folder_id: str) -> int:
    if not drive:
        return 1
    query = f"'{folder_id}' in parents and trashed = false"
    try:
        result = drive.files().list(
            q=query, fields="files(name)", pageSize=1000, supportsAllDrives=True, includeItemsFromAllDrives=True
        ).execute(num_retries=3)
        numbers = []
        for f in result.get("files", []):
            match = re.search(r"_S(\\d+)\\.mp4$", f.get("name", ""), re.IGNORECASE)
            if match:
                numbers.append(int(match.group(1)))
        return max(numbers, default=0) + 1
    except Exception:
        return 1

class ZoomStreamMedia(MediaUpload):
    def __init__(self, zoom: ZoomClient, url: str, size: int, chunksize: int, mimetype: str = "video/mp4"):
        self._zoom = zoom
        self._url = url
        self._size = size
        self._chunksize = chunksize
        self._mimetype = mimetype
        self._resp: Optional[requests.Response] = None
        self._pos = 0

    def chunksize(self) -> int:
        return self._chunksize

    def mimetype(self) -> str:
        return self._mimetype

    def size(self) -> int:
        return self._size

    def resumable(self) -> bool:
        return True

    def has_stream(self) -> bool:
        return False

    def getbytes(self, begin: int, length: int) -> bytes:
        length = min(length, self._size - begin)
        if length <= 0:
            return b""
        if self._resp is None or begin != self._pos:
            self._reopen(begin)

        buf = bytearray(length)
        view = memoryview(buf)
        got = 0
        try:
            while got < length:
                n = self._resp.raw.readinto(view[got:])
                if not n:
                    break
                got += n
        except (urllib3.exceptions.HTTPError, requests.RequestException, OSError) as exc:
            self.close()
            raise ConnectionError(f"Zoom download interrupted: {exc}") from exc

        if got < length:
            self.close()
            raise ConnectionError("Zoom stream ended early.")
        self._pos = begin + got
        return bytes(buf)

    def _reopen(self, start: int) -> None:
        self.close()
        self._resp = self._zoom.open_download(self._url, start=start)
        self._pos = start

    def close(self) -> None:
        if self._resp is not None:
            try:
                self._resp.close()
            finally:
                self._resp = None

def _is_retryable_http_error(exc: HttpError) -> bool:
    status = exc.resp.status
    if status in (408, 429) or status >= 500:
        return True
    if status == 403:
        content = exc.content or b""
        return b"rateLimitExceeded" in content or b"userRateLimitExceeded" in content
    return False

def build_drive_filename(rec: Recording, course_code: str, session_num: int) -> str:
    if rec.start_time:
        date_str = rec.start_time.astimezone(timezone.utc).strftime("%Y%m%d")
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"GMT{date_str}_{course_code}_S{session_num}.mp4"

def upload_recording(drive, zoom: ZoomClient, rec: Recording, folder: dict, session_num: int) -> Optional[dict]:
    course_code = folder.get("code", "Course")
    folder_id = folder["id"]
    name = build_drive_filename(rec, course_code, session_num)

    existing_id = drive_file_exists(drive, folder_id, name)
    if existing_id:
        print(f"  = Already in Drive folder as {name!r}; not uploading again.")
        return None

    size = rec.file_size or zoom.get_content_length(rec.download_url)
    if size <= 0:
        raise ZoomError("Recording reports 0 bytes.")

    media = ZoomStreamMedia(zoom, rec.download_url, size, config.UPLOAD_CHUNK_SIZE)
    body = {
        "name": name,
        "parents": [folder_id],
        "description": f"Zoom meeting {rec.meeting_id} | host {rec.host_email}",
    }
    request = drive.files().create(
        body=body, media_body=media, fields="id,name,size,webViewLink", supportsAllDrives=True
    )

    response = None
    failures = 0
    started = time.time()
    try:
        while response is None:
            try:
                status, response = request.next_chunk(num_retries=5)
                failures = 0
                if status:
                    done = status.resumable_progress
                    pct = done / size * 100
                    print(f"\\r  Uploading: {pct:5.1f}%  ({human_size(done)} / {human_size(size)})", end="", flush=True)
            except HttpError as exc:
                if not _is_retryable_http_error(exc):
                    raise
                failures += 1
                if failures > config.MAX_RETRIES:
                    raise
                print()
                log.warning("Drive HTTP %s; will resume.", exc.resp.status)
                backoff_sleep(failures)
            except (OSError, httplib2.HttpLib2Error) as exc:
                failures += 1
                if failures > config.MAX_RETRIES:
                    raise
                print()
                log.warning("Transfer interrupted (%s); resuming.", exc)
                backoff_sleep(failures)
    finally:
        media.close()
    print()

    uploaded_size = int(response.get("size", 0) or 0)
    if uploaded_size and uploaded_size != size:
        raise RuntimeError(f"Size mismatch: Zoom {size} vs Drive {uploaded_size}.")
    elapsed = max(time.time() - started, 0.001)
    print(f"  + Uploaded {human_size(size)} in {elapsed:.0f}s ({human_size(size / elapsed)}/s)")
    return response

# =========================================================================== #
# Telegram Bot Approval Layer
# =========================================================================== #
@dataclass
class ApprovalRequest:
    recording: Recording
    index: int
    total: int
    suggested_key: str
    matched_keyword: Optional[str]

@dataclass
class ApprovalDecision:
    action: Literal["upload", "skip", "quit"]
    folder_key: Optional[str] = None

class TelegramBot:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.offset = 0
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

    def get_user_decision(self, request: ApprovalRequest) -> ApprovalDecision:
        rec = request.recording
        folders = config.FOLDERS
        sug = folders[request.suggested_key]
        start = rec.local_start()
        start_str = f"{start:%Y-%m-%d %H:%M}" if start else "unknown"
        why = f'matched "{request.matched_keyword}"' if request.matched_keyword else "default folder"

        msg_text = (
            f"📹 <b>New Recording ({request.index}/{request.total})</b>\n\n"
            f"<b>Title:</b> {rec.topic}\n"
            f"<b>Host:</b> {rec.host_email}\n"
            f"<b>Started:</b> {start_str}\n"
            f"<b>Duration:</b> {human_duration(rec.duration_min)} | <b>Size:</b> {human_size(rec.file_size)}\n\n"
            f"<b>Suggested Folder:</b> [{request.suggested_key}] {sug['name']} ({why})"
        )

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
        print(f"\\n[Telegram] Approval request sent for: {rec.topic}. Waiting for your response on Telegram...")

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
                            if msg_id: self.edit_message(msg_id, f"✅ <b>Approved for:</b> {chosen}\\nTitle: {rec.topic}")
                            return ApprovalDecision("upload", f_key)
            except Exception as exc:
                log.warning("Telegram polling retry: %s", exc)
                time.sleep(2)

    def get_session_number(self, rec: Recording, folder: dict, auto_s: int) -> int:
        code = folder.get("code", "Course")
        if rec.start_time:
            d_str = rec.start_time.astimezone(timezone.utc).strftime("%Y%m%d")
        else:
            d_str = datetime.now(timezone.utc).strftime("%Y%m%d")

        text = (
            f"📁 <b>Target Folder:</b> {folder['name']} ({code})\n"
            f"<b>Auto-detected Session:</b> S{auto_s}\n"
            f"<b>Filename Preview:</b> <code>GMT{d_str}_{code}_S{auto_s}.mp4</code>\n\n"
            f"Click <b>Use S{auto_s}</b> or send a text message with a custom number (e.g. 26)."
        )

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

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Zoom cloud recordings -> Google Drive (via Telegram approval).")
    parser.add_argument("--days", type=int, default=config.LOOKBACK_DAYS, help="lookback window in days")
    parser.add_argument("--dry-run", action="store_true", help="fetch and prompt only; do not upload")
    return parser.parse_args(argv)

def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging()

    bot = TelegramBot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
    state = StateStore(config.STATE_FILE)
    zoom = ZoomClient()

    print(f"Fetching Zoom recordings from the last {args.days} day(s)...")
    try:
        recordings = sorted(
            zoom.iter_recordings(args.days), key=lambda r: r.start_time or datetime.min.replace(tzinfo=timezone.utc)
        )
    except ZoomError as exc:
        print(f"Zoom error: {exc}")
        return 1

    pending = [r for r in recordings if not state.should_hide(r.key)]
    print(f"Found {len(recordings)} recording(s); {len(pending)} not yet processed.")
    if not pending:
        return 0

    drive = None
    if not args.dry_run:
        try:
            drive = get_drive_service()
        except Exception as exc:
            print(f"Google Drive auth failed: {exc}")
            return 1
        print("Checking Drive folders...")
        preflight_check_folders(drive)

    uploaded = skipped = failed = 0
    try:
        for i, rec in enumerate(pending, start=1):
            suggested_key, matched = categorize(rec.topic)
            req = ApprovalRequest(rec, i, len(pending), suggested_key, matched)
            decision = bot.get_user_decision(req)

            if decision.action == "quit":
                print("Stopping at your request.")
                break

            if decision.action == "skip":
                skipped += 1
                if not args.dry_run:
                    state.mark(rec.key, "skipped", topic=rec.topic)
                print("  - Skipped.")
                continue

            folder = config.FOLDERS[decision.folder_key]
            auto_s = get_next_session_number(drive, folder["id"])
            session_num = bot.get_session_number(rec, folder, auto_s)
            code = folder.get("code", "Course")

            final_name = build_drive_filename(rec, code, session_num)
            print(f"  Target Filename: {final_name}")

            if args.dry_run:
                print(f"  [dry-run] Would upload {final_name} to {folder['name']}")
                uploaded += 1
                continue

            bot.send_message(f"🚀 <b>Starting Upload:</b> <code>{final_name}</code> to <b>{folder['name']}</b>...")
            print(f"  Uploading {final_name} to {folder['name']}...")
            try:
                result = upload_recording(drive, zoom, rec, folder, session_num)
                state.mark(rec.key, "uploaded", topic=rec.topic, folder=folder["name"], drive_file_id=(result or {}).get("id"))
                uploaded += 1
                bot.send_message(f"✅ <b>Uploaded successfully:</b> <code>{final_name}</code>")
            except Exception as exc:
                failed += 1
                print(f"\\n  ! Upload failed: {exc}")
                bot.send_message(f"❌ <b>Upload failed:</b> {exc}")
    except KeyboardInterrupt:
        print("\\nInterrupted.")

    print(f"Done. Uploaded: {uploaded} | Skipped: {skipped} | Failed: {failed}")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
'''

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(main_code)

print("Repaired main.py successfully!")
