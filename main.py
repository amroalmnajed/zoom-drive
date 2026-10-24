#!/usr/bin/env python3
"""
main.py - Entry point for Railway deployment.
Runs the Telegram bot in polling mode.
"""
import sys
from bot_runtime import main

if __name__ == "__main__":
    sys.exit(main(["--bot"]))
