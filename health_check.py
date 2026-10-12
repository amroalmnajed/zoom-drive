#!/usr/bin/env python3
"""
health_check.py - Lightweight HTTP health check server for Railway deployment monitoring.
Run on a separate port (default 8080) to avoid conflicts with the bot.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify

import config

log = logging.getLogger("zoom_to_drive.health")

app = Flask(__name__)

# Get port from environment or use default
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))


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


@app.route("/health")
def health_check():
    """Health check endpoint for Railway monitoring."""
    checks = {
        "zoom": check_zoom_credentials(),
        "telegram": check_telegram_credentials(),
        "google": check_google_credentials(),
        "state": check_state_files(),
    }

    all_ok = all(status for status, _ in checks.values())
    
    response = {
        "status": "ok" if all_ok else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {
            "zoom": {"status": checks["zoom"][0], "message": checks["zoom"][1]},
            "telegram": {"status": checks["telegram"][0], "message": checks["telegram"][1]},
            "google": {"status": checks["google"][0], "message": checks["google"][1]},
            "state": {"status": checks["state"][0], "message": checks["state"][1]},
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
