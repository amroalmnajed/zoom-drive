"""
error_handler.py - Enhanced error handling and notification utilities.
Provides detailed error categorization, context, and suggested fixes.
"""
from __future__ import annotations

import logging
import traceback
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import config

log = logging.getLogger("zoom_to_drive")


class ErrorCategory:
    """Error category constants."""
    ZOOM = "Zoom"
    DRIVE = "Google Drive"
    TELEGRAM = "Telegram"
    DOCX = "Docx"
    NETWORK = "Network"
    CONFIGURATION = "Configuration"
    UNKNOWN = "Unknown"


def categorize_error(error: Exception, context: dict) -> tuple[str, str]:
    """Categorize an error and return (category, suggested_fix).
    
    Args:
        error: The exception that occurred
        context: Dictionary with context (recording_id, folder, session_num, etc.)
    
    Returns:
        Tuple of (category_name, suggested_fix)
    """
    err_str = str(error).lower()
    err_type = type(error).__name__
    
    # Zoom-related errors
    if "zoom" in err_str or "401" in err_str or "403" in err_str:
        if "token" in err_str or "auth" in err_str:
            return ErrorCategory.ZOOM, "Zoom token expired or invalid. Check ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET in environment variables."
        elif "download" in err_str or "stream" in err_str:
            return ErrorCategory.ZOOM, "Failed to download recording from Zoom. The recording may have been deleted or is not accessible."
        return ErrorCategory.ZOOM, "Zoom API error. Check Zoom credentials and permissions."
    
    # Drive-related errors
    if "drive" in err_str or "google" in err_str or "resumable" in err_str:
        if "quota" in err_str or "storage" in err_str:
            return ErrorCategory.DRIVE, "Google Drive storage quota exceeded. Free up space or upgrade storage."
        elif "404" in err_str:
            return ErrorCategory.DRIVE, "Google Drive folder not found. Check folder ID in config.py."
        elif "403" in err_str or "permission" in err_str:
            return ErrorCategory.DRIVE, "Permission denied for Google Drive. Check OAuth token and folder sharing settings."
        elif "timeout" in err_str:
            return ErrorCategory.DRIVE, "Google Drive upload timeout. Check network connection and try again."
        return ErrorCategory.DRIVE, "Google Drive error. Check credentials and folder access."
    
    # Telegram-related errors
    if "telegram" in err_str or "bot" in err_str:
        if "token" in err_str or "401" in err_str:
            return ErrorCategory.TELEGRAM, "Telegram bot token invalid. Check TELEGRAM_BOT_TOKEN in environment variables."
        elif "chat" in err_str or "403" in err_str:
            return ErrorCategory.TELEGRAM, "Bot blocked by user or invalid chat ID. Check TELEGRAM_CHAT_ID and ensure bot is not blocked."
        return ErrorCategory.TELEGRAM, "Telegram API error. Check bot token and chat ID."
    
    # Docx-related errors
    if "docx" in err_str or "python-docx" in err_str:
        return ErrorCategory.DOCX, "Docx file processing error. The file may be corrupted or in an unsupported format."
    
    # Network errors
    if "timeout" in err_str or "connection" in err_str or "network" in err_str:
        return ErrorCategory.NETWORK, "Network error. Check internet connection and try again."
    
    # Configuration errors
    if "config" in err_str or "missing" in err_str or "environment" in err_str:
        return ErrorCategory.CONFIGURATION, "Configuration error. Check all required environment variables are set."
    
    return ErrorCategory.UNKNOWN, f"Unexpected error ({err_type}). Check logs for details."


def format_error_notification(
    error: Exception,
    context: dict,
    category: Optional[str] = None,
    suggested_fix: Optional[str] = None
) -> str:
    """Format a detailed error notification for Telegram.
    
    Args:
        error: The exception that occurred
        context: Dictionary with context (recording_id, folder, session_num, etc.)
        category: Error category (auto-detected if not provided)
        suggested_fix: Suggested fix (auto-detected if not provided)
    
    Returns:
        Formatted markdown string for Telegram message
    """
    if category is None or suggested_fix is None:
        category, suggested_fix = categorize_error(error, context)
    
    # Build context lines
    context_lines = []
    if context.get("recording_id"):
        context_lines.append(f"📄 <b>Recording ID:</b> <code>{context['recording_id']}</code>")
    if context.get("topic"):
        context_lines.append(f"📝 <b>Topic:</b> {context['topic']}")
    if context.get("folder"):
        context_lines.append(f"📁 <b>Folder:</b> {context['folder']}")
    if context.get("session_num"):
        context_lines.append(f"🔢 <b>Session:</b> {context['session_num']}")
    if context.get("filename"):
        context_lines.append(f"📎 <b>File:</b> <code>{context['filename']}</code>")
    
    now_str = datetime.now(ZoneInfo(config.LOCAL_TIMEZONE)).strftime("%Y-%m-%d %H:%M")
    
    # Build error message
    message = [
        f"❌ <b>Error: {category}</b>",
        "",
        *context_lines,
        f"🕐 <b>Time:</b> {now_str}",
        "",
        f"<b>Error:</b> <code>{str(error)[:500]}</code>",
        "",
        f"💡 <b>Suggested Fix:</b>",
        suggested_fix,
    ]
    
    # Add retry suggestion
    message.extend([
        "",
        f"ℹ️ Recording marked as <i>failed</i> — send /reset then /run to retry it."
    ])
    
    return "\n".join(message)


def log_error_with_traceback(error: Exception, context: dict) -> None:
    """Log error with full traceback for debugging.
    
    Args:
        error: The exception that occurred
        context: Dictionary with context information
    """
    category, _ = categorize_error(error, context)
    log.error(
        "Error in category '%s': %s\nContext: %s\nTraceback:\n%s",
        category,
        error,
        context,
        traceback.format_exc()
    )
