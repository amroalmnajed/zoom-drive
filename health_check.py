#!/usr/bin/env python3
"""
health_check.py - Lightweight HTTP health check server for Railway deployment monitoring.
Run on a separate port (default 8080) to avoid conflicts with the bot.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify

import config

log = logging.getLogger("zoom_to_drive.health")

app = Flask(__name__)

# Get port from environment (Render injects PORT, Railway uses HEALTH_PORT/PORT) or use default
HEALTH_PORT = int(os.getenv("PORT", os.getenv("HEALTH_PORT", "8080")))

# Simple cache for API connectivity checks (30 seconds)
_last_health_check_time = 0
_cached_health_checks = {}


def check_zoom_credentials() -> tuple[bool, str]:
    """Check if Zoom credentials are configured."""
    if not config.ZOOM_ACCOUNT_ID or not config.ZOOM_CLIENT_ID or not config.ZOOM_CLIENT_SECRET:
        return False, "Zoom credentials missing"
    return True, "OK"


def check_telegram_credentials() -> tuple[bool, str]:
    """Check if Telegram credentials are configured."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False, "Telegram credentials missing"
    return True, "OK"


def check_google_credentials() -> tuple[bool, str]:
    """Check if Google Drive credentials are configured."""
    if config.GOOGLE_AUTH_MODE == "oauth":
        if not config.GOOGLE_OAUTH_CLIENT_FILE.exists() and not config.GOOGLE_OAUTH_TOKEN_FILE.exists():
            return False, f"OAuth files not found: {config.GOOGLE_OAUTH_CLIENT_FILE}"
    elif config.GOOGLE_AUTH_MODE == "service_account":
        if not config.GOOGLE_SERVICE_ACCOUNT_FILE.exists():
            return False, f"Service account file not found: {config.GOOGLE_SERVICE_ACCOUNT_FILE}"
    return True, "OK"


def check_state_files() -> tuple[bool, str]:
    """Check if state files are accessible."""
    try:
        if config.STATE_FILE.exists():
            # Try to read it to ensure it's valid JSON
            json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
        if config.UNDO_FILE.exists():
            json.loads(config.UNDO_FILE.read_text(encoding="utf-8"))
        return True, "OK"
    except Exception as e:
        return False, f"State file error: {e}"


def check_zoom_api() -> tuple[bool, str]:
    """Verify Zoom API connectivity by attempting to get a token."""
    try:
        from zoom_api import ZoomClient
        client = ZoomClient()
        client._get_token()  # Lightweight token fetch
        return True, "OK"
    except Exception as e:
        return False, str(e)


def check_google_api() -> tuple[bool, str]:
    """Verify Google Drive API connectivity with a lightweight list call."""
    try:
        from drive_api import get_drive_service
        drive = get_drive_service()
        # Simple list call with minimal fields
        drive.files().list(pageSize=1, fields="files(id)",
                           supportsAllDrives=True).execute()
        return True, "OK"
    except Exception as e:
        return False, str(e)


def check_telegram_api() -> tuple[bool, str]:
    """Verify Telegram API connectivity with getMe call."""
    try:
        import requests
        resp = requests.get(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getMe",
                           timeout=10)
        return resp.status_code == 200, "OK" if resp.status_code == 200 else f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


@app.route("/health")
def health_check():
    """Health check endpoint for Railway monitoring."""
    global _last_health_check_time, _cached_health_checks

    # Use cached results if less than 30 seconds old
    now = time.time()
    if now - _last_health_check_time < 30 and _cached_health_checks:
        checks = _cached_health_checks
    else:
        checks = {
            "zoom": check_zoom_credentials(),
            "telegram": check_telegram_credentials(),
            "google": check_google_credentials(),
            "state": check_state_files(),
            "zoom_api": check_zoom_api(),
            "google_api": check_google_api(),
            "telegram_api": check_telegram_api(),
        }
        _cached_health_checks = checks
        _last_health_check_time = now

    all_ok = all(status for status, _ in checks.values())

    response = {
        "status": "ok" if all_ok else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {
            "zoom": {"status": checks["zoom"][0], "message": checks["zoom"][1]},
            "telegram": {"status": checks["telegram"][0], "message": checks["telegram"][1]},
            "google": {"status": checks["google"][0], "message": checks["google"][1]},
            "state": {"status": checks["state"][0], "message": checks["state"][1]},
            "zoom_api": {"status": checks["zoom_api"][0], "message": checks["zoom_api"][1]},
            "google_api": {"status": checks["google_api"][0], "message": checks["google_api"][1]},
            "telegram_api": {"status": checks["telegram_api"][0], "message": checks["telegram_api"][1]},
        }
    }

    status_code = 200 if all_ok else 503
    return jsonify(response), status_code


@app.route("/")
def index():
    """Root endpoint with basic info."""
    return jsonify({
        "service": "zoom-drive health check",
        "version": "1.0",
        "health_endpoint": "/health"
    })


def main():
    """Run the health check server."""
    # Setup basic logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    log.info("Starting health check server on port %d", HEALTH_PORT)
    app.run(host="0.0.0.0", port=HEALTH_PORT, threaded=True)


if __name__ == "__main__":
    main()
