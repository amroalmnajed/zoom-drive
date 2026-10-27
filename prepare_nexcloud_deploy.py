#!/usr/bin/env python3
"""
Script to create a deployment package for NexCloud hosting.
This packages all necessary files for the Zoom-to-Google-Drive Telegram bot.
"""

import os
import zipfile
from pathlib import Path

def create_nexcloud_deploy():
    """Create a zip file with all necessary files for NexCloud deployment."""

    # Files to include in the deployment package
    required_files = [
        "bot_runtime.py",
        "telegram_bot.py",
        "standard_upload.py",
        "multi_upload.py",
        "undo_admin.py",
        "undo_store.py",
        "state.py",
        "zoom_api.py",
        "drive_api.py",
        "docx_updater.py",
        "config.py",
        "auth_all.py",
        "nexcloud_start.py",
        "models.py",
        "utils.py",
        "error_handler.py",
        "health_check.py",
        "requirements.txt",
        "env.example",
    ]

    # Create the zip file
    zip_path = Path("nexcloud_deploy.zip")
    project_dir = Path.cwd()

    print(f"Creating deployment package at: {zip_path.absolute()}")

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file_name in required_files:
            file_path = project_dir / file_name
            if file_path.exists():
                print(f"Adding: {file_name}")
                zipf.write(file_path, file_name)
            else:
                print(f"Warning: {file_name} not found, skipping")

    print(f"\nDeployment package created: {zip_path.absolute()}")
    print(f"Size: {zip_path.stat().st_size / 1024:.2f} KB")

if __name__ == "__main__":
    create_nexcloud_deploy()
