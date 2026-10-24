#!/usr/bin/env python3
"""
undo_admin.py - Undo and state administration workflow.

Undo operations (revert DOCX links, trash Drive videos, forget processed recordings)
and state administration (status, stats, reset, backup, restore).
"""
from __future__ import annotations

import json
import logging
import zipfile
import io
from datetime import datetime, timezone

import config
from drive_api import get_drive_service
from googleapiclient.errors import HttpError
from state import StateStore
from undo_store import UndoStore
from telegram_bot import TelegramBot

log = logging.getLogger("zoom_to_drive")


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
    skipped = sum(1 for v in data.values() if v.get("status") == "skipped")
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
    """Describe what an undo operation will do."""
    parts = []
    if e.get("docx_updated"):
        parts.append(f"remove the link from <b>{e.get('docx_name')}</b>")
    if e.get("video_uploaded"):
        how = "permanently delete" if config.UNDO_DELETE_PERMANENTLY else "move to the Drive Bin"
        parts.append(f"{how} <code>{e['video_name']}</code>")
    parts.append("offer this recording again on the next /run")
    return "\n".join(f"• {p}" for p in parts)


def _handle_undo_list(bot: "TelegramBot") -> None:
    """List recent uploads available for undo."""
    undo = UndoStore(config.UNDO_FILE)
    recent = undo.recent(5)
    if not recent:
        bot.send_message("↩️ Nothing to undo — no recent uploads on record.")
        return
    keyboard = [[{"text": f"↩️ S{e['session']} {e['folder_name']} — {e['video_name']}",
                  "callback_data": f"undo:{e['id']}"}] for e in recent]
    bot.send_message("↩️ <b>Which upload do you want to undo?</b>", reply_markup={"inline_keyboard": keyboard})


def _handle_undo_ask(bot: "TelegramBot", entry_id: str) -> None:
    """Ask user to confirm undo operation."""
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
    """Perform the undo operation after confirmation."""
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
