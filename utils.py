import logging
import random
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional, Tuple
import config
from models import Recording

log = logging.getLogger("zoom_to_drive")

def setup_logging() -> None:
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
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
def build_drive_filename(rec: Recording, course_code: str, session_num: int) -> str:
    if rec.start_time:
        date_str = rec.start_time.astimezone(timezone.utc).strftime("%Y%m%d")
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"GMT{date_str}_{course_code}_S{session_num}.mp4"
