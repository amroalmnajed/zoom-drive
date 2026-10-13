"""
config.py - All settings for the Zoom -> Google Drive uploader.

Secrets (Zoom credentials) are read from environment variables or from a local
`.env` file (see `.env.example`). Never hard-code secrets in this file.
Everything else (folders, keywords, tuning knobs) is edited right here.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Load a local .env file if python-dotenv is installed (it is in requirements.txt).
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # pragma: no cover - dotenv is optional
    pass


# --------------------------------------------------------------------------- #
# Zoom (Server-to-Server OAuth)
# --------------------------------------------------------------------------- #
ZOOM_ACCOUNT_ID = os.getenv("ZOOM_ACCOUNT_ID", "")
ZOOM_CLIENT_ID = os.getenv("ZOOM_CLIENT_ID", "")
ZOOM_CLIENT_SECRET = os.getenv("ZOOM_CLIENT_SECRET", "")

# --------------------------------------------------------------------------- #
# Telegram Settings
# --------------------------------------------------------------------------- #
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Server-to-Server OAuth apps cannot use the "me" alias. Put the email address
# (or Zoom user ID) of the account whose cloud recordings should be processed.
ZOOM_USER_ID = os.getenv("ZOOM_USER_ID", "")

ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
ZOOM_API_BASE = "https://api.zoom.us/v2"

# How far back to look for recordings on each run (days). Zoom only allows a
# 30-day window per request; the script splits longer ranges automatically.
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))

# Scheduled run configuration (cron-like)
# Format: "H:M" for daily runs at specific time (e.g., "02:00" for 2 AM)
# Set to empty string to disable scheduled runs
SCHEDULED_RUN_TIME = os.getenv("SCHEDULED_RUN_TIME", "")

# Timezone used when DISPLAYING start times and building Drive file names.
LOCAL_TIMEZONE = os.getenv("LOCAL_TIMEZONE", "Africa/Cairo")

# When a meeting has several MP4 files (speaker view, gallery view, screen
# share...), the first type found in this list is chosen. If none match, the
# largest MP4 is used.
PREFERRED_RECORDING_TYPES = [
    "shared_screen_with_speaker_view",
    "shared_screen_with_gallery_view",
    "active_speaker",
    "gallery_view",
    "shared_screen",
]


# --------------------------------------------------------------------------- #
# Google Drive
# --------------------------------------------------------------------------- #
# "oauth"           -> sign in once as YOUR Google user (recommended for folders
#                      in a normal "My Drive"; uses your storage quota).
# "service_account" -> only works if the folders live in a SHARED DRIVE. Service
#                      accounts have no storage quota of their own in My Drive.
GOOGLE_AUTH_MODE = os.getenv("GOOGLE_AUTH_MODE", "oauth").lower()

GOOGLE_OAUTH_CLIENT_FILE = Path(
    os.getenv("GOOGLE_OAUTH_CLIENT_FILE", BASE_DIR / "credentials.json")
)
GOOGLE_OAUTH_TOKEN_FILE = Path(
    os.getenv("GOOGLE_OAUTH_TOKEN_FILE", BASE_DIR / "token.json")
)
GOOGLE_SERVICE_ACCOUNT_FILE = Path(
    os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", BASE_DIR / "service_account.json")
)

# If JSON strings are passed as environment variables (e.g. on Railway/Cloud), write them to disk
def _write_env_json(env_key: str, file_path: Path):
    """Write a JSON env var to disk, normalizing it to remove control characters.

    Railway's raw editor and copy-paste can inject literal newlines inside the
    JSON string value, which makes json.loads() raise
    'Invalid control character at line 1 column N'. We fix this by:
      1. Stripping surrounding whitespace and quote wrappers.
      2. Removing all literal CR/LF/tab characters that appear OUTSIDE of
         proper JSON string escapes (i.e. bare control chars in the raw text).
      3. Parsing the JSON and re-dumping it so the file is always clean.
    We always overwrite (not skip-if-exists) so a previously corrupted file
    is self-healed on the next restart.
    """
    import json as _json
    val = os.getenv(env_key)
    if not val:
        return
    # Strip surrounding whitespace and stray quote wrappers added by Railway
    clean_val = val.strip().strip("'").strip('"').strip()
    # Remove bare (unescaped) ASCII control characters: \x00-\x1f except
    # legitimate JSON whitespace (\t \n \r) which json.loads handles fine.
    # We do a blanket removal of CR and LF because they must NEVER appear
    # literally inside a JSON string value (only as \n \r escapes).
    clean_val = clean_val.replace("\r", "").replace("\n", "").replace("\t", " ")
    try:
        parsed = _json.loads(clean_val)
        normalized = _json.dumps(parsed, ensure_ascii=False)
        file_path.write_text(normalized, encoding="utf-8")
    except Exception:
        # Last resort: write as-is and let the caller surface the error
        try:
            file_path.write_text(clean_val, encoding="utf-8")
        except Exception:
            pass

_write_env_json("GOOGLE_CREDENTIALS_JSON", GOOGLE_OAUTH_CLIENT_FILE)
# Still write the default one just in case
_write_env_json("GOOGLE_TOKEN_JSON", GOOGLE_OAUTH_TOKEN_FILE)



# Full Drive scope is required to upload into folders that already exist.
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive"]


# --------------------------------------------------------------------------- #
# Folder routing rules
# --------------------------------------------------------------------------- #
# Keys ("1".."4") are what the user types in the terminal to override.
# Matching is case-insensitive, whole-word (so "Main" will NOT match
# "Maintenance"), and the LONGEST matching keyword across all folders wins
# (so "Pre-IGCSE Crash 1" -> Pre-igcse, not Combine Crash).
FOLDERS: dict[str, dict] = {
    "1": {
        "name": "Main-Revision",
        "code": "Main",
        "id": "1FG-D6YaW2I2VtWRGJl65Nz0OPu8HoRjb",
        "keywords": ["Main", "Combined Main"],
        "token_env": "GOOGLE_TOKEN_MAIN_JSON",
        "token_file": BASE_DIR / "token_main.json",
    },
    "2": {
        "name": "Combine Crash",
        "code": "Crash",
        "id": "1zSHOl52WfwWROnp1pq2O4aLpmFfx7Tv8",
        "keywords": ["Crash 1", "Combined Crash", "Combined Crash 1"],
        "token_env": "GOOGLE_TOKEN_CRASH_JSON",
        "token_file": BASE_DIR / "token_crash.json",
    },
    "3": {
        "name": "IGCSE",
        "code": "G10",
        "id": "1U0nJ4owRepRAf-hGjARDxWaHliLs4QA9",
        "keywords": ["Private G10", "G10"],
        "token_env": "GOOGLE_TOKEN_G10_JSON",
        "token_file": BASE_DIR / "token_g10.json",
    },
    "4": {
        "name": "Pre-igcse",
        "code": "G9",
        "id": "1DC7Qfj1vrR32AcLR3Qgc7NjTId4iJOkw",
        "keywords": ["Pre-IGCSE", "Pre", "pre", "pre-", "Pre-"],
        "token_env": "GOOGLE_TOKEN_G9_JSON",
        "token_file": BASE_DIR / "token_g9.json",
    },
}

# Folder suggested when NO keyword matches. Change to any key above.
DEFAULT_FOLDER_KEY = "1"

# Write folder-specific tokens from env variables
for _f in FOLDERS.values():
    if "token_env" in _f and "token_file" in _f:
        _write_env_json(_f["token_env"], _f["token_file"])


# --------------------------------------------------------------------------- #
# Behaviour / tuning
# --------------------------------------------------------------------------- #
# Upload chunk size in MiB. Must be a multiple of 0.25 (256 KiB). Peak RAM use is
# roughly 2x this value. 16 is a good default; use 8 on very small machines.
UPLOAD_CHUNK_MB = int(os.getenv("UPLOAD_CHUNK_MB", "16"))
UPLOAD_CHUNK_SIZE = UPLOAD_CHUNK_MB * 1024 * 1024

# Retry policy for network errors, HTTP 429 and HTTP 5xx.
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "6"))
BACKOFF_BASE_SECONDS = float(os.getenv("BACKOFF_BASE_SECONDS", "2"))
BACKOFF_MAX_SECONDS = int(os.getenv("BACKOFF_MAX_SECONDS", "60"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "120"))

# Zoom API rate limiting (calls per second)
ZOOM_RATE_LIMIT = float(os.getenv("ZOOM_RATE_LIMIT", "10"))

# If True, videos you skipped with 's' are not offered again on later runs.
# If False, they are shown again until uploaded (until they leave the lookback window).
REMEMBER_SKIPPED = True

STATE_FILE = BASE_DIR / "processed.json"
UNDO_FILE = BASE_DIR / "undo_history.json"

# /undo moves the uploaded video to the Drive Bin (recoverable for 30 days).
# Set UNDO_DELETE_PERMANENTLY=1 to delete it for good and free the space at once.
UNDO_DELETE_PERMANENTLY = os.getenv("UNDO_DELETE_PERMANENTLY", "1") == "1"
LOG_FILE = BASE_DIR / "zoom_to_drive.log"

# Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Recording size filters (in MB)
# Set MIN_RECORDING_SIZE_MB to skip recordings smaller than this size
# Set MAX_RECORDING_SIZE_MB to skip recordings larger than this size
# Set to 0 or empty to disable the filter
MIN_RECORDING_SIZE_MB = int(os.getenv("MIN_RECORDING_SIZE_MB", "0"))
MAX_RECORDING_SIZE_MB = int(os.getenv("MAX_RECORDING_SIZE_MB", "0"))

# Recording metadata extraction
# Set to True to enable participant data fetching from Zoom API
FETCH_PARTICIPANTS = os.getenv("FETCH_PARTICIPANTS", "true").lower() == "true"

# Set to True to enable video metadata extraction (resolution, bitrate) using FFmpeg
# Requires FFmpeg binary to be installed on the system
ENABLE_VIDEO_METADATA = os.getenv("ENABLE_VIDEO_METADATA", "true").lower() == "true"

# Set to True to enable thumbnail generation from recordings using FFmpeg
# Requires FFmpeg binary to be installed on the system
ENABLE_THUMBNAILS = os.getenv("ENABLE_THUMBNAILS", "true").lower() == "true"

# Time offset in seconds to capture thumbnail frame (default: 2.0)
THUMBNAIL_TIME_OFFSET = float(os.getenv("THUMBNAIL_TIME_OFFSET", "2.0"))

# Telegram webhook settings
# Set to True to use webhooks instead of polling (requires public HTTPS endpoint)
TELEGRAM_USE_WEBHOOK = os.getenv("TELEGRAM_USE_WEBHOOK", "false").lower() == "true"

# Webhook URL (required if webhook enabled)
# Format: https://your-domain.com/webhook/<bot_token>
TELEGRAM_WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL", "")

# Optional secret token for webhook security validation
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

# Webhook server configuration
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8443"))
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_config(need_google: bool = True) -> None:
    """Raise ValueError with a readable message if something is misconfigured."""
    problems: list[str] = []

    # Zoom credentials
    for name in ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET", "ZOOM_USER_ID"):
        if not globals().get(name):
            problems.append(f"Missing Zoom credential: {name} (set it in .env or Railway Variables).")

    # Telegram credentials
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not globals().get(name):
            problems.append(f"Missing Telegram credential: {name} (set it in .env or Railway Variables).")

    if LOOKBACK_DAYS < 1:
        problems.append("LOOKBACK_DAYS must be >= 1. (Recommended: 7-30 days)")
    if UPLOAD_CHUNK_SIZE % (256 * 1024) != 0 or UPLOAD_CHUNK_SIZE <= 0:
        problems.append("UPLOAD_CHUNK_MB must be a positive multiple of 0.25. (Recommended: 8-16 MB)")
    if DEFAULT_FOLDER_KEY not in FOLDERS:
        problems.append(f"DEFAULT_FOLDER_KEY {DEFAULT_FOLDER_KEY!r} is not in FOLDERS. (Check config.py FOLDERS dict)")
    for key, folder in FOLDERS.items():
        if not folder.get("id") or not folder.get("name"):
            problems.append(f"Folder {key} needs both 'name' and 'id'. (Check config.py FOLDERS dict)")

    if need_google:
        if GOOGLE_AUTH_MODE == "oauth":
            if not GOOGLE_OAUTH_CLIENT_FILE.exists() and not GOOGLE_OAUTH_TOKEN_FILE.exists():
                problems.append(
                    f"OAuth client file not found: {GOOGLE_OAUTH_CLIENT_FILE} "
                    "(download it from Google Cloud Console and save as credentials.json)."
                )
        elif GOOGLE_AUTH_MODE == "service_account":
            if not GOOGLE_SERVICE_ACCOUNT_FILE.exists():
                problems.append(f"Service account key not found: {GOOGLE_SERVICE_ACCOUNT_FILE}")
        else:
            problems.append("GOOGLE_AUTH_MODE must be 'oauth' or 'service_account'.")

    # Check for keyword conflicts
    keyword_map: dict[str, str] = {}
    for key, folder in FOLDERS.items():
        for kw in folder.get("keywords", []):
            kw_lower = kw.strip().lower()
            if kw_lower in keyword_map and keyword_map[kw_lower] != key:
                problems.append(
                    f"Keyword conflict: '{kw}' is used in both folder '{keyword_map[kw_lower]}' and '{key}'."
                )
            keyword_map[kw_lower] = key

    # Validate timezone
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(LOCAL_TIMEZONE)
    except Exception:
        problems.append(f"Invalid LOCAL_TIMEZONE: {LOCAL_TIMEZONE}")

    # Validate size filters
    if MIN_RECORDING_SIZE_MB < 0:
        problems.append("MIN_RECORDING_SIZE_MB must be >= 0.")
    if MAX_RECORDING_SIZE_MB < 0:
        problems.append("MAX_RECORDING_SIZE_MB must be >= 0.")
    if MIN_RECORDING_SIZE_MB > 0 and MAX_RECORDING_SIZE_MB > 0 and MIN_RECORDING_SIZE_MB > MAX_RECORDING_SIZE_MB:
        problems.append("MIN_RECORDING_SIZE_MB cannot be greater than MAX_RECORDING_SIZE_MB.")

    # Validate retry policy
    if MAX_RETRIES < 0:
        problems.append("MAX_RETRIES must be >= 0.")
    if BACKOFF_BASE_SECONDS < 0:
        problems.append("BACKOFF_BASE_SECONDS must be >= 0.")
    if BACKOFF_MAX_SECONDS < 0:
        problems.append("BACKOFF_MAX_SECONDS must be >= 0.")
    if HTTP_TIMEOUT_SECONDS < 1:
        problems.append("HTTP_TIMEOUT_SECONDS must be >= 1.")
    if ZOOM_RATE_LIMIT <= 0:
        problems.append("ZOOM_RATE_LIMIT must be > 0.")

    # Validate webhook configuration
    if TELEGRAM_USE_WEBHOOK and not TELEGRAM_WEBHOOK_URL:
        problems.append("TELEGRAM_USE_WEBHOOK is True but TELEGRAM_WEBHOOK_URL is not set.")
    if WEBHOOK_PORT not in (80, 88, 443, 8443):
        problems.append("WEBHOOK_PORT must be one of: 80, 88, 443, 8443 (Telegram requirement).")
    if THUMBNAIL_TIME_OFFSET < 0:
        problems.append("THUMBNAIL_TIME_OFFSET must be >= 0.")

    if problems:
        raise ValueError("Configuration problems:\n  - " + "\n  - ".join(problems))
