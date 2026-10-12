#!/usr/bin/env python3
"""
validate_env.py - Environment variable validation helper.
Run this script to check if all required environment variables are set correctly.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent))

import config


def print_section(title: str) -> None:
    """Print a section header."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def validate_env() -> int:
    """Validate environment variables and return exit code."""
    errors = []
    warnings = []
    
    print_section("Environment Variable Validation")
    
    # Check Zoom credentials
    print("\n📹 Zoom Credentials:")
    if config.ZOOM_ACCOUNT_ID:
        print(f"  ✅ ZOOM_ACCOUNT_ID: {config.ZOOM_ACCOUNT_ID[:10]}...")
    else:
        errors.append("ZOOM_ACCOUNT_ID is not set")
        print(f"  ❌ ZOOM_ACCOUNT_ID is not set")
    
    if config.ZOOM_CLIENT_ID:
        print(f"  ✅ ZOOM_CLIENT_ID: {config.ZOOM_CLIENT_ID[:10]}...")
    else:
        errors.append("ZOOM_CLIENT_ID is not set")
        print(f"  ❌ ZOOM_CLIENT_ID is not set")
    
    if config.ZOOM_CLIENT_SECRET:
        print(f"  ✅ ZOOM_CLIENT_SECRET: {'*' * 10}...")
    else:
        errors.append("ZOOM_CLIENT_SECRET is not set")
        print(f"  ❌ ZOOM_CLIENT_SECRET is not set")
    
    if config.ZOOM_USER_ID:
        print(f"  ✅ ZOOM_USER_ID: {config.ZOOM_USER_ID}")
    else:
        errors.append("ZOOM_USER_ID is not set")
        print(f"  ❌ ZOOM_USER_ID is not set")
    
    # Check Telegram credentials
    print("\n📱 Telegram Credentials:")
    if config.TELEGRAM_BOT_TOKEN:
        print(f"  ✅ TELEGRAM_BOT_TOKEN: {config.TELEGRAM_BOT_TOKEN[:10]}...")
    else:
        errors.append("TELEGRAM_BOT_TOKEN is not set")
        print(f"  ❌ TELEGRAM_BOT_TOKEN is not set")
    
    if config.TELEGRAM_CHAT_ID:
        print(f"  ✅ TELEGRAM_CHAT_ID: {config.TELEGRAM_CHAT_ID}")
    else:
        errors.append("TELEGRAM_CHAT_ID is not set")
        print(f"  ❌ TELEGRAM_CHAT_ID is not set")
    
    # Check Google Drive credentials
    print("\n☁️ Google Drive Credentials:")
    print(f"  ℹ️  GOOGLE_AUTH_MODE: {config.GOOGLE_AUTH_MODE}")
    
    if config.GOOGLE_AUTH_MODE == "oauth":
        if config.GOOGLE_OAUTH_CLIENT_FILE.exists():
            print(f"  ✅ OAuth client file: {config.GOOGLE_OAUTH_CLIENT_FILE}")
        else:
            errors.append(f"OAuth client file not found: {config.GOOGLE_OAUTH_CLIENT_FILE}")
            print(f"  ❌ OAuth client file not found: {config.GOOGLE_OAUTH_CLIENT_FILE}")
        
        if config.GOOGLE_OAUTH_TOKEN_FILE.exists():
            print(f"  ✅ OAuth token file: {config.GOOGLE_OAUTH_TOKEN_FILE}")
        else:
            warnings.append(f"OAuth token file not found: {config.GOOGLE_OAUTH_TOKEN_FILE} (will be created on first run)")
            print(f"  ⚠️  OAuth token file not found: {config.GOOGLE_OAUTH_TOKEN_FILE} (will be created on first run)")
    
    elif config.GOOGLE_AUTH_MODE == "service_account":
        if config.GOOGLE_SERVICE_ACCOUNT_FILE.exists():
            print(f"  ✅ Service account file: {config.GOOGLE_SERVICE_ACCOUNT_FILE}")
        else:
            errors.append(f"Service account file not found: {config.GOOGLE_SERVICE_ACCOUNT_FILE}")
            print(f"  ❌ Service account file not found: {config.GOOGLE_SERVICE_ACCOUNT_FILE}")
    
    # Check folder-specific tokens
    print("\n📁 Folder-Specific Tokens:")
    for key, folder in config.FOLDERS.items():
        if "token_file" in folder:
            if folder["token_file"].exists():
                print(f"  ✅ {key} ({folder['name']}): Token file exists")
            else:
                warnings.append(f"{key} ({folder['name']}): Token file not found (will be created on first use)")
                print(f"  ⚠️  {key} ({folder['name']}): Token file not found (will be created on first use)")
    
    # Check configuration settings
    print("\n⚙️  Configuration Settings:")
    print(f"  ℹ️  LOOKBACK_DAYS: {config.LOOKBACK_DAYS}")
    print(f"  ℹ️  LOCAL_TIMEZONE: {config.LOCAL_TIMEZONE}")
    print(f"  ℹ️  UPLOAD_CHUNK_MB: {config.UPLOAD_CHUNK_MB}")
    print(f"  ℹ️  SCHEDULED_RUN_TIME: {config.SCHEDULED_RUN_TIME or 'Not set'}")
    
    # Check folders
    print("\n📂 Folder Configuration:")
    for key, folder in config.FOLDERS.items():
        print(f"  ℹ️  {key}: {folder['name']} (ID: {folder['id']})")
    
    # Print summary
    print_section("Validation Summary")
    
    if not errors and not warnings:
        print("\n✅ All environment variables are configured correctly!")
        return 0
    
    if warnings:
        print(f"\n⚠️  {len(warnings)} warning(s):")
        for warning in warnings:
            print(f"  - {warning}")
    
    if errors:
        print(f"\n❌ {len(errors)} error(s):")
        for error in errors:
            print(f"  - {error}")
        print("\nPlease fix the errors above before running the bot.")
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(validate_env())
