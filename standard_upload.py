#!/usr/bin/env python3
"""
standard_upload.py - Standard Zoom recording approval/upload workflow.

Fetches Zoom recordings, presents each via Telegram for approval/categorization,
streams approved videos to Drive, updates DOCX documents, and records state/undo history.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional, Tuple

import config
from models import Recording
from state import StateStore
from undo_store import UndoStore
from zoom_api import ZoomClient
from drive_api import get_drive_service, get_next_session_number, upload_recording
from telegram_bot import TelegramBot
from error_handler import format_error_notification, log_error_with_traceback

log = logging.getLogger("zoom_to_drive")


def _build_rules() -> list[tuple[str, str, re.Pattern]]:
    """Build keyword matching rules for folder categorization."""
    rules: list[tuple[str, str, re.Pattern]] = []
    seen: set[tuple[str, str]] = set()
    for key, folder in config.FOLDERS.items():
        for kw in folder["keywords"]:
            kw_clean = kw.strip()
            if not kw_clean or (key, kw_clean.lower()) in seen:
                continue
            seen.add((key, kw_clean.lower()))
            left = r"(?<![A-Za-z0-9])" if kw_clean[0].isalnum() else ""
            right = r"(?![A-Za-z0-9])" if kw_clean[-1].isalnum() else ""
            pattern = re.compile(left + re.escape(kw_clean) + right, re.IGNORECASE)
            rules.append((key, kw_clean, pattern))
    rules.sort(key=lambda r: -len(r[1]))
    return rules


_RULES = _build_rules()


def categorize(title: str) -> tuple[str, Optional[str]]:
    """Categorize a recording title to a folder key based on keyword matching.

    Args:
        title: Recording topic/title to categorize

    Returns:
        Tuple of (folder_key, matched_keyword) or (DEFAULT_FOLDER_KEY, None)
    """
    for key, kw, pattern in _RULES:
        if pattern.search(title):
            return key, kw
    return config.DEFAULT_FOLDER_KEY, None


def extract_video_metadata(video_url: str) -> tuple[Optional[str], Optional[str]]:
    """Extract video resolution and bitrate using FFmpeg.

    Args:
        video_url: URL of the video file

    Returns:
        Tuple of (resolution, bitrate) or (None, None) if extraction fails
    """
    try:
        import subprocess
        import json

        # Use ffprobe to get video metadata without downloading the entire file
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,bit_rate",
            "-of", "json",
            video_url
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.debug("FFprobe failed: %s", result.stderr)
            return None, None

        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            return None, None

        stream = streams[0]
        width = stream.get("width")
        height = stream.get("height")
        bit_rate = stream.get("bit_rate")

        resolution = f"{width}x{height}" if width and height else None
        bitrate = f"{int(bit_rate) / 1000:.0f} kbps" if bit_rate else None

        return resolution, bitrate
    except FileNotFoundError:
        log.debug("FFprobe not found, skipping video metadata extraction")
        return None, None
    except Exception as exc:
        log.debug("Failed to extract video metadata: %s", exc)
        return None, None


def generate_thumbnail(video_url: str, time_offset: float = 2.0) -> Optional[bytes]:
    """Generate a thumbnail from video using FFmpeg.

    Args:
        video_url: URL of the video file
        time_offset: Time offset in seconds to capture frame (default: 2.0)

    Returns:
        Thumbnail image as bytes, or None if generation fails
    """
    try:
        import subprocess

        # Use ffmpeg to extract a single frame at the specified time offset
        cmd = [
            "ffmpeg",
            "-ss", str(time_offset),
            "-i", video_url,
            "-vframes", "1",
            "-q:v", "2",
            "-vf", "scale=320:240",
            "-f", "image2pipe",
            "-"
        ]

        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            log.debug("FFmpeg thumbnail generation failed: %s", result.stderr.decode())
            return None

        return result.stdout
    except FileNotFoundError:
        log.debug("FFmpeg not found, skipping thumbnail generation")
        return None
    except Exception as exc:
        log.debug("Failed to generate thumbnail: %s", exc)
        return None


def run_pipeline(bot: "TelegramBot", days: int, dry_run: bool = False,
                 filter_topic: Optional[str] = None, filter_from: Optional[str] = None,
                 filter_to: Optional[str] = None, min_size: Optional[int] = None,
                 max_size: Optional[int] = None, shutdown_requested: bool = False) -> tuple[int, int, int]:
    """
    Fetch Zoom recordings, present each via Telegram for approval, upload approved ones.
    Returns (uploaded, skipped, failed).
    """
    from utils import build_drive_filename
    from models import ApprovalRequest

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
            if shutdown_requested:
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
                result = upload_recording(drive, zoom, rec, folder, session_num, bot=bot,
                                         shutdown_requested=shutdown_requested)
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
