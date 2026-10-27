#!/usr/bin/env python3
"""
Botkeep startup entry point for the Zoom-to-Google-Drive Telegram bot.
This script is used as the main entry point when deploying to Botkeep.
"""

import sys
import os

# Add the current directory to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import and run the bot runtime
from bot_runtime import main

if __name__ == "__main__":
    # Start the bot in bot mode
    main(["--bot"])
