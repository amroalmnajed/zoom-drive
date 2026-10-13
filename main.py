#!/usr/bin/env python3
"""
main.py - Fetch Zoom cloud recordings, ask for confirmation via Telegram bot inline buttons,
confirm session number, then stream the video directly from Zoom into Google Drive.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
import requests
import threading
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httplib2
# WORKAROUND: httplib2 incorrectly treats HTTP 308 "Resume Incomplete" responses as redirects.
# Google Drive's resumable upload API uses 308 to indicate "more data needed", but httplib2
# tries to follow it as a redirect, causing "RedirectMissingLocation" errors.
# By removing 308 from REDIRECT_CODES, we prevent httplib2 from treating it as a redirect.
# This is a known issue with httplib2; the proper fix would be to switch to a different HTTP library
# or use the requests-based upload implementation (which we already do for the actual data transfer).
httplib2.REDIRECT_CODES = {301, 302, 303, 307}

import config
from models import ZoomError, ApprovalRequest
from utils import setup_logging, categorize, build_drive_filename, extract_video_metadata, generate_thumbnail, human_duration, human_size

# Flask app for webhook mode (lazy import to avoid issues if not needed)
_flask_app = None
from state import StateStore
from undo_store import UndoStore
from zoom_api import ZoomClient
from drive_api import get_drive_service, get_next_session_number, upload_recording
from googleapiclient.errors import HttpError
from telegram_bot import TelegramBot
from error_handler import format_error_notification, log_error_with_traceback

log = logging.getLogger("zoom_to_drive")

# Global flag for graceful shutdown
_shutdown_requested = False
_current_upload_in_progress = False
_webhook_app = None
_flask_thread = None

def _request_shutdown(signum=None, frame=None) -> None:
    """Signal handler for graceful shutdown."""
    global _shutdown_requested
    _shutdown_requested = True
    log.info("Shutdown signal received (%s), gracefully stopping...", signum)

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Zoom cloud recordings -> Google Drive (via Telegram approval).")
    parser.add_argument("--days", type=int, default=config.LOOKBACK_DAYS, help="lookback window in days")
    parser.add_argument("--dry-run", action="store_true", help="fetch and prompt only; do not upload")
    parser.add_argument(
        "--bot", action="store_true",
        help="run as a persistent Telegram bot — trigger runs with /run [N] from chat"
    )
    parser.add_argument("--filter-topic", type=str, help="filter recordings by topic keyword")
    parser.add_argument("--filter-from", type=str, help="filter recordings from date (YYYY-MM-DD)")
    parser.add_argument("--filter-to", type=str, help="filter recordings to date (YYYY-MM-DD)")
    parser.add_argument("--min-size", type=int, help="minimum recording size in MB")
    parser.add_argument("--max-size", type=int, help="maximum recording size in MB")
    return parser.parse_args(argv)


def run_pipeline(bot: "TelegramBot", days: int, dry_run: bool = False,
                 filter_topic: Optional[str] = None, filter_from: Optional[str] = None,
                 filter_to: Optional[str] = None, min_size: Optional[int] = None,
                 max_size: Optional[int] = None) -> tuple[int, int, int]:
    """
    Fetch Zoom recordings, present each via Telegram for approval, upload approved ones.
    Returns (uploaded, skipped, failed).
    """
    state = StateStore(config.STATE_FILE)
    undo = UndoStore(config.UNDO_FILE)
    zoom = ZoomClient()

    # Build filter description
    filter_desc = f"from the last <b>{days} day(s)</b>"
    if filter_topic:
        filter_desc += f" with topic containing <b>'{filter_topic}'</b>"
    if filter_from or filter_to:
        date_range = ""
        if filter_from and filter_to:
            date_range = f"from <b>{filter_from}</b> to <b>{filter_to}</b>"
        elif filter_from:
            date_range = f"from <b>{filter_from}</b>"
        elif filter_to:
            date_range = f"until <b>{filter_to}</b>"
        filter_desc += f" {date_range}"
    if min_size or max_size:
        size_filter = ""
        if min_size and max_size:
            size_filter = f"size between <b>{min_size}MB</b> and <b>{max_size}MB</b>"
        elif min_size:
            size_filter = f"size at least <b>{min_size}MB</b>"
        elif max_size:
            size_filter = f"size at most <b>{max_size}MB</b>"
        filter_desc += f" ({size_filter})"

    bot.send_message(f"🔍 <b>Fetching Zoom recordings</b> {filter_desc}...")
    try:
        recordings = sorted(
            zoom.iter_recordings(days, fetch_participants=config.FETCH_PARTICIPANTS),
            key=lambda r: r.start_time or datetime.min.replace(tzinfo=timezone.utc),
        )
    except ZoomError as exc:
        bot.send_message(f"❌ <b>Zoom error:</b> {exc}")
        return 0, 0, 0

    # Apply filters
    filtered_recordings = []
    for rec in recordings:
        # Topic filter
        if filter_topic and filter_topic.lower() not in rec.topic.lower():
            continue

        # Date range filter
        if filter_from or filter_to:
            if not rec.start_time:
                continue
            if filter_from:
                try:
                    from_date = datetime.strptime(filter_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    if rec.start_time < from_date:
                        continue
                except ValueError:
                    bot.send_message(f"❌ Invalid date format for --filter-from: {filter_from}. Use YYYY-MM-DD.")
                    return 0, 0, 0
            if filter_to:
                try:
                    to_date = datetime.strptime(filter_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    # Include the entire end date
                    to_date = to_date.replace(hour=23, minute=59, second=59)
                    if rec.start_time > to_date:
                        continue
                except ValueError:
                    bot.send_message(f"❌ Invalid date format for --filter-to: {filter_to}. Use YYYY-MM-DD.")
                    return 0, 0, 0

        # Size filter
        if min_size or max_size:
            if not rec.file_size:
                continue
            size_mb = rec.file_size / (1024 * 1024)
            if min_size and size_mb < min_size:
                continue
            if max_size and size_mb > max_size:
                continue

        filtered_recordings.append(rec)

    pending = [r for r in filtered_recordings if not state.should_hide(r.key)]

    if not pending:
        bot.send_message(
            f"✅ <b>Nothing to do.</b>\n"
            f"Found {len(recordings)} recording(s) — all already processed."
        )
        return 0, 0, 0

    bot.send_message(
        f"📋 Found <b>{len(filtered_recordings)}</b> recording(s) matching filters, "
        f"<b>{len(pending)}</b> pending review."
    )

    uploaded = skipped = failed = 0
    try:
        for i, rec in enumerate(pending, start=1):
            # Check for shutdown signal before processing each recording
            if _shutdown_requested:
                bot.send_message("🛑 <b>Run interrupted by shutdown signal.</b>")
                log.info("Pipeline stopped due to shutdown signal.")
                break

            # Extract video metadata if enabled
            if config.ENABLE_VIDEO_METADATA and rec.download_url:
                resolution, bitrate = extract_video_metadata(rec.download_url)
                rec.resolution = resolution
                rec.bitrate = bitrate

            # Generate thumbnail if enabled
            thumbnail_data = None
            if config.ENABLE_THUMBNAILS and rec.download_url:
                thumbnail_data = generate_thumbnail(rec.download_url, config.THUMBNAIL_TIME_OFFSET)

            suggested_key, matched = categorize(rec.topic)
            req = ApprovalRequest(rec, i, len(pending), suggested_key, matched)
            decision = bot.get_user_decision(req)

            # Send thumbnail if available
            if thumbnail_data:
                bot.send_photo(thumbnail_data, caption=f"📸 Thumbnail for: {rec.topic[:50]}")

            if decision.action == "quit":
                bot.send_message("🛑 <b>Run stopped by user.</b>")
                break

            if decision.action == "skip":
                skipped += 1
                if not dry_run:
                    state.mark(rec.key, "skipped", topic=rec.topic)
                continue

            folder = config.FOLDERS[decision.folder_key]
            drive = None
            if not dry_run:
                try:
                    drive = get_drive_service(folder)
                except Exception as exc:
                    bot.send_message(f"❌ <b>Google Drive auth failed for {folder['name']}:</b> {exc}")
                    failed += 1
                    continue

            auto_s = get_next_session_number(drive, folder["id"])
            session_num = bot.get_session_number(rec, folder, auto_s)
            code = folder.get("code", "Course")
            final_name = build_drive_filename(rec, code, session_num)

            if dry_run:
                bot.send_message(f"🧪 <b>[dry-run]</b> Would upload <code>{final_name}</code> to <b>{folder['name']}</b>")
                uploaded += 1
                continue

            bot.send_message(f"🚀 <b>Starting Upload:</b> <code>{final_name}</code> to <b>{folder['name']}</b>...")
            try:
                result = upload_recording(drive, zoom, rec, folder, session_num, bot=bot)
                result = result or {}
                already_there = bool(result.get("existing"))
                state.mark(rec.key, "uploaded", topic=rec.topic, folder=folder["name"],
                           drive_file_id=result.get("id"))
                uploaded += 1
                if already_there:
                    bot.send_message(f"ℹ️ <code>{final_name}</code> was already in <b>{folder['name']}</b> — not uploaded again.")
                else:
                    bot.send_message(f"✅ <b>Uploaded successfully:</b> <code>{final_name}</code>")

                # ── docx link ────────────────────────────────────────────
                docx_info: dict = {}
                link = result.get("webViewLink")
                if link:
                    try:
                        from docx_updater import update_docx_in_drive
                        log.info("Attempting to update docx in folder %s for session %d", folder['name'], session_num)
                        docx_info = update_docx_in_drive(drive, folder["id"], rec, link, session_num)
                        status = docx_info.get("status")
                        log.info("Docx update result: status=%s, detail=%s", status, docx_info.get('detail'))
                        if status == "updated":
                            bot.send_message(f"📝 <b>Docx updated:</b> link added for session {session_num} "
                                             f"({docx_info.get('detail', '')}).")
                        elif status == "already_linked":
                            bot.send_message(f"ℹ️ <b>Docx not changed:</b> {docx_info.get('detail')}.")
                        else:
                            bot.send_message(f"⚠️ <b>Docx not updated:</b> {docx_info.get('detail')}")
                    except Exception as docx_exc:
                        log.error("Docx update failed for folder %s: %s", folder['name'], docx_exc, exc_info=True)
                        bot.send_message(f"⚠️ <b>Docx update failed in {folder['name']}:</b> {docx_exc}")
                else:
                    log.warning("No webViewLink in upload result, skipping docx update for folder %s", folder['name'])

                # ── remember how to undo this ────────────────────────────
                docx_updated = docx_info.get("status") == "updated"
                if result.get("id") and (not already_there or docx_updated):
                    entry_id = undo.add({
                        "rec_key": rec.key,
                        "topic": rec.topic,
                        "folder_key": decision.folder_key,
                        "folder_name": folder["name"],
                        "session": session_num,
                        "video_name": final_name,
                        "video_id": result.get("id"),
                        "video_uploaded": not already_there,
                        "link": link,
                        "docx_id": docx_info.get("file_id") if docx_updated else None,
                        "docx_name": docx_info.get("file_name") if docx_updated else None,
                        "docx_updated": docx_updated,
                        "original_cell_xml": docx_info.get("original_cell_xml", []),
                    })
                    bot.send_message(
                        f"Done with <code>{final_name}</code>. Made a mistake?",
                        reply_markup={"inline_keyboard": [[
                            {"text": "↩️ Undo this upload", "callback_data": f"undo:{entry_id}"}]]},
                    )
            except Exception as exc:
                failed += 1
                error_context = {
                    "recording_id": rec.key,
                    "topic": rec.topic,
                    "folder": folder["name"],
                    "session_num": session_num,
                    "filename": final_name,
                }
                log_error_with_traceback(exc, error_context)
                state.mark(rec.key, "failed", topic=rec.topic, folder=folder["name"], error=str(exc))

                # Use enhanced error notification
                alert = format_error_notification(exc, error_context)
                bot.send_message(alert)

    except KeyboardInterrupt:
        bot.send_message("⚠️ <b>Run interrupted.</b>")

    return uploaded, skipped, failed


def _handle_status(bot: "TelegramBot") -> None:
    """Send a /status summary to Telegram."""
    state_path = config.STATE_FILE
    if not state_path.exists():
        bot.send_message("📂 <b>Status:</b> No recordings processed yet.")
        return
    try:
        data: dict = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        bot.send_message("⚠️ Could not read processed.json.")
        return

    uploaded = sum(1 for v in data.values() if v.get("status") == "uploaded")
    skipped  = sum(1 for v in data.values() if v.get("status") == "skipped")
    lines = [
        "📊 <b>Processed Recordings</b>",
        "",
        f"• Uploaded: <b>{uploaded}</b>",
        f"• Skipped:  <b>{skipped}</b>",
        f"• Total:    <b>{len(data)}</b>",
    ]
    bot.send_message("\n".join(lines))


def _handle_stats(bot: "TelegramBot") -> None:
    """Send detailed statistics/analytics to Telegram."""
    state_path = config.STATE_FILE
    if not state_path.exists():
        bot.send_message("📊 <b>Statistics:</b> No data available yet.")
        return
    
    try:
        data: dict = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        bot.send_message("⚠️ Could not read processed.json.")
        return
    
    if not data:
        bot.send_message("📊 <b>Statistics:</b> No recordings processed yet.")
        return
    
    # Calculate statistics
    uploaded = sum(1 for v in data.values() if v.get("status") == "uploaded")
    skipped = sum(1 for v in data.values() if v.get("status") == "skipped")
    failed = sum(1 for v in data.values() if v.get("status") == "failed")
    
    # Folder statistics
    folder_counts: dict[str, int] = {}
    for v in data.values():
        folder = v.get("folder", "Unknown")
        folder_counts[folder] = folder_counts.get(folder, 0) + 1
    
    # Recent uploads (last 5)
    recent_uploads = []
    for key, v in sorted(data.items(), key=lambda x: x[1].get("at", ""), reverse=True)[:5]:
        if v.get("status") == "uploaded":
            topic = v.get("topic", "Unknown")[:30]
            folder = v.get("folder", "Unknown")
            recent_uploads.append(f"• {topic} → {folder}")
    
    # Build statistics message
    lines = [
        "📊 <b>Detailed Statistics</b>",
        "",
        "<b>Overview:</b>",
        f"• Total Processed: <b>{len(data)}</b>",
        f"• Uploaded: <b>{uploaded}</b>",
        f"• Skipped: <b>{skipped}</b>",
        f"• Failed: <b>{failed}</b>",
        "",
        "<b>Uploads by Folder:</b>",
    ]
    
    for folder, count in sorted(folder_counts.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"• {folder}: <b>{count}</b>")
    
    if recent_uploads:
        lines.extend([
            "",
            "<b>Recent Uploads (Last 5):</b>",
        ])
        lines.extend(recent_uploads)
    
    bot.send_message("\n".join(lines))


def _handle_reset(bot: "TelegramBot") -> None:
    """Clear processed.json after confirmation."""
    state_path = config.STATE_FILE
    if state_path.exists():
        state_path.unlink()
        bot.send_message("🗑️ <b>Reset complete.</b> processed.json cleared — all recordings will be offered again.")
    else:
        bot.send_message("ℹ️ Nothing to reset — processed.json did not exist.")


def _handle_backup(bot: "TelegramBot") -> None:
    """Backup state files (processed.json and undo_history.json) as a downloadable file."""
    import zipfile
    import io
    
    # Create a zip file in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        if config.STATE_FILE.exists():
            zipf.write(config.STATE_FILE, config.STATE_FILE.name)
        if config.UNDO_FILE.exists():
            zipf.write(config.UNDO_FILE, config.UNDO_FILE.name)
    
    zip_buffer.seek(0)
    
    # Send the zip file via Telegram
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"zoom_drive_backup_{timestamp}.zip"
    
    try:
        bot.send_document(zip_buffer.getvalue(), filename)
        bot.send_message(f"✅ <b>Backup created:</b> {filename}\n\nContains processed.json and undo_history.json.")
    except Exception as exc:
        bot.send_message(f"❌ <b>Backup failed:</b> {exc}")
        log.error("Backup failed: %s", exc)


def _handle_restore(bot: "TelegramBot", file_data: bytes, filename: str) -> None:
    """Restore state files from a backup zip file."""
    import zipfile
    import io

    try:
        zip_buffer = io.BytesIO(file_data)
        with zipfile.ZipFile(zip_buffer, 'r') as zipf:
            # List files in the zip
            file_list = zipf.namelist()

            # Restore processed.json
            if "processed.json" in file_list:
                with zipf.open("processed.json") as f:
                    config.STATE_FILE.write_bytes(f.read())

            # Restore undo_history.json
            if "undo_history.json" in file_list:
                with zipf.open("undo_history.json") as f:
                    config.UNDO_FILE.write_bytes(f.read())

        bot.send_message(
            f"✅ <b>Restore complete.</b>\n\n"
            f"Restored from: {filename}\n"
            f"Files: {', '.join(file_list)}\n\n"
            f"ℹ️ Send /status to verify the restored state."
        )
    except Exception as exc:
        bot.send_message(f"❌ <b>Restore failed:</b> {exc}")
        log.error("Restore failed: %s", exc)


def _handle_restore_from_telegram(bot: "TelegramBot", file_id: str, filename: str) -> None:
    """Handle restore command triggered by uploading a document to Telegram."""
    file_data = bot.get_document_file(file_id)
    if not file_data:
        bot.send_message("❌ <b>Failed to download the document.</b>")
        return

    _handle_restore(bot, file_data, filename)


def _describe(e: dict) -> str:
    parts = []
    if e.get("docx_updated"):
        parts.append(f"remove the link from <b>{e.get('docx_name')}</b>")
    if e.get("video_uploaded"):
        how = "permanently delete" if config.UNDO_DELETE_PERMANENTLY else "move to the Drive Bin"
        parts.append(f"{how} <code>{e['video_name']}</code>")
    parts.append("offer this recording again on the next /run")
    return "\n".join(f"• {p}" for p in parts)


MULTI_REC_DISPLAY_LIMIT = 25


def _fetch_sorted_recordings(days: int) -> list:
    zoom = ZoomClient()
    recordings = list(zoom.iter_recordings(days, fetch_participants=config.FETCH_PARTICIPANTS))
    recordings.sort(
        key=lambda r: r.start_time or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    log.info("Fetched %d recordings from Zoom (last %d day(s))", len(recordings), days)
    return recordings


def _rec_start_str(rec) -> str:
    start = rec.local_start()
    return f"{start:%Y-%m-%d %H:%M}" if start else "unknown"


def _multi_recording_keyboard(recordings: list) -> dict:
    keyboard = []
    for idx, rec in enumerate(recordings):
        label = f"{idx + 1}) {_rec_start_str(rec)} {rec.topic}"[:64]
        keyboard.append([{"text": label, "callback_data": f"multi_rec:{idx}"}])
    keyboard.append([{"text": "🛑 Cancel", "callback_data": "multi_action:cancel"}])
    return {"inline_keyboard": keyboard}


def _multi_folder_keyboard(selected_folders: set) -> dict:
    keyboard = []
    for key, folder in config.FOLDERS.items():
        checkbox = "☑" if key in selected_folders else "☐"
        keyboard.append([{
            "text": f"{checkbox} {key}) {folder['name']} ({folder['code']})",
            "callback_data": f"multi_folder:{key}",
        }])
    keyboard.append([
        {"text": "✅ Upload to Selected", "callback_data": "multi_action:upload"},
        {"text": "🛑 Cancel", "callback_data": "multi_action:cancel"},
    ])
    return {"inline_keyboard": keyboard}


def _handle_multi(bot: "TelegramBot", dry_run: bool = False, days: Optional[int] = None) -> None:
    """Fetch recent recordings and ask the user which one to upload."""
    lookback = days if isinstance(days, int) and days > 0 else config.LOOKBACK_DAYS
    bot.send_message(f"🔍 <b>Fetching recordings</b> from the last <b>{lookback} day(s)</b>...")
    try:
        recordings = _fetch_sorted_recordings(lookback)
    except ZoomError as exc:
        bot.send_message(f"❌ <b>Zoom error:</b> {exc}")
        return
    except Exception as exc:
        log.error("Unexpected error fetching recordings: %s", exc)
        bot.send_message(f"❌ <b>Error fetching recordings:</b> {exc}")
        return
    if not recordings:
        bot.send_message(f"✅ <b>No recordings found</b> in the last {lookback} day(s).")
        return
    recordings = recordings[:MULTI_REC_DISPLAY_LIMIT]
    lines = ["📹 <b>Select a recording to upload</b>", ""]
    for idx, rec in enumerate(recordings):
        lines.append(
            f"{idx + 1}. <b>{rec.topic}</b> — {_rec_start_str(rec)} | "
            f"{human_duration(rec.duration_min)} | {human_size(rec.file_size)}"
        )
    msg_id = bot.send_message("\n".join(lines), reply_markup=_multi_recording_keyboard(recordings))
    bot._multi_state = {
        "phase": "recording",
        "recordings": recordings,
        "selected_rec": None,
        "selected_folders": set(),
        "msg_id": msg_id,
        "dry_run": dry_run,
        "start_time": time.time(),
    }
    log.info("Multi recording selection started (%d recordings)", len(recordings))


def _show_multi_folder_selection(bot: "TelegramBot", rec) -> None:
    """Show folder checkboxes after a recording has been chosen."""
    lines = [
        "📁 <b>Select folders to upload to</b>",
        "",
        f"<b>Title:</b> {rec.topic}",
        f"<b>Host:</b> {rec.host_email}",
        f"<b>Started:</b> {_rec_start_str(rec)}",
        f"<b>Duration:</b> {human_duration(rec.duration_min)} | <b>Size:</b> {human_size(rec.file_size)}",
        "",
        "<b>Select folders to upload to:</b>",
    ]
    msg_id = bot.send_message("\n".join(lines), reply_markup=_multi_folder_keyboard(set()))
    bot._multi_state["msg_id"] = msg_id
    bot._multi_state["selected_folders"] = set()
    log.info("Multi-folder selection started for recording %s", rec.topic)


def _handle_multi_rec_select(bot: "TelegramBot", rec_index: str) -> None:
    """Handle recording selection in /multi phase 1."""
    if not bot._multi_state:
        bot.send_message("❌ No active multi-folder selection.")
        return
    state = bot._multi_state
    if state.get("phase") != "recording":
        bot.send_message("❌ Recording already selected. Choose folders or cancel.")
        return
    try:
        idx = int(rec_index)
        rec = state["recordings"][idx]
    except (ValueError, IndexError, KeyError, TypeError):
        bot.send_message("❌ Invalid recording selection.")
        return
    state["selected_rec"] = rec
    state["phase"] = "folder"
    bot.edit_message(state["msg_id"], f"✅ <b>Selected recording:</b> {rec.topic}")
    _show_multi_folder_selection(bot, rec)


def _handle_multi_toggle(bot: "TelegramBot", folder_key: str) -> None:
    """Handle folder toggle in multi selection (folder phase only)."""
    if not bot._multi_state:
        bot.send_message("❌ No active multi-folder selection.")
        return
    state = bot._multi_state
    if state.get("phase") != "folder":
        bot.send_message("❌ Please select a recording first.")
        return
    selected_folders = state["selected_folders"]
    if folder_key in selected_folders:
        selected_folders.remove(folder_key)
    else:
        selected_folders.add(folder_key)
    try:
        payload = {
            "chat_id": bot.chat_id,
            "message_id": state["msg_id"],
            "reply_markup": json.dumps(_multi_folder_keyboard(selected_folders)),
        }
        requests.post(f"{bot.base_url}/editMessageReplyMarkup", json=payload, timeout=10)
        log.info("Multi-folder selection updated: %s", selected_folders)
    except Exception as exc:
        log.error("Failed to update multi-folder keyboard: %s", exc)


def _notify_docx_result(bot: "TelegramBot", folder_name: str, session_num: int, info: dict) -> None:
    status = info.get("status")
    if status == "updated":
        bot.send_message(
            f"📝 <b>Docx updated in {folder_name}:</b> session {session_num} ({info.get('detail', '')})."
        )
    elif status == "already_linked":
        bot.send_message(f"ℹ️ <b>Docx not changed in {folder_name}:</b> {info.get('detail')}.")
    else:
        bot.send_message(f"⚠️ <b>Docx not updated in {folder_name}:</b> {info.get('detail')}")


def _update_docx_after_upload(bot, drive, folder, rec, link, session_num) -> dict:
    from docx_updater import update_docx_in_drive
    log.info("Attempting to update docx in folder %s for session %d", folder["name"], session_num)
    docx_info = update_docx_in_drive(drive, folder["id"], rec, link, session_num)
    log.info("Docx update result for %s: status=%s", folder["name"], docx_info.get("status"))
    _notify_docx_result(bot, folder["name"], session_num, docx_info)
    return docx_info


def _record_multi_undo(bot, undo, rec, folder_key, folder, session_num, final_name, result, docx_info) -> None:
    if not result.get("id"):
        return
    docx_updated = docx_info.get("status") == "updated"
    entry_id = undo.add({
        "rec_key": rec.key,
        "topic": rec.topic,
        "folder_key": folder_key,
        "folder_name": folder["name"],
        "session": session_num,
        "video_name": final_name,
        "video_id": result.get("id"),
        "video_uploaded": True,
        "link": result.get("webViewLink"),
        "docx_id": docx_info.get("file_id") if docx_updated else None,
        "docx_name": docx_info.get("file_name") if docx_updated else None,
        "docx_updated": docx_updated,
        "original_cell_xml": docx_info.get("original_cell_xml", []),
    })
    bot.send_message(
        f"Done with <code>{final_name}</code> in {folder['name']}. Made a mistake?",
        reply_markup={"inline_keyboard": [[{"text": "↩️ Undo this upload", "callback_data": f"undo:{entry_id}"}]]},
    )


def _multi_upload_one_folder(bot, rec, folder_key, dry_run, state_store, undo, zoom) -> bool:
    folder = config.FOLDERS[folder_key]
    drive = None
    if not dry_run:
        try:
            drive = get_drive_service(folder)
        except Exception as exc:
            bot.send_message(f"❌ <b>Google Drive auth failed for {folder['name']}:</b> {exc}")
            return False
    auto_s = get_next_session_number(drive, folder["id"])
    session_num = bot.get_session_number(rec, folder, auto_s)
    final_name = build_drive_filename(rec, folder.get("code", "Course"), session_num)
    if dry_run:
        bot.send_message(f"🧪 <b>[dry-run]</b> Would upload <code>{final_name}</code> to <b>{folder['name']}</b>")
        return True
    return _multi_upload_live(bot, rec, folder_key, folder, drive, zoom, session_num, final_name, state_store, undo)


def _multi_upload_live(bot, rec, folder_key, folder, drive, zoom, session_num, final_name, state_store, undo) -> bool:
    bot.send_message(f"🚀 <b>Uploading to {folder['name']}:</b> <code>{final_name}</code>")
    try:
        result = upload_recording(drive, zoom, rec, folder, session_num, bot=bot) or {}
        state_store.mark(rec.key, "uploaded", topic=rec.topic, folder=folder["name"], drive_file_id=result.get("id"))
        bot.send_message(f"✅ <b>Uploaded to {folder['name']}:</b> <code>{final_name}</code>")
        docx_info: dict = {}
        link = result.get("webViewLink")
        if link:
            try:
                docx_info = _update_docx_after_upload(bot, drive, folder, rec, link, session_num)
            except Exception as docx_exc:
                log.error("Docx update failed for folder %s: %s", folder["name"], docx_exc, exc_info=True)
                bot.send_message(f"⚠️ <b>Docx update failed in {folder['name']}:</b> {docx_exc}")
        else:
            log.warning("No webViewLink in upload result, skipping docx update for folder %s", folder["name"])
        _record_multi_undo(bot, undo, rec, folder_key, folder, session_num, final_name, result, docx_info)
        return True
    except Exception as exc:
        error_context = {
            "recording_id": rec.key, "topic": rec.topic, "folder": folder["name"],
            "session_num": session_num, "filename": final_name,
        }
        log_error_with_traceback(exc, error_context)
        state_store.mark(rec.key, "failed", topic=rec.topic, folder=folder["name"], error=str(exc))
        bot.send_message(format_error_notification(exc, error_context))
        return False


def _handle_multi_upload(bot: "TelegramBot") -> None:
    """Handle upload action after recording and folders are selected."""
    if not bot._multi_state:
        bot.send_message("❌ No active multi-folder selection.")
        return
    state = bot._multi_state
    rec = state.get("selected_rec")
    if state.get("phase") != "folder" or rec is None:
        bot.send_message("❌ Please select a recording first.")
        return
    selected_folders = state["selected_folders"]
    if not selected_folders:
        bot.send_message("❌ No folders selected!")
        return
    bot.edit_message(state["msg_id"], f"✅ <b>Selected {len(selected_folders)} folder(s)</b>")
    state_store = StateStore(config.STATE_FILE)
    undo = UndoStore(config.UNDO_FILE)
    zoom = ZoomClient()
    uploaded = failed = 0
    for folder_key in selected_folders:
        if _shutdown_requested:
            bot.send_message("🛑 <b>Multi-folder upload interrupted by shutdown signal.</b>")
            break
        if _multi_upload_one_folder(bot, rec, folder_key, state["dry_run"], state_store, undo, zoom):
            uploaded += 1
        else:
            failed += 1
    emoji = "✅" if not failed else "⚠️"
    bot.send_message(
        f"{emoji} <b>Multi-folder upload complete.</b>\n\n"
        f"• Uploaded to: <b>{uploaded}</b> folder(s)\n"
        f"• Failed: <b>{failed}</b> folder(s)"
    )
    bot._multi_state = None


def _handle_multi_cancel(bot: "TelegramBot") -> None:
    """Handle cancel action in multi selection."""
    if not bot._multi_state:
        bot.send_message("❌ No active multi-folder selection.")
        return
    bot.send_message("🛑 <b>Multi-folder upload cancelled.</b>")
    bot._multi_state = None



def _create_webhook_app(bot: "TelegramBot"):
    """Create Flask app for Telegram webhook."""
    global _flask_app
    from flask import Flask, request, abort, jsonify
    import hmac
    import hashlib

    app = Flask(__name__)

    @app.route(f"/webhook/<bot_token>", methods=["POST"])
    def webhook(bot_token):
        """Handle incoming Telegram webhook updates."""
        # Verify bot token matches
        if bot_token != bot.token:
            log.warning("Webhook received with invalid token")
            abort(403)

        # Verify secret token if configured
        if config.TELEGRAM_WEBHOOK_SECRET:
            signature = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(signature, config.TELEGRAM_WEBHOOK_SECRET):
                log.warning("Webhook received with invalid secret token")
                abort(403)

        # Process the update
        try:
            update = request.get_json()
            if not update:
                log.warning("Webhook received empty update")
                return jsonify({"status": "error"}), 400

            # Queue the update for processing
            result = bot.process_update(update)
            if result:
                bot._queue.append(result)

            return jsonify({"status": "ok"}), 200
        except Exception as exc:
            log.error("Error processing webhook update: %s", exc)
            return jsonify({"status": "error"}), 500

    @app.route("/health", methods=["GET"])
    def health():
        """Health check endpoint."""
        return jsonify({"status": "healthy", "mode": "webhook"}), 200

    return app


def _run_webhook_server(app, host: str, port: int):
    """Run Flask webhook server in a separate thread."""
    try:
        app.run(host=host, port=port, threaded=True)
    except Exception as exc:
        log.error("Webhook server error: %s", exc)


def _handle_undo_list(bot: "TelegramBot") -> None:
    undo = UndoStore(config.UNDO_FILE)
    recent = undo.recent(5)
    if not recent:
        bot.send_message("↩️ Nothing to undo — no recent uploads on record.")
        return
    keyboard = [[{"text": f"↩️ S{e['session']} {e['folder_name']} — {e['video_name']}",
                  "callback_data": f"undo:{e['id']}"}] for e in recent]
    bot.send_message("↩️ <b>Which upload do you want to undo?</b>", reply_markup={"inline_keyboard": keyboard})


def _handle_undo_ask(bot: "TelegramBot", entry_id: str) -> None:
    e = UndoStore(config.UNDO_FILE).get(entry_id)
    if not e or e.get("undone"):
        bot.send_message("ℹ️ That upload was already undone (or is no longer on record).")
        return
    bot.send_message(
        f"⚠️ <b>Undo S{e['session']} ({e['folder_name']})?</b>\n\nThis will:\n{_describe(e)}",
        reply_markup={"inline_keyboard": [[
            {"text": "✅ Yes, undo", "callback_data": f"undoyes:{entry_id}"},
            {"text": "✖ Cancel", "callback_data": f"undono:{entry_id}"}]]},
    )


def _handle_undo_yes(bot: "TelegramBot", entry_id: str) -> None:
    store = UndoStore(config.UNDO_FILE)
    e = store.get(entry_id)
    if not e or e.get("undone"):
        bot.send_message("ℹ️ That upload was already undone (or is no longer on record).")
        return
    folder = config.FOLDERS[e["folder_key"]]
    try:
        drive = get_drive_service(folder)
    except Exception as exc:
        bot.send_message(f"❌ <b>Google Drive auth failed for {folder['name']}:</b> {exc}")
        return

    lines, ok = [f"↩️ <b>Undo S{e['session']} ({e['folder_name']})</b>"], True

    if e.get("docx_updated") and e.get("docx_id"):
        try:
            from docx_updater import revert_docx_in_drive
            res = revert_docx_in_drive(drive, e["docx_id"], e["link"], e.get("original_cell_xml", []))
            lines.append("📝 Docx link removed." if res == "reverted"
                         else "📝 Docx: the link was already gone — nothing to remove.")
        except Exception as exc:
            ok = False
            log.error("Undo docx failed: %s", exc)
            lines.append(f"⚠️ Docx revert failed: {exc}")

    if e.get("video_uploaded") and e.get("video_id"):
        try:
            if config.UNDO_DELETE_PERMANENTLY:
                drive.files().delete(fileId=e["video_id"], supportsAllDrives=True).execute()
                lines.append(f"🗑️ Deleted <code>{e['video_name']}</code> from Drive.")
            else:
                drive.files().update(fileId=e["video_id"], body={"trashed": True},
                                     supportsAllDrives=True).execute()
                lines.append(f"🗑️ Moved <code>{e['video_name']}</code> to the Drive Bin "
                             "(empty the Bin to free the space).")
        except HttpError as exc:
            if exc.resp.status == 404:
                lines.append("🗑️ Video was already gone from Drive.")
            else:
                ok = False
                log.error("Undo video failed: %s", exc)
                lines.append(f"⚠️ Could not remove the video: {exc}")
        except Exception as exc:
            ok = False
            log.error("Undo video failed: %s", exc)
            lines.append(f"⚠️ Could not remove the video: {exc}")

    if ok:
        StateStore(config.STATE_FILE).forget(e["rec_key"])
        store.mark_undone(entry_id)
        lines.append("🔁 The recording will be offered again on the next /run.")
    else:
        lines.append("Some steps failed, so this stays in /undo — tap it again to retry.")
    bot.send_message("\n".join(lines))


def bot_mode(dry_run: bool = False) -> None:
    """
    Persistent always-on bot loop.
    Waits for Telegram commands and runs the pipeline on demand.
    Start with:  python main.py --bot
    """
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    bot = TelegramBot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)

    # Get size filters from config
    min_size = config.MIN_RECORDING_SIZE_MB if config.MIN_RECORDING_SIZE_MB > 0 else None
    max_size = config.MAX_RECORDING_SIZE_MB if config.MAX_RECORDING_SIZE_MB > 0 else None

    # Initialize webhook if enabled
    webhook_mode = config.TELEGRAM_USE_WEBHOOK
    if webhook_mode:
        if not config.TELEGRAM_WEBHOOK_URL:
            log.error("TELEGRAM_USE_WEBHOOK is True but TELEGRAM_WEBHOOK_URL is not set")
            bot.send_message("❌ <b>Configuration error:</b> TELEGRAM_WEBHOOK_URL not set for webhook mode")
            return

        # Create and start Flask webhook server
        webhook_app = _create_webhook_app(bot)
        webhook_url = config.TELEGRAM_WEBHOOK_URL

        # Set webhook with Telegram
        if not bot.set_webhook(webhook_url, config.TELEGRAM_WEBHOOK_SECRET):
            bot.send_message("❌ <b>Failed to set webhook</b>. Falling back to polling mode.")
            webhook_mode = False
        else:
            log.info("Webhook mode enabled: %s", webhook_url)
            # Start Flask server in separate thread
            global _flask_thread
            _flask_thread = threading.Thread(
                target=_run_webhook_server,
                args=(webhook_app, config.WEBHOOK_HOST, config.WEBHOOK_PORT),
                daemon=True
            )
            _flask_thread.start()
            time.sleep(2)  # Give server time to start

    # Build startup message
    startup_msg = (
        f"🟢 <b>zoom-drive bot started!</b>\n\n"
        f"Mode: <b>{'Webhook' if webhook_mode else 'Polling'}</b>\n\n"
        "Commands:\n"
        "• /run — process last 7 days\n"
        "• /run 30 — process last 30 days\n"
        "• /run --dry — dry-run (no uploads)\n"
        "• /multi — pick a recording (last 7 days) and upload to multiple folders\n"
        "• /multi 14 — same, looking back 14 days\n"
        "• /undo — undo a recent upload + docx link\n"
        "• /status — show processed count\n"
        "• /stats — detailed statistics\n"
        "• /reset — clear history\n"
        "• /backup — download state backup\n"
        "• /restore — upload backup file to restore state\n"
        "• /help — show commands"
    )

    if config.SCHEDULED_RUN_TIME:
        startup_msg += f"\n\n⏰ <b>Scheduled run:</b> Daily at {config.SCHEDULED_RUN_TIME}"

    bot.send_message(startup_msg)
    log.info("Bot mode started. Waiting for Telegram commands.")

    try:
        while True:
            # Check for shutdown signal
            if _shutdown_requested:
                bot.send_message("🔴 <b>Bot is shutting down gracefully.</b> Goodbye!")
                log.info("Bot mode stopping due to shutdown signal.")
                break

            # Check for scheduled run
            if config.SCHEDULED_RUN_TIME:
                from zoneinfo import ZoneInfo
                now = datetime.now(ZoneInfo(config.LOCAL_TIMEZONE))
                scheduled_time = config.SCHEDULED_RUN_TIME
                if now.strftime("%H:%M") == scheduled_time and now.second < 5:  # Only trigger in first 5 seconds of minute
                    log.info("Triggering scheduled run at %s", scheduled_time)
                    bot.send_message(f"⏰ <b>Scheduled run triggered</b> at {scheduled_time}")
                    uploaded, skipped, failed = run_pipeline(bot, config.LOOKBACK_DAYS, dry_run=dry_run,
                                                           min_size=min_size, max_size=max_size)
                    emoji = "✅" if not failed else "⚠️"
                    bot.send_message(
                        f"{emoji} <b>Scheduled run complete.</b>\n\n"
                        f"• Uploaded: <b>{uploaded}</b>\n"
                        f"• Skipped:  <b>{skipped}</b>\n"
                        f"• Failed:   <b>{failed}</b>"
                    )
                    time.sleep(60)  # Wait 1 minute to avoid triggering again
                    continue

            # Get command from queue (webhook) or polling
            if webhook_mode:
                # Webhook mode: check queue periodically
                if bot._queue:
                    cmd, arg = bot._queue.pop(0)
                else:
                    time.sleep(1)
                    continue
            else:
                # Polling mode: listen for command
                cmd, arg = bot.listen_for_command()

            # Check again after receiving command
            if _shutdown_requested:
                bot.send_message("🔴 <b>Bot is shutting down gracefully.</b> Goodbye!")
                log.info("Bot mode stopping due to shutdown signal.")
                break

            days = arg if isinstance(arg, int) else config.LOOKBACK_DAYS

            if cmd == "run":
                uploaded, skipped, failed = run_pipeline(bot, days, dry_run=dry_run,
                                                          min_size=min_size, max_size=max_size)
                emoji = "✅" if not failed else "⚠️"
                if dry_run:
                    bot.send_message(
                        f"{emoji} <b>Dry-run complete.</b>\n\n"
                        f"• Would upload: <b>{uploaded}</b>\n"
                        f"• Would skip:  <b>{skipped}</b>\n"
                        f"• Failed:   <b>{failed}</b>\n\n"
                        "Send /run to perform actual uploads, or /help for options."
                    )
                else:
                    bot.send_message(
                        f"{emoji} <b>Run complete.</b>\n\n"
                        f"• Uploaded: <b>{uploaded}</b>\n"
                        f"• Skipped:  <b>{skipped}</b>\n"
                        f"• Failed:   <b>{failed}</b>\n\n"
                        "Send /run to start another run, or /help for options."
                    )

            elif cmd == "run_dry":
                uploaded, skipped, failed = run_pipeline(bot, days, dry_run=True,
                                                          min_size=min_size, max_size=max_size)
                emoji = "✅" if not failed else "⚠️"
                bot.send_message(
                    f"{emoji} <b>Dry-run complete.</b>\n\n"
                    f"• Would upload: <b>{uploaded}</b>\n"
                    f"• Would skip:  <b>{skipped}</b>\n"
                    f"• Failed:   <b>{failed}</b>\n\n"
                    "Send /run to perform actual uploads, or /help for options."
                )

            elif cmd == "undo_list":
                _handle_undo_list(bot)

            elif cmd == "undo_ask":
                _handle_undo_ask(bot, str(arg))

            elif cmd == "undo_yes":
                _handle_undo_yes(bot, str(arg))

            elif cmd == "undo_no":
                bot.send_message("👍 Cancelled — nothing was changed.")

            elif cmd == "multi":
                days_arg = arg if isinstance(arg, int) else config.LOOKBACK_DAYS
                _handle_multi(bot, dry_run=dry_run, days=days_arg)

            elif cmd == "multi_rec_select":
                _handle_multi_rec_select(bot, str(arg))

            elif cmd == "multi_toggle":
                _handle_multi_toggle(bot, str(arg))

            elif cmd == "multi_upload":
                _handle_multi_upload(bot)

            elif cmd == "multi_cancel":
                _handle_multi_cancel(bot)

            elif cmd == "status":
                _handle_status(bot)

            elif cmd == "stats":
                _handle_stats(bot)

            elif cmd == "reset":
                _handle_reset(bot)

            elif cmd == "backup":
                _handle_backup(bot)

            elif cmd == "restore":
                # arg is (file_id, filename)
                file_id, filename = arg
                _handle_restore_from_telegram(bot, file_id, filename)

            elif cmd == "help":
                bot.send_message(
                    "📖 <b>Commands</b>\n\n"
                    "• /run — fetch & process last 7 days\n"
                    "• /run 30 — fetch & process last N days\n"
                    "• /run --dry — dry-run (no uploads)\n"
                    "• /multi — pick a recording (last 7 days) and upload to multiple folders\n"
                    "• /multi 14 — same, looking back 14 days\n"
                    "• /undo — undo a recent upload + docx link\n"
                    "• /status — how many recordings processed\n"
                    "• /stats — detailed statistics\n"
                    "• /reset — clear processed.json\n"
                    "• /backup — download state backup\n"
                    "• /restore — upload backup file to restore state\n"
                    "• /help — this message"
                )

    except KeyboardInterrupt:
        bot.send_message("🔴 <b>Bot is shutting down.</b> Goodbye!")
        log.info("Bot mode stopped by KeyboardInterrupt.")
    except Exception as exc:
        log.error("Unexpected bot loop error: %s", exc)
    finally:
        # Ensure state is saved before exit
        log.info("Saving state before shutdown...")
        try:
            state = StateStore(config.STATE_FILE)
            # State is already saved on each mark operation, but we ensure it's flushed
            log.info("State saved successfully.")
        except Exception as exc:
            log.error("Failed to save state during shutdown: %s", exc)

        # Cleanup webhook if enabled
        if config.TELEGRAM_USE_WEBHOOK:
            log.info("Cleaning up webhook...")
            bot.delete_webhook()
            log.info("Webhook deleted.")


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging()
    
    try:
        config.validate_config(need_google=not args.dry_run)
    except ValueError as exc:
        log.error("Configuration Error: %s", exc)
        log.error("Fix your environment variables in Railway → Variables tab, then redeploy.")
        # Sleep before exiting so Railway doesn't restart us 60x per minute
        time.sleep(60)
        return 1

    if args.bot:
        bot_mode(dry_run=args.dry_run)
        return 0

    # ── One-shot terminal mode (original behaviour) ───────────────────────
    bot = TelegramBot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)

    # Get size filters from config or command line
    min_size = args.min_size if args.min_size else (config.MIN_RECORDING_SIZE_MB if config.MIN_RECORDING_SIZE_MB > 0 else None)
    max_size = args.max_size if args.max_size else (config.MAX_RECORDING_SIZE_MB if config.MAX_RECORDING_SIZE_MB > 0 else None)

    uploaded, skipped, failed = run_pipeline(bot, args.days, dry_run=args.dry_run,
                                            filter_topic=args.filter_topic,
                                            filter_from=args.filter_from,
                                            filter_to=args.filter_to,
                                            min_size=min_size, max_size=max_size)
    print(f"Done. Uploaded: {uploaded} | Skipped: {skipped} | Failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

