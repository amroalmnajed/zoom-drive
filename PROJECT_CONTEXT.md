# Project Context

## Project Overview & Tech Stack
**Purpose**: An automated pipeline that fetches Zoom cloud recordings, routes them through a Telegram approval bot, streams the approved videos directly to Google Drive (bypassing local disk storage), and updates specific `.docx` tracking files with the uploaded video links.

**Tech Stack**:
- **Language**: Python 3.9+
- **Core Frameworks**: `google-api-python-client` (Drive API), `python-docx` (Word document manipulation), `requests` (streaming transfers).
- **Environment**: Deployed continuously on Railway (via GitHub), communicating primarily over external APIs.

## Directory & Component Map
- `bot_runtime.py` (Entry Point): Bot runtime, command dispatch, and CLI entrypoint. Orchestrates persistent bot loop and one-shot mode.
- `standard_upload.py`: Standard Zoom recording approval/upload workflow (fetch, approve, stream, update docs).
- `multi_upload.py`: Multi-folder recording selection/upload workflow (interactive Telegram inline buttons).
- `undo_admin.py`: Undo operations (revert DOCX links, trash Drive videos) and state administration (status, stats, reset, backup, restore).
- `config.py`: Centralizes environment variables, tuning parameters, and Google Drive folder configurations (`FOLDERS`).
- `zoom_api.py`: Handles Zoom Server-to-Server (S2S) OAuth and retrieves recording metadata/download URLs.
- `drive_api.py`: Manages Google Drive OAuth and performs the core chunked Resumable Upload (streaming directly from Zoom to Google Drive).
- `docx_updater.py`: Locates `.docx` files in Drive folders, reads tables, matches the `Session #` column, inserts an external hyperlink to the new video, and re-uploads the modified document.
- `telegram_bot.py`: Implements Telegram long-polling and interactive inline keyboards for manual user categorization/approval.
- `state.py`: Manages local persistence (`processed.json`) to prevent duplicate processing.
- `models.py`: Data structures (`Recording`, `ApprovalRequest`).
- `auth_all.py`: Utility script for authenticating the 4 different Google accounts locally to generate `token_*.json` files.

## Data Flow & Architecture
1. **Fetch**: `standard_upload.py` queries `zoom_api.py` for recent recordings.
2. **Filter**: Skips recordings already marked in `processed.json` (managed by `state.py`).
3. **Approve**: Sends interactive messages to Telegram (`telegram_bot.py`). Waits for user decision.
4. **Stream**: If approved, `drive_api.py` requests a video stream from Zoom and `PUT`s it directly to Google Drive in chunks (16MB default).
5. **Update Tracking**: On successful upload, `docx_updater.py` modifies the `.docx` file in the target Drive folder, matching the `Session #` row and injecting the `Link`.

## Development Conventions
- **No Local Storage**: Do not buffer full videos to disk. Use chunked streaming (`ZoomStreamMedia` / `requests`).
- **Multi-Account**: Different classes upload to different Google accounts. Always pass `folder` context to `get_drive_service()` and `_load_google_credentials()`.
- **Security**: OAuth tokens (`token_*.json`) are generated locally and excluded from git. Cloud deployments (Railway) require raw JSON strings pasted into environment variables.
- **Fail-Safes**: Resumable upload implements backoff/retries for HTTP 429 and 5xx errors.

## Environment & Configuration
Requires `.env` (locally) or Railway Dashboard variables:
- **Zoom**: `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`, `ZOOM_USER_ID`.
- **Telegram**: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- **Google Drive**: JSON credential strings for specific class accounts (`GOOGLE_TOKEN_MAIN_JSON`, `GOOGLE_TOKEN_CRASH_JSON`, `GOOGLE_TOKEN_G10_JSON`, `GOOGLE_TOKEN_G9_JSON`).
