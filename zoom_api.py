import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from typing import Iterator, Optional
import requests
import config
from models import ZoomError, Recording
from utils import backoff_sleep, parse_zoom_time

class RateLimiter:
    """Simple token bucket rate limiter for API calls."""
    
    def __init__(self, calls_per_second: float = 10):
        """Initialize rate limiter.
        
        Args:
            calls_per_second: Maximum API calls per second
        """
        self.min_interval = 1.0 / calls_per_second
        self.last_call = 0.0
    
    def wait(self) -> None:
        """Wait if necessary to respect rate limit."""
        now = time.time()
        elapsed = now - self.last_call
        if elapsed < self.min_interval:
            sleep_time = self.min_interval - elapsed
            time.sleep(sleep_time)
        self.last_call = time.time()

class ZoomClient:
    """Client for Zoom Server-to-Server OAuth API."""

    def __init__(self) -> None:
        """Initialize Zoom API client."""
        self._session = requests.Session()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._rate_limiter = RateLimiter(calls_per_second=10)  # Zoom allows ~10 calls/sec

    def _fetch_token(self) -> None:
        """Fetch OAuth token from Zoom API with retry logic."""
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
        """Get valid OAuth token, refreshing if expired or near expiry.

        Returns:
            Valid OAuth access token
        """
        if not self._token or time.time() >= self._expires_at - 300:
            self._fetch_token()
        assert self._token is not None
        return self._token

    def _request(
        self, method: str, url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None, stream: bool = False
    ) -> requests.Response:
        """Make authenticated HTTP request to Zoom API with retry logic.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Request URL
            params: Query parameters
            headers: Additional headers
            stream: Whether to stream response

        Returns:
            Response object
        """
        # Apply rate limiting before making the request
        self._rate_limiter.wait()
        
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

    def iter_recordings(self, days_back: int, fetch_participants: bool = False) -> Iterator[Recording]:
        """Iterate over recordings from the last N days.

        Args:
            days_back: Number of days to look back
            fetch_participants: Whether to fetch participant data

        Yields:
            Recording objects
        """
        today = datetime.now(timezone.utc).date()
        window_start = today - timedelta(days=days_back)
        seen: set[str] = set()
        while window_start <= today:
            window_end = min(window_start + timedelta(days=29), today)
            yield from self._iter_window(window_start.isoformat(), window_end.isoformat(), seen, fetch_participants=fetch_participants)
            window_start = window_end + timedelta(days=1)

    def _iter_window(self, date_from: str, date_to: str, seen: set[str], fetch_participants: bool = False) -> Iterator[Recording]:
        url = f"{config.ZOOM_API_BASE}/users/{quote(config.ZOOM_USER_ID, safe='@')}/recordings"
        params: dict = {"from": date_from, "to": date_to, "page_size": 300}
        while True:
            data = self._request("GET", url, params=params).json()
            for meeting in data.get("meetings") or []:
                rec = self._meeting_to_recording(meeting, fetch_participants=fetch_participants)
                if rec and rec.key not in seen:
                    seen.add(rec.key)
                    yield rec
            token = data.get("next_page_token")
            if not token:
                return
            params["next_page_token"] = token

    def _meeting_to_recording(self, meeting: dict, fetch_participants: bool = False) -> Optional[Recording]:
        topic = (meeting.get("topic") or "").strip() or "Untitled Meeting"
        meeting_id = str(meeting.get("id") or "unknown")
        meeting_uuid = str(meeting.get("uuid") or "")

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

        # Fetch participants if requested
        participants = None
        if fetch_participants and meeting_uuid:
            participants = self.get_meeting_participants(meeting_uuid)

        return Recording(
            meeting_id=meeting_id,
            meeting_uuid=meeting_uuid,
            topic=topic,
            host_email=(meeting.get("host_email") or "unknown host"),
            start_time=parse_zoom_time(meeting.get("start_time")) or rec_start,
            duration_min=duration,
            file_id=str(primary.get("id") or ""),
            file_size=int(primary["file_size"]) if primary.get("file_size") else None,
            download_url=primary["download_url"],
            recording_type=str(primary.get("recording_type") or "video"),
            participants=participants,
        )

    def open_download(self, url: str, start: int = 0) -> requests.Response:
        """Open a streaming download connection for a recording.

        Args:
            url: Recording download URL
            start: Byte offset to resume from (for resumable downloads)

        Returns:
            Streaming response object

        Raises:
            ZoomError: If download fails or Range request not honored
        """
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
        """Get the content length of a recording.

        Args:
            url: Recording download URL

        Returns:
            Content length in bytes

        Raises:
            ZoomError: If content length cannot be determined
        """
        resp = self.open_download(url)
        try:
            length = resp.headers.get("Content-Length")
        finally:
            resp.close()
        if not length:
            raise ZoomError("Cannot determine recording size.")
        return int(length)

    def get_meeting_participants(self, meeting_uuid: str) -> list[str]:
        """Get list of participant emails for a meeting.

        Args:
            meeting_uuid: Meeting UUID

        Returns:
            List of participant email addresses
        """
        try:
            url = f"{config.ZOOM_API_BASE}/past_meetings/{meeting_uuid}/participants"
            data = self._request("GET", url).json()
            participants = []
            for p in data.get("participants", []):
                email = p.get("email") or p.get("name")
                if email:
                    participants.append(str(email))
            return participants
        except Exception as exc:
            # Participant data may not be available for all meetings
            # Return empty list instead of failing
            return []

