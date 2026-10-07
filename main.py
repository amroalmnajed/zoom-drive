#!/usr/bin/env python3
"""
main.py - Fetch Zoom cloud recordings, ask for confirmation via Telegram bot inline buttons,
confirm session number, then stream the video directly from Zoom into Google Drive.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httplib2
httplib2.REDIRECT_CODES = {301, 302, 303, 307}

import config
from models import ZoomError, ApprovalRequest
from utils import setup_logging, categorize, build_drive_filename
from state import StateStore
from zoom_api import ZoomClient
from drive_api import get_drive_service, get_next_session_number, upload_recording
from telegram_bot import TelegramBot

log = logging.getLogger("zoom_to_drive")

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Zoom cloud recordings -> Google Drive (via Telegram approval).")
    parser.add_argument("--days", type=int, default=config.LOOKBACK_DAYS, help="lookback window in days")
    parser.add_argument("--dry-run", action="store_true", help="fetch and prompt only; do not upload")
    parser.add_argument(
        "--bot", action="store_true",
        help="run as a persistent Telegram bot — trigger runs with /run [N] from chat"
    )
    return parser.parse_args(argv)


def run_pipeline(bot: "TelegramBot", days: int, dry_run: bool = False) -> tuple[int, int, int]:
    """
    Fetch Zoom recordings, present each via Telegram for approval, upload approved ones.
    Returns (uploaded, skipped, failed).
    """
    state = StateStore(config.STATE_FILE)
    zoom = ZoomClient()

    bot.send_message(f"🔍 <b>Fetching Zoom recordings</b> from the last <b>{days} day(s)</b>...")
    try:
        recordings = sorted(
            zoom.iter_recordings(days),
            key=lambda r: r.start_time or datetime.min.replace(tzinfo=timezone.utc),
        )
    except ZoomError as exc:
        bot.send_message(f"❌ <b>Zoom error:</b> {exc}")
        return 0, 0, 0

    pending = [r for r in recordings if not state.should_hide(r.key)]

    if not pending:
        bot.send_message(
            f"✅ <b>Nothing to do.</b>\n"
            f"Found {len(recordings)} recording(s) — all already processed."
        )
        return 0, 0, 0

    bot.send_message(
        f"📋 Found <b>{len(recordings)}</b> recording(s), "
        f"<b>{len(pending)}</b> pending review."
    )

    uploaded = skipped = failed = 0
    try:
        for i, rec in enumerate(pending, start=1):
            suggested_key, matched = categorize(rec.topic)
            req = ApprovalRequest(rec, i, len(pending), suggested_key, matched)
            decision = bot.get_user_decision(req)

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
                state.mark(rec.key, "uploaded", topic=rec.topic, folder=folder["name"],
                           drive_file_id=(result or {}).get("id"))
                uploaded += 1
                bot.send_message(f"✅ <b>Uploaded successfully:</b> <code>{final_name}</code>")

                try:
                    from docx_updater import update_docx_in_drive
                    link = (result or {}).get("webViewLink") if result else None
                    if link:
                        update_docx_in_drive(drive, folder["id"], rec, link, session_num)
                        bot.send_message(f"📝 <b>Docx updated</b> with link for session {session_num}")
                except Exception as docx_exc:
                    log.error("Docx update failed: %s", docx_exc)
                    bot.send_message(f"⚠️ <b>Docx update failed:</b> {docx_exc}")
            except Exception as exc:
                failed += 1
                log.error("Upload failed for %s: %s", final_name, exc)
                state.mark(rec.key, "failed", topic=rec.topic, folder=folder["name"], error=str(exc))

                # ── Feature 2: Rich error alert ──────────────────────────
                err_str = str(exc)
                if "Zoom download" in err_str or "Zoom stream" in err_str:
                    source = "📥 <b>Source:</b> Zoom download failed"
                elif "Drive" in err_str or "resumable" in err_str.lower():
                    source = "☁️ <b>Source:</b> Google Drive upload failed"
                else:
                    source = "⚙️ <b>Source:</b> Unexpected error"

                now_str = datetime.now(ZoneInfo(config.LOCAL_TIMEZONE)).strftime("%Y-%m-%d %H:%M")
                alert = (
                    f"❌ <b>Upload Failed!</b>\n\n"
                    f"📄 <b>File:</b> <code>{final_name}</code>\n"
                    f"📁 <b>Folder:</b> {folder['name']}\n"
                    f"🕐 <b>Time:</b> {now_str}\n"
                    f"{source}\n\n"
                    f"<b>Error:</b> <code>{err_str[:300]}</code>\n\n"
                    f"ℹ️ Recording marked as <i>failed</i> — send /reset then /run to retry it."
                )
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


def _handle_reset(bot: "TelegramBot") -> None:
    """Clear processed.json after confirmation."""
    state_path = config.STATE_FILE
    if state_path.exists():
        state_path.unlink()
        bot.send_message("🗑️ <b>Reset complete.</b> processed.json cleared — all recordings will be offered again.")
    else:
        bot.send_message("ℹ️ Nothing to reset — processed.json did not exist.")


def bot_mode(dry_run: bool = False) -> None:
    """
    Persistent always-on bot loop.
    Waits for Telegram commands and runs the pipeline on demand.
    Start with:  python main.py --bot
    """
    bot = TelegramBot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
    bot.send_message(
        "🟢 <b>zoom-drive bot started!</b>\n\n"
        "Commands:\n"
        "• /run — process last 7 days\n"
        "• /run 30 — process last 30 days\n"
        "• /status — show processed count\n"
        "• /reset — clear history\n"
        "• /help — show commands"
    )
    log.info("Bot mode started. Waiting for Telegram commands.")

    while True:
        try:
            cmd, days = bot.listen_for_command()

            if cmd == "run":
                uploaded, skipped, failed = run_pipeline(bot, days, dry_run=dry_run)
                emoji = "✅" if not failed else "⚠️"
                bot.send_message(
                    f"{emoji} <b>Run complete.</b>\n\n"
                    f"• Uploaded: <b>{uploaded}</b>\n"
                    f"• Skipped:  <b>{skipped}</b>\n"
                    f"• Failed:   <b>{failed}</b>\n\n"
                    "Send /run to start another run, or /help for options."
                )

            elif cmd == "status":
                _handle_status(bot)

            elif cmd == "reset":
                _handle_reset(bot)

            elif cmd == "help":
                bot.send_message(
                    "📖 <b>Commands</b>\n\n"
                    "• /run — fetch & process last 7 days\n"
                    "• /run 30 — fetch & process last N days\n"
                    "• /status — how many recordings processed\n"
                    "• /reset — clear processed.json\n"
                    "• /help — this message"
                )

        except KeyboardInterrupt:
            bot.send_message("🔴 <b>Bot is shutting down.</b> Goodbye!")
            log.info("Bot mode stopped by KeyboardInterrupt.")
            break
        except Exception as exc:
            log.error("Unexpected bot loop error: %s", exc)
            time.sleep(5)


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
    uploaded, skipped, failed = run_pipeline(bot, args.days, dry_run=args.dry_run)
    print(f"Done. Uploaded: {uploaded} | Skipped: {skipped} | Failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

