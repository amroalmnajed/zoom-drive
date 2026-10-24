#!/usr/bin/env python3
"""
multi_upload.py - Multi-folder recording selection/upload workflow.

Interactive multi-recording selection and multi-folder upload workflow via Telegram inline buttons.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import config
import requests
from models import Recording
from state import StateStore
from undo_store import UndoStore
from zoom_api import ZoomClient
from drive_api import get_drive_service, get_next_session_number, upload_recording
from telegram_bot import TelegramBot
from error_handler import format_error_notification, log_error_with_traceback
from utils import build_drive_filename, human_duration, human_size

log = logging.getLogger("zoom_to_drive")

MULTI_REC_DISPLAY_LIMIT = 25

_shutdown_requested = False


def set_shutdown_requested(value: bool) -> None:
    """Set shutdown request flag for graceful interruption."""
    global _shutdown_requested
    _shutdown_requested = value


def _fetch_sorted_recordings(days: int) -> list:
    """Fetch and sort recordings from Zoom by start time (newest first)."""
    zoom = ZoomClient()
    recordings = list(zoom.iter_recordings(days, fetch_participants=config.FETCH_PARTICIPANTS))
    recordings.sort(
        key=lambda r: r.start_time or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    log.info("Fetched %d recordings from Zoom (last %d day(s))", len(recordings), days)
    return recordings


def _rec_start_str(rec) -> str:
    """Format recording start time as string."""
    start = rec.local_start()
    return f"{start:%Y-%m-%d %H:%M}" if start else "unknown"


def _multi_recording_keyboard(recordings: list) -> dict:
    """Build inline keyboard for recording selection."""
    keyboard = []
    for idx, rec in enumerate(recordings):
        label = f"{idx + 1}) {_rec_start_str(rec)} {rec.topic}"[:64]
        keyboard.append([{"text": label, "callback_data": f"multi_rec:{idx}"}])
    keyboard.append([{"text": "🛑 Cancel", "callback_data": "multi_action:cancel"}])
    return {"inline_keyboard": keyboard}


def _multi_folder_keyboard(selected_folders: set) -> dict:
    """Build inline keyboard for folder selection with checkboxes."""
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
    """Notify user of DOCX update result."""
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
    """Update DOCX file after upload and notify user."""
    from docx_updater import update_docx_in_drive
    log.info("Attempting to update docx in folder %s for session %d", folder["name"], session_num)
    docx_info = update_docx_in_drive(drive, folder["id"], rec, link, session_num)
    log.info("Docx update result for %s: status=%s", folder["name"], docx_info.get("status"))
    _notify_docx_result(bot, folder["name"], session_num, docx_info)
    return docx_info


def _record_multi_undo(bot, undo, rec, folder_key, folder, session_num, final_name, result, docx_info) -> None:
    """Record undo entry for multi-folder upload."""
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
    """Upload recording to a single folder in multi-folder workflow."""
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
    """Perform live upload for multi-folder workflow."""
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
