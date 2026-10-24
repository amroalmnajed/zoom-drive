# Zoom-to-Drive Automation

An automated pipeline that fetches Zoom cloud recordings, routes them through a Telegram approval bot, streams approved videos directly to Google Drive (bypassing local disk storage), and updates `.docx` tracking files with the uploaded video links.

## Features

- **Automated Zoom Recording Fetching**: Retrieves recent Zoom cloud recordings via Server-to-Server OAuth
- **Telegram Approval Workflow**: Interactive inline keyboard for manual categorization and approval
- **Direct Streaming**: Uploads videos directly from Zoom to Google Drive without local storage
- **Multi-Account Support**: Routes different class videos to different Google Drive accounts
- **Auto-Session Numbering**: Automatically detects next session number in Drive folders
- **Docx Integration**: Updates Word documents with hyperlinks to uploaded videos
- **Undo Functionality**: Rollback uploads and docx changes via Telegram commands
- **Resumable Uploads**: Handles network interruptions with automatic retry and resume

## Tech Stack

- **Language**: Python 3.9+
- **Core Libraries**:
  - `google-api-python-client` - Google Drive API
  - `python-docx` - Word document manipulation
  - `requests` - HTTP streaming and API calls
- **Deployment**: Railway (continuous deployment via GitHub)

## Project Structure

```
zoom-drive/
├── bot_runtime.py       # Bot runtime, command dispatch, and CLI entrypoint
├── standard_upload.py   # Standard Zoom recording approval/upload workflow
├── multi_upload.py      # Multi-folder recording selection/upload workflow
├── undo_admin.py        # Undo and state administration workflow
├── config.py            # Configuration and environment variables
├── zoom_api.py          # Zoom Server-to-Server OAuth and recording fetch
├── drive_api.py         # Google Drive OAuth and resumable upload
├── telegram_bot.py      # Telegram bot with inline keyboards
├── docx_updater.py      # Word document hyperlink management
├── state.py             # Processed recording state persistence
├── undo_store.py        # Undo history management
├── models.py            # Data models (Recording, ApprovalRequest)
├── utils.py             # Utility functions
├── auth_all.py          # Local authentication helper
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template
└── tests/               # Test files
```

## Installation

### Prerequisites

- Python 3.9 or higher
- Zoom Server-to-Server OAuth app credentials
- Telegram bot token
- Google Drive OAuth credentials (one per account)

### Local Setup

1. **Clone the repository**:
   ```bash
   git clone <your-repo-url>
   cd zoom-drive
   ```

2. **Create virtual environment**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**:
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

5. **Authenticate Google accounts** (local development only):
   ```bash
   python auth_all.py
   ```
   This will generate `token_*.json` files for each Google account.

## Configuration

### Environment Variables

Create a `.env` file with the following variables:

#### Zoom Credentials
```env
ZOOM_ACCOUNT_ID=your_zoom_account_id
ZOOM_CLIENT_ID=your_zoom_client_id
ZOOM_CLIENT_SECRET=your_zoom_client_secret
ZOOM_USER_ID=your_zoom_user_id_or_email
```

#### Telegram
```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

#### Google Drive (Multi-Account)
```env
# Folder IDs (get these from Google Drive URL: https://drive.google.com/drive/folders/FOLDER_ID)
FOLDER_ID_1=your_drive_folder_id_1
FOLDER_ID_2=your_drive_folder_id_2
FOLDER_ID_3=your_drive_folder_id_3
FOLDER_ID_4=your_drive_folder_id_4

# OAuth tokens (for local development with file-based auth)
GOOGLE_TOKEN_MAIN_JSON='{"token":"..."}'
GOOGLE_TOKEN_CRASH_JSON='{"token":"..."}'
GOOGLE_TOKEN_G10_JSON='{"token":"..."}'
GOOGLE_TOKEN_G9_JSON='{"token":"..."}'
```

#### Optional Settings
```env
LOOKBACK_DAYS=7
LOCAL_TIMEZONE=Africa/Cairo
UPLOAD_CHUNK_MB=16
UNDO_DELETE_PERMANENTLY=0
MAX_RETRIES=6
BACKOFF_BASE_SECONDS=2
BACKOFF_MAX_SECONDS=60
HTTP_TIMEOUT_SECONDS=120
ZOOM_RATE_LIMIT=10
```

### Folder Configuration

Folder IDs are configured via environment variables for security and deployment flexibility:

1. **For local development**: Set folder IDs in your `.env` file
2. **For Railway/Cloud deployment**: Set them as environment variables in the Railway dashboard

The folder routing rules (keywords, names, codes) are defined in `config.py`. To add or modify folders, edit the `FOLDERS` dictionary in `config.py` and add corresponding `FOLDER_ID_N` environment variables.

## Usage

### One-Shot Mode (Terminal)

Process recordings from the last 7 days:
```bash
python bot_runtime.py
```

Process recordings from the last 30 days:
```bash
python bot_runtime.py --days 30
```

Dry run (fetch and prompt only, no upload):
```bash
python bot_runtime.py --dry-run
```

### Bot Mode (Persistent)

Run as a persistent Telegram bot:
```bash
python bot_runtime.py --bot
```

#### Telegram Commands

- `/run` - Process last 7 days
- `/run 30` - Process last N days
- `/run --dry` - Dry run (no uploads)
- `/multi` - Upload same recording to multiple folders
- `/undo` - Undo a recent upload + docx link
- `/status` - Show processed recordings count
- `/stats` - Detailed statistics
- `/reset` - Clear processed history
- `/backup` - Download state backup
- `/restore` - Upload backup file to restore state
- `/help` - Show available commands

### Railway Deployment

1. **Push to GitHub**:
   ```bash
   git add .
   git commit -m "Deploy to Railway"
   git push
   ```

2. **Configure Railway Variables**:
   - Go to Railway Dashboard → Variables
   - Add all environment variables from `.env`
   - For Google tokens, paste the JSON content as strings (not file paths)
   - **Important**: Set folder IDs as `FOLDER_ID_1`, `FOLDER_ID_2`, etc.

3. **Configure Persistent Storage** (optional but recommended):
   - Railway's filesystem is ephemeral - state files are lost on redeploy
   - Add a Railway Volume for `/app/data` to persist `processed.json` and `undo_history.json`
   - Or use Railway's built-in database for state persistence

3. **Deploy**:
   - Railway will auto-deploy from GitHub
   - The bot will start automatically

## Data Flow

1. **Fetch**: `standard_upload.py` queries `zoom_api.py` for recent recordings
2. **Filter**: Skips recordings already in `processed.json`
3. **Approve**: Sends interactive messages to Telegram for user decision
4. **Stream**: If approved, streams video directly from Zoom to Google Drive in chunks
5. **Update**: On successful upload, updates `.docx` file with video hyperlink

## Railway Deployment Notes

### Ephemeral Filesystem
Railway's filesystem is ephemeral - files are lost on each deployment. This affects:
- `processed.json` - tracking which recordings have been processed
- `undo_history.json` - undo functionality

**Solutions**:
1. **Recommended**: Add a Railway Volume mounted to `/app/data` for persistent storage
2. **Alternative**: Use Railway's built-in database (PostgreSQL/Redis) for state persistence
3. **Accept limitation**: State resets on each deploy (recordings may be re-processed)

### Environment Variables
All configuration must be set as Railway environment variables:
- Zoom credentials (ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, ZOOM_USER_ID)
- Telegram credentials (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- Google Drive folder IDs (FOLDER_ID_1, FOLDER_ID_2, etc.)
- Google OAuth tokens as JSON strings (GOOGLE_TOKEN_MAIN_JSON, etc.)

## Development Conventions

- **No Local Storage**: Videos are streamed directly; never buffer full files to disk
- **Multi-Account**: Different classes upload to different Google accounts
- **Security**: OAuth tokens are excluded from git; cloud deployments use environment variables
- **Fail-Safes**: Resumable upload implements backoff/retries for HTTP 429 and 5xx errors

## Troubleshooting

### Zoom API Errors

**Error**: "Zoom rejected credentials (HTTP 401)"
- **Solution**: Verify `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, and `ZOOM_CLIENT_SECRET` are correct

### Google Drive Auth Errors

**Error**: "Google Drive auth failed"
- **Solution**: 
  - Local: Run `python auth_all.py` to regenerate tokens
  - Railway: Verify token JSON strings are correctly formatted in environment variables

### Docx Update Failures

**Error**: "No row for session X"
- **Solution**: Ensure the `.docx` file has a table with a "Session #" column matching the session number

### Telegram Not Responding

**Error**: Bot doesn't send messages
- **Solution**: Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are correct

### Upload Interrupted

**Error**: "Zoom download interrupted"
- **Solution**: The system will automatically retry. If persistent, check network connectivity and Zoom API status

## Testing

Run existing tests:
```bash
python -m pytest tests/
```

## Security Notes

- Never commit `.env`, `credentials.json`, or `token_*.json` files
- Use Railway's encrypted environment variables for cloud deployments
- Rotate OAuth tokens periodically
- Limit Telegram bot to authorized chat IDs only

## License

[Add your license here]

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Submit a pull request

## Support

For issues or questions, please open an issue on GitHub or contact the maintainers.
