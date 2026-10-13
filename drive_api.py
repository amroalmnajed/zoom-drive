import json
import logging
import re
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING
import requests
import urllib3
import httplib2
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaUpload

import config
from models import ZoomError, Recording
from utils import backoff_sleep, build_drive_filename, human_size
from zoom_api import ZoomClient

if TYPE_CHECKING:
    from telegram_bot import TelegramBot

log = logging.getLogger("zoom_to_drive")

def _load_google_credentials(folder: Optional[dict] = None):
    scopes = config.GOOGLE_SCOPES
    if config.GOOGLE_AUTH_MODE == "service_account":
        return service_account.Credentials.from_service_account_file(
            str(config.GOOGLE_SERVICE_ACCOUNT_FILE), scopes=scopes
        )

    # Use folder-specific token if provided, else fallback to default
    if folder and "token_file" in folder:
        token_path = folder["token_file"]
    else:
        token_path = config.GOOGLE_OAUTH_TOKEN_FILE

    creds = None
    if token_path.exists():
        try:
            creds = UserCredentials.from_authorized_user_file(str(token_path), scopes)
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning("Token file %s is invalid (%s) — deleting and attempting to re-create.", token_path, exc)
            try:
                token_path.unlink(missing_ok=True)
            except OSError:
                pass
            
            env_key = folder["token_env"] if folder and "token_env" in folder else "GOOGLE_TOKEN_JSON"
            config._write_env_json(env_key, token_path)
            if token_path.exists():
                try:
                    creds = UserCredentials.from_authorized_user_file(str(token_path), scopes)
                except (ValueError, json.JSONDecodeError):
                    creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleAuthRequest())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except RefreshError as exc:
            log.warning("Token refresh failed: %s", exc)

    # Check if we're in a server environment (no display)
    import os
    import sys
    # Only skip browser auth if explicitly in Railway/Vercel
    if os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('VERCEL'):
        # Server environment - don't try interactive auth
        raise RuntimeError(
            f"Google Drive token not found or invalid for {token_path.name}. "
            "Please ensure the token is provided via environment variable "
            f"(e.g., {folder.get('token_env') if folder and 'token_env' in folder else 'GOOGLE_TOKEN_JSON'}) "
            "or run 'python auth_all.py' locally to generate the token file."
        )

    # Local development - use interactive browser auth
    flow = InstalledAppFlow.from_client_secrets_file(str(config.GOOGLE_OAUTH_CLIENT_FILE), scopes)
    creds = flow.run_local_server(port=8080)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds

def get_drive_service(folder: Optional[dict] = None):
    creds = _load_google_credentials(folder)
    http = creds.authorize(httplib2.Http())
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

def create_folder_if_missing(drive, parent_id: str, folder_name: str) -> str:
    """Create a folder in Drive if it doesn't exist.
    
    Args:
        drive: Drive service instance
        parent_id: Parent folder ID
        folder_name: Name of the folder to create
    
    Returns:
        Folder ID (existing or newly created)
    """
    # Check if folder already exists
    existing = drive_file_exists(drive, parent_id, folder_name)
    if existing:
        log.info("Folder %s already exists in parent %s", folder_name, parent_id)
        return existing
    
    # Create the folder
    folder_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    
    try:
        result = drive.files().create(
            body=folder_metadata,
            fields="id",
            supportsAllDrives=True
        ).execute(num_retries=3)
        log.info("Created folder %s with ID %s", folder_name, result.get("id"))
        return result.get("id")
    except HttpError as exc:
        log.error("Failed to create folder %s: %s", folder_name, exc)
        raise

def drive_file_exists(drive, folder_id: str, name: str) -> Optional[str]:
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    query = f"name = '{safe}' and '{folder_id}' in parents and trashed = false"
    result = drive.files().list(
        q=query, fields="files(id,name)", pageSize=1, supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute(num_retries=3)
    files = result.get("files", [])
    return files[0]["id"] if files else None

def find_file(drive, folder_id: str, name: str) -> Optional[dict]:
    """Like drive_file_exists but returns id, name and webViewLink."""
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    query = f"name = '{safe}' and '{folder_id}' in parents and trashed = false"
    result = drive.files().list(
        q=query, fields="files(id,name,webViewLink)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute(num_retries=3)
    files = result.get("files", [])
    return files[0] if files else None

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
            match = re.search(r"_S(\d+)\.mp4$", f.get("name", ""), re.IGNORECASE)
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

def _initiate_resumable_upload(creds, folder_id: str, name: str, size: int, description: str, token_path: Path) -> str:
    """
    Start a Google Drive resumable upload session and return the upload URI.
    Uses the requests library directly for reliable HTTP 308 handling.
    """
    # Proactively refresh if we already know it's expired
    if not creds.valid and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        token_path.write_text(creds.to_json(), encoding="utf-8")

    token = creds.token
    if not token:
        creds.refresh(GoogleAuthRequest())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        token = creds.token

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/mp4",
        "X-Upload-Content-Length": str(size),
    }
    metadata = {
        "name": name,
        "parents": [folder_id],
        "description": description,
        "copyRequiresWriterPermission": True,
    }
    
    def do_post():
        return requests.post(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&supportsAllDrives=true&fields=id,name,size,webViewLink",
            headers=headers,
            json=metadata,
            timeout=30,
        )

    resp = do_post()
    
    # If Drive rejects the token despite our checks, force a refresh and retry
    if resp.status_code == 401 and creds.refresh_token:
        log.warning("Drive returned HTTP 401. Forcing a token refresh and retrying...")
        creds.refresh(GoogleAuthRequest())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        headers["Authorization"] = f"Bearer {creds.token}"
        resp = do_post()

    if resp.status_code != 200:
        raise RuntimeError(f"Failed to initiate Drive resumable upload: HTTP {resp.status_code} {resp.text[:300]}")
    location = resp.headers.get("Location")
    if not location:
        raise RuntimeError("Drive did not return a resumable upload URI.")
    return location


def _query_resume_offset(upload_uri: str) -> int:
    """Ask Drive how many bytes it has received so far (for crash recovery)."""
    resp = requests.put(
        upload_uri,
        headers={"Content-Range": "bytes */*", "Content-Length": "0"},
        timeout=30,
    )
    if resp.status_code == 308:
        range_hdr = resp.headers.get("Range", "")
        if range_hdr:
            return int(range_hdr.split("-")[1]) + 1
        return 0
    if resp.status_code in (200, 201):
        return -1  # already complete
    raise RuntimeError(f"Unexpected status querying resume offset: {resp.status_code}")


def upload_recording(
    drive,
    zoom: ZoomClient,
    rec: Recording,
    folder: dict,
    session_num: int,
    bot: Optional["TelegramBot"] = None,
) -> Optional[dict]:
    """
    Stream a Zoom recording directly into Google Drive using a pure-requests
    resumable upload. This uses requests for reliable HTTP 308 handling,
    which eliminates issues with HTTP 308 "Resume Incomplete" responses.

    If bot is provided, a single Telegram message is sent at the start and then
    edited in-place to show live upload progress.
    """
    course_code = folder.get("code", "Course")
    folder_id = folder["id"]
    name = build_drive_filename(rec, course_code, session_num)

    existing = find_file(drive, folder_id, name)
    if existing:
        print(f"  = Already in Drive folder as {name!r}; not uploading again.")
        # Hand the existing file back (flagged) so the docx link can still be
        # added, e.g. when a previous run uploaded fine but the docx step failed.
        return {**existing, "existing": True}

    size = rec.file_size or zoom.get_content_length(rec.download_url)
    if size <= 0:
        raise ZoomError("Recording reports 0 bytes.")

    description = f"Zoom meeting {rec.meeting_id} | host {rec.host_email}"

    # We need the raw OAuth credentials object to generate bearer tokens.
    creds = _load_google_credentials(folder)
    token_path = folder["token_file"] if folder and "token_file" in folder else config.GOOGLE_OAUTH_TOKEN_FILE

    # ── Initiate resumable upload session ──────────────────────────────────
    upload_uri = _initiate_resumable_upload(creds, folder_id, name, size, description, token_path)
    log.debug("Resumable upload URI obtained for %s", name)

    chunk_size = config.UPLOAD_CHUNK_SIZE
    offset = 0
    failures = 0
    started = time.time()

    # ── Telegram live progress tracking ────────────────────────────────────
    # One message is sent, then edited in-place. Updates fire when:
    #   • a new 10% boundary is crossed, OR
    #   • at least 25 MB has been sent since the last update
    _PROGRESS_INTERVAL_BYTES = 25 * 1024 * 1024  # 25 MB
    _PROGRESS_INTERVAL_PCT   = 10                 # 10 %
    progress_msg_id: Optional[int] = None
    last_reported_pct: int = -1
    last_reported_bytes: int = 0

    def _send_progress(current_offset: int, final: bool = False, speed_bps: int = 0) -> None:
        """Send or edit the Telegram progress message."""
        nonlocal progress_msg_id
        if not bot:
            return
        pct = int(current_offset / size * 100) if size else 0
        bar_filled = pct // 5          # 0-20 blocks
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        if final:
            elapsed = max(time.time() - started, 0.001)
            spd = human_size(int(size / elapsed))
            text = (
                f"✅ <b>Upload complete!</b>\n"
                f"<code>{name}</code>\n\n"
                f"[{bar}] 100%\n"
                f"{human_size(size)} in {elapsed:.0f}s — {spd}/s"
            )
        else:
            text = (
                f"📤 <b>Uploading…</b>\n"
                f"<code>{name}</code>\n\n"
                f"[{bar}] {pct}%\n"
                f"{human_size(current_offset)} / {human_size(size)}"
            )
        if progress_msg_id:
            bot.edit_message(progress_msg_id, text)
        else:
            progress_msg_id = bot.send_message(text)

    def _maybe_update_progress(current_offset: int) -> None:
        """Fire a progress update only if enough has changed."""
        nonlocal last_reported_pct, last_reported_bytes
        if not bot:
            return
        pct_now = int(current_offset / size * 100) if size else 0
        pct_boundary = (pct_now // _PROGRESS_INTERVAL_PCT) * _PROGRESS_INTERVAL_PCT
        bytes_since = current_offset - last_reported_bytes
        if pct_boundary > last_reported_pct or bytes_since >= _PROGRESS_INTERVAL_BYTES:
            last_reported_pct = pct_boundary
            last_reported_bytes = current_offset
            _send_progress(current_offset)
    zoom_resp: Optional[requests.Response] = None

    def _zoom_stream_at(start: int) -> requests.Response:
        nonlocal zoom_resp
        if zoom_resp is not None:
            try:
                zoom_resp.close()
            except Exception:
                pass
            zoom_resp = None
        zoom_resp = zoom.open_download(rec.download_url, start=start)
        return zoom_resp

    try:
        _zoom_stream_at(0)
        _send_progress(0)   # send the initial "0%" progress message

        while offset < size:
            # Refresh token if it has expired
            if creds.expired or not creds.token:
                creds.refresh(GoogleAuthRequest())

            chunk_end = min(offset + chunk_size, size) - 1  # inclusive
            actual_chunk = chunk_end - offset + 1

            # Read the chunk from the Zoom stream
            buf = bytearray(actual_chunk)
            view = memoryview(buf)
            got = 0
            try:
                while got < actual_chunk:
                    n = zoom_resp.raw.readinto(view[got:])
                    if not n:
                        break
                    got += n
            except (urllib3.exceptions.HTTPError, requests.RequestException, OSError) as exc:
                log.warning("Zoom download interrupted at offset %d: %s", offset, exc)
                failures += 1
                if failures > config.MAX_RETRIES:
                    raise ConnectionError(f"Zoom download failed after {failures} attempts: {exc}") from exc
                backoff_sleep(failures)
                _zoom_stream_at(offset)
                continue

            if got < actual_chunk:
                # Stream ended early; try to recover
                failures += 1
                if failures > config.MAX_RETRIES:
                    raise ConnectionError("Zoom stream ended early repeatedly; giving up.")
                log.warning("Zoom stream ended early (%d/%d bytes); retrying from offset %d", got, actual_chunk, offset)
                backoff_sleep(failures)
                _zoom_stream_at(offset)
                continue

            chunk_bytes = bytes(buf)

            # ── PUT the chunk to Drive ────────────────────────────────────
            headers = {
                "Authorization": f"Bearer {creds.token}",
                "Content-Length": str(actual_chunk),
                "Content-Range": f"bytes {offset}-{chunk_end}/{size}",
            }
            try:
                put_resp = requests.put(upload_uri, headers=headers, data=chunk_bytes,
                                        timeout=(10, config.HTTP_TIMEOUT_SECONDS))
            except requests.RequestException as exc:
                log.warning("Drive PUT network error: %s", exc)
                failures += 1
                if failures > config.MAX_RETRIES:
                    raise
                backoff_sleep(failures)
                # Re-query where Drive got to
                try:
                    offset = _query_resume_offset(upload_uri)
                    if offset < 0:
                        break  # somehow complete
                    _zoom_stream_at(offset)
                except Exception:
                    pass
                continue

            status_code = put_resp.status_code

            if status_code in (200, 201):
                # Upload complete
                offset = size
                failures = 0
                data = put_resp.json()
                print(f"\r  Uploading: 100.0%  ({human_size(size)} / {human_size(size)})", flush=True)
                elapsed = max(time.time() - started, 0.001)
                print(f"\n  + Uploaded {human_size(size)} in {elapsed:.0f}s ({human_size(int(size / elapsed))}/s)")
                _send_progress(size, final=True)   # ← edit to ✅ complete
                return data

            elif status_code == 308:
                # Resume Incomplete — Drive acknowledges this chunk
                failures = 0
                range_hdr = put_resp.headers.get("Range", "")
                if range_hdr:
                    offset = int(range_hdr.split("-")[1]) + 1
                else:
                    offset += actual_chunk
                pct = offset / size * 100
                print(f"\r  Uploading: {pct:5.1f}%  ({human_size(offset)} / {human_size(size)})", end="", flush=True)
                _maybe_update_progress(offset)     # ← throttled Telegram edit

            elif status_code in (500, 502, 503, 504):
                failures += 1
                if failures > config.MAX_RETRIES:
                    raise RuntimeError(f"Drive returned HTTP {status_code} {config.MAX_RETRIES} times; aborting.")
                log.warning("Drive transient error HTTP %d; will resume (attempt %d).", status_code, failures)
                backoff_sleep(failures)
                try:
                    new_offset = _query_resume_offset(upload_uri)
                    if new_offset < 0:
                        break  # complete
                    if new_offset != offset:
                        log.debug("Drive resume offset corrected %d -> %d", offset, new_offset)
                        offset = new_offset
                        _zoom_stream_at(offset)
                except Exception:
                    pass

            elif status_code == 429:
                failures += 1
                retry_after = float(put_resp.headers.get("Retry-After", 0))
                log.warning("Drive rate-limited (429); backing off.")
                backoff_sleep(failures, retry_after)

            else:
                raise RuntimeError(
                    f"Drive upload failed with unexpected HTTP {status_code}: {put_resp.text[:300]}"
                )

    finally:
        if zoom_resp is not None:
            try:
                zoom_resp.close()
            except Exception:
                pass

    # Should not reach here unless offset == size without a 200/201
    elapsed = max(time.time() - started, 0.001)
    print(f"\n  + Uploaded {human_size(size)} in {elapsed:.0f}s ({human_size(int(size / elapsed))}/s)")
    _send_progress(size, final=True)
    return None

