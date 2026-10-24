#!/usr/bin/env python3
"""
validate_config.py - Helper script to validate environment variables and configuration.

Run this script to check if your environment variables and configuration are set up correctly
before running the main bot.

Usage:
    python validate_config.py
"""
import sys
from pathlib import Path

# Add the project directory to the path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from utils import setup_logging
import logging

log = logging.getLogger("zoom_to_drive")


def check_env_var(name: str, value: str, required: bool = True) -> tuple[bool, str]:
    """Check if an environment variable is set.

    Args:
        name: Environment variable name
        value: Value of the environment variable
        required: Whether the variable is required

    Returns:
        (is_valid, message) tuple
    """
    if not value:
        if required:
            return False, f"[X] Missing {name}"
        else:
            return True, f"[!] Optional {name} not set"
    return True, f"[OK] {name} is set"


def check_file_exists(path: Path, name: str, required: bool = True) -> tuple[bool, str]:
    """Check if a file exists.

    Args:
        path: Path to check
        name: Display name for the file
        required: Whether the file is required

    Returns:
        (is_valid, message) tuple
    """
    if path.exists():
        return True, f"[OK] {name} exists at {path}"
    if required:
        return False, f"[X] {name} not found at {path}"
    return True, f"[!] Optional {name} not found at {path}"


def main() -> int:
    """Run configuration validation checks."""
    print("=" * 60)
    print("Zoom-Drive Configuration Validation")
    print("=" * 60)
    print()

    all_valid = True

    # Check Zoom credentials
    print("[Zoom] Credentials:")
    zoom_vars = {
        "ZOOM_ACCOUNT_ID": config.ZOOM_ACCOUNT_ID,
        "ZOOM_CLIENT_ID": config.ZOOM_CLIENT_ID,
        "ZOOM_CLIENT_SECRET": config.ZOOM_CLIENT_SECRET,
        "ZOOM_USER_ID": config.ZOOM_USER_ID,
    }
    for var, value in zoom_vars.items():
        valid, msg = check_env_var(var, value, required=True)
        print(f"  {msg}")
        if not valid:
            all_valid = False
    print()

    # Check Telegram credentials
    print("[Telegram] Credentials:")
    telegram_vars = {
        "TELEGRAM_BOT_TOKEN": config.TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": config.TELEGRAM_CHAT_ID,
    }
    for var, value in telegram_vars.items():
        valid, msg = check_env_var(var, value, required=True)
        print(f"  {msg}")
        if not valid:
            all_valid = False
    print()

    # Check Google Drive credentials
    print("[Google Drive] Credentials:")
    if config.GOOGLE_AUTH_MODE == "oauth":
        print(f"  [i] Auth mode: OAuth")
        valid, msg = check_file_exists(config.GOOGLE_OAUTH_CLIENT_FILE, "OAuth client file", required=True)
        print(f"  {msg}")
        if not valid:
            all_valid = False
        valid, msg = check_file_exists(config.GOOGLE_OAUTH_TOKEN_FILE, "OAuth token file", required=False)
        print(f"  {msg}")
    elif config.GOOGLE_AUTH_MODE == "service_account":
        print(f"  [i] Auth mode: Service Account")
        valid, msg = check_file_exists(config.GOOGLE_SERVICE_ACCOUNT_FILE, "Service account key", required=True)
        print(f"  {msg}")
        if not valid:
            all_valid = False
    else:
        print(f"  [X] Invalid auth mode: {config.GOOGLE_AUTH_MODE}")
        all_valid = False
    print()

    # Check folder-specific tokens
    print("[Folder-Specific] Tokens:")
    for key, folder in config.FOLDERS.items():
        if "token_file" in folder:
            valid, msg = check_file_exists(folder["token_file"], f"Token for {folder['name']}", required=False)
            print(f"  {msg}")
    print()

    # Check configuration values
    print("[Configuration] Values:")
    print(f"  [i] LOOKBACK_DAYS: {config.LOOKBACK_DAYS}")
    print(f"  [i] UPLOAD_CHUNK_MB: {config.UPLOAD_CHUNK_MB}")
    print(f"  [i] LOCAL_TIMEZONE: {config.LOCAL_TIMEZONE}")
    print(f"  [i] SCHEDULED_RUN_TIME: {config.SCHEDULED_RUN_TIME or 'Not set'}")
    print(f"  [i] REMEMBER_SKIPPED: {config.REMEMBER_SKIPPED}")
    print(f"  [i] UNDO_DELETE_PERMANENTLY: {config.UNDO_DELETE_PERMANENTLY}")
    print()

    # Validate configuration
    print("[Configuration] Validation:")
    try:
        config.validate_config(need_google=True)
        print("  [OK] Configuration is valid")
    except ValueError as exc:
        print(f"  [X] Configuration error: {exc}")
        all_valid = False
    print()

    # Check state files
    print("[State] Files:")
    if config.STATE_FILE.exists():
        print(f"  [OK] State file exists: {config.STATE_FILE}")
    else:
        print(f"  [i] State file not found (will be created): {config.STATE_FILE}")
    if config.UNDO_FILE.exists():
        print(f"  [OK] Undo file exists: {config.UNDO_FILE}")
    else:
        print(f"  [i] Undo file not found (will be created): {config.UNDO_FILE}")
    print()

    # Summary
    print("=" * 60)
    if all_valid:
        print("[OK] All checks passed! Your configuration is ready.")
        print()
        print("You can now run the bot with:")
        print("  python bot_runtime.py --bot")
        return 0
    else:
        print("[X] Some checks failed. Please fix the issues above.")
        print()
        print("Tips:")
        print("  • Make sure all required environment variables are set in .env")
        print("  • Download OAuth credentials from Google Cloud Console")
        print("  • For service accounts, ensure the JSON key file exists")
        return 1


if __name__ == "__main__":
    sys.exit(main())
