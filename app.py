import os
import threading
import logging
import gradio as gr
import bot_runtime
import config

log = logging.getLogger("zoom_to_drive")

def _start_bot_background():
    """Run the Telegram bot loop in a background daemon thread."""
    try:
        log.info("Starting Telegram bot from Hugging Face app.py...")
        bot_runtime.bot_mode()
    except Exception as exc:
        log.error("Fatal error in bot background thread: %s", exc)

# Start the bot as a background thread as soon as the app starts
bot_thread = threading.Thread(target=_start_bot_background, daemon=True)
bot_thread.start()

# Build lightweight Gradio interface for Hugging Face
with gr.Blocks(title="Zoom to Drive Bot") as demo:
    gr.Markdown("# 🟢 Zoom-to-Drive Automation Bot is Running")
    gr.Markdown(
        "The bot is active in the background and processing Zoom cloud recordings "
        "directly into Google Drive using Hugging Face's high-speed cloud connection."
    )
    gr.Markdown("### How to control it:")
    gr.Markdown(
        "- Open your Telegram chat with the bot.\n"
        "- Send `/run` to process recordings from the last 7 days.\n"
        "- Send `/status` or `/help` to see available commands."
    )

if __name__ == "__main__":
    port = int(os.getenv("PORT", "7860"))
    demo.launch(server_name="0.0.0.0", server_port=port)
