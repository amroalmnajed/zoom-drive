import logging
import random
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

import config
from models import Recording

log = logging.getLogger("zoom_to_drive")

def setup_logging() -> None:
    """Configure logging with console and rotating file handlers.

    Log level is controlled by LOG_LEVEL environment variable (default: INFO).
    """
    # Get log level from config, default to INFO
    log_level_str = getattr(config, 'LOG_LEVEL', 'INFO')
    log_level = getattr(logging, log_level_str.upper(), logging.INFO)

    log.setLevel(log_level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    # Console handler - use the configured log level
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(log_level)
    console.setFormatter(fmt)

    # Use RotatingFileHandler to prevent log file from growing indefinitely
    # Keep 10 files of 10MB each (100MB total)
    # File handler always logs at DEBUG level for troubleshooting
    file_handler = RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=10,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    log.addHandler(console)
    log.addHandler(file_handler)
def backoff_sleep(attempt: int, retry_after: Optional[float] = None) -> None:
    """Sleep with exponential backoff for retry attempts.

    Args:
        attempt: Current retry attempt number (1-indexed)
        retry_after: Optional Retry-After header value in seconds
    """
    if retry_after and retry_after > 0:
        delay = min(retry_after, 300)
    else:
        delay = min(config.BACKOFF_MAX_SECONDS, config.BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
    delay += random.uniform(0, 1)
    log.warning("Retrying in %.1fs (attempt %d)", delay, attempt)
    time.sleep(delay)

def human_size(num_bytes: Optional[int]) -> str:
    """Convert byte count to human-readable string.

    Args:
        num_bytes: Number of bytes to convert

    Returns:
        Human-readable string (e.g., "1.5 MB", "500 B")
    """
    if not num_bytes:
        return "unknown size"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"

def human_duration(minutes: Optional[float]) -> str:
    """Convert minutes to human-readable duration string.

    Args:
        minutes: Duration in minutes

    Returns:
        Human-readable string (e.g., "1h 30m", "45m")
    """
    if minutes is None:
        return "unknown"
    total = int(round(minutes))
    hours, mins = divmod(total, 60)
    return f"{hours}h {mins:02d}m" if hours else f"{mins}m"

def parse_zoom_time(value: Optional[str]) -> Optional[datetime]:
    """Parse Zoom API timestamp string to datetime object.

    Args:
        value: ISO 8601 timestamp string (may end with 'Z')

    Returns:
        Parsed datetime with UTC timezone, or None if parsing fails
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_drive_filename(rec: Recording, course_code: str, session_num: int) -> str:
    """Build standardized filename for Drive upload.

    Args:
        rec: Recording object with metadata
        course_code: Course code (e.g., "Main", "Crash", "G10")
        session_num: Session number

    Returns:
        Filename in format: GMTYYYYMMDD_COURSE_S##.mp4
    """
    if rec.start_time:
        date_str = rec.start_time.astimezone(timezone.utc).strftime("%Y%m%d")
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"GMT{date_str}_{course_code}_S{session_num}.mp4"
