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

# Server-to-Server OAuth apps cannot use the "me" alias. Put the email address
# (or Zoom user ID) of the account whose cloud recordings should be processed.
ZOOM_USER_ID = os.getenv("ZOOM_USER_ID", "")

ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
ZOOM_API_BASE = "https://api.zoom.us/v2"

# How far back to look for recordings on each run (days). Zoom only allows a
# 30-day window per request; the script splits longer ranges automatically.
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))

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
    val = os.getenv(env_key)
    if val and not file_path.exists():
        # Railway raw editor might leave surrounding single quotes; strip them
        clean_val = val.strip().strip("'").strip('"')
        try:
            file_path.write_text(clean_val, encoding="utf-8")
        except Exception:
            pass

_write_env_json("GOOGLE_TOKEN_JSON", GOOGLE_OAUTH_TOKEN_FILE)
_write_env_json("GOOGLE_CREDENTIALS_JSON", GOOGLE_OAUTH_CLIENT_FILE)

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
    },
    "2": {
        "name": "Combine Crash",
        "code": "Crash",
        "id": "1zSHOl52WfwWROnp1pq2O4aLpmFfx7Tv8",
        "keywords": ["Crash 1", "Combined Crash", "Combined Crash 1"],
    },
    "3": {
        "name": "IGCSE",
        "code": "G10",
        "id": "1U0nJ4owRepRAf-hGjARDxWaHliLs4QA9",
        "keywords": ["Private G10", "G10"],
    },
    "4": {
        "name": "Pre-igcse",
        "code": "G9",
        "id": "1DC7Qfj1vrR32AcLR3Qgc7NjTId4iJOkw",
        "keywords": ["Pre-IGCSE", "Pre", "pre", "pre-", "Pre-"],
    },
}

# Folder suggested when NO keyword matches. Change to any key above.
DEFAULT_FOLDER_KEY = "1"


# --------------------------------------------------------------------------- #
# Behaviour / tuning
# --------------------------------------------------------------------------- #
# Upload chunk size in MiB. Must be a multiple of 0.25 (256 KiB). Peak RAM use is
# roughly 2x this value. 16 is a good default; use 8 on very small machines.
UPLOAD_CHUNK_MB = int(os.getenv("UPLOAD_CHUNK_MB", "16"))
UPLOAD_CHUNK_SIZE = UPLOAD_CHUNK_MB * 1024 * 1024

# Retry policy for network errors, HTTP 429 and HTTP 5xx.
MAX_RETRIES = 6
BACKOFF_BASE_SECONDS = 2
BACKOFF_MAX_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 120

# If True, videos you skipped with 's' are not offered again on later runs.
# If False, they are shown again until uploaded (until they leave the lookback window).
REMEMBER_SKIPPED = True

STATE_FILE = BASE_DIR / "processed.json"
LOG_FILE = BASE_DIR / "zoom_to_drive.log"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_config(need_google: bool = True) -> None:
    """Raise ValueError with a readable message if something is misconfigured."""
    problems: list[str] = []

    for name in ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET", "ZOOM_USER_ID"):
        if not globals()[name]:
            problems.append(f"Missing {name} (set it in .env or the environment).")

    if LOOKBACK_DAYS < 1:
        problems.append("LOOKBACK_DAYS must be >= 1.")
    if UPLOAD_CHUNK_SIZE % (256 * 1024) != 0 or UPLOAD_CHUNK_SIZE <= 0:
        problems.append("UPLOAD_CHUNK_MB must be a positive multiple of 0.25.")
    if DEFAULT_FOLDER_KEY not in FOLDERS:
        problems.append(f"DEFAULT_FOLDER_KEY {DEFAULT_FOLDER_KEY!r} is not in FOLDERS.")
    for key, folder in FOLDERS.items():
        if not folder.get("id") or not folder.get("name"):
            problems.append(f"Folder {key} needs both 'name' and 'id'.")

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

    if problems:
        raise ValueError("Configuration problems:\n  - " + "\n  - ".join(problems))


# --------------------------------------------------------------------------- #
# Telegram Settings
# --------------------------------------------------------------------------- #
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
