#!/usr/bin/env python3
"""
bot_runtime.py - Telegram bot runtime, command dispatch, and CLI entrypoint.

Telegram bot runtime (polling or webhook mode), command dispatch (/run, /multi, /undo, /status, /stats, /reset, /backup, /restore, /help), scheduled runs, graceful shutdown, and CLI entrypoint.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
import threading
from datetime import datetime, timezone
from typing import Optional

import config
from telegram_bot import TelegramBot
from state import StateStore
from utils import setup_logging
from standard_upload import run_pipeline
from multi_upload import (
    _handle_multi, _handle_multi_rec_select, _handle_multi_toggle,
    _handle_multi_upload, _handle_multi_cancel, set_shutdown_requested
)
from undo_admin import (
    _handle_undo_list, _handle_undo_ask, _handle_undo_yes,
    _handle_status, _handle_stats, _handle_reset,
    _handle_backup, _handle_restore_from_telegram
)

log = logging.getLogger("zoom_to_drive")

# Global flag for graceful shutdown
_shutdown_requested = False
_webhook_app = None
_flask_thread = None


def _request_shutdown(signum=None, frame=None) -> None:
    """Signal handler for graceful shutdown."""
    global _shutdown_requested
    _shutdown_requested = True
    set_shutdown_requested(True)
    log.info("Shutdown signal received (%s), gracefully stopping...", signum)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
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


def _run_health_server(host: str, port: int):
    """Run Flask health check server in a separate thread for cloud deployments (e.g. Render/Railway)."""
    try:
        from health_check import app as health_app
        import logging as _logging
        _logging.getLogger("werkzeug").setLevel(_logging.WARNING)
        health_app.run(host=host, port=port, threaded=True)
    except Exception as exc:
        log.error("Health check server error: %s", exc)


def _run_webhook_server(app, host: str, port: int):
    """Run Flask webhook server in a separate thread."""
    try:
        app.run(host=host, port=port, threaded=True)
    except Exception as exc:
        log.error("Webhook server error: %s", exc)


def bot_mode(dry_run: bool = False) -> None:
    """
    Persistent always-on bot loop.
    Waits for Telegram commands and runs the pipeline on demand.
    Start with:  python bot_runtime.py --bot
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
    else:
        # Polling mode: if PORT or ENABLE_HEALTH_CHECK is set (e.g., on Render or Railway Web Service),
        # start the health check HTTP server in a background thread to satisfy port binding requirements
        http_port = os.getenv("PORT") or (os.getenv("HEALTH_PORT") if os.getenv("ENABLE_HEALTH_CHECK", "false").lower() == "true" else None)
        if http_port:
            try:
                port_num = int(http_port)
                log.info("Starting background health check server on port %d", port_num)
                health_thread = threading.Thread(
                    target=_run_health_server,
                    args=("0.0.0.0", port_num),
                    daemon=True
                )
                health_thread.start()
            except ValueError:
                log.warning("Invalid HTTP port for health check: %s", http_port)

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
                                                           min_size=min_size, max_size=max_size,
                                                           shutdown_requested=_shutdown_requested)
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
                                                          min_size=min_size, max_size=max_size,
                                                          shutdown_requested=_shutdown_requested)
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
                                                          min_size=min_size, max_size=max_size,
                                                          shutdown_requested=_shutdown_requested)
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
    """CLI entrypoint."""
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
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    bot = TelegramBot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)

    # Get size filters from config or command line
    min_size = args.min_size if args.min_size else (config.MIN_RECORDING_SIZE_MB if config.MIN_RECORDING_SIZE_MB > 0 else None)
    max_size = args.max_size if args.max_size else (config.MAX_RECORDING_SIZE_MB if config.MAX_RECORDING_SIZE_MB > 0 else None)

    uploaded, skipped, failed = run_pipeline(bot, args.days, dry_run=args.dry_run,
                                            filter_topic=args.filter_topic,
                                            filter_from=args.filter_from,
                                            filter_to=args.filter_to,
                                            min_size=min_size, max_size=max_size,
                                            shutdown_requested=_shutdown_requested)
    print(f"Done. Uploaded: {uploaded} | Skipped: {skipped} | Failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
