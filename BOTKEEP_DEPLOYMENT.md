# Botkeep Deployment Guide

This guide explains how to deploy the Zoom-to-Google-Drive Telegram bot to Botkeep hosting.

## What is Botkeep?

Botkeep is a free hosting platform that provides:
- **512 MB RAM, 50% CPU, 1 GB storage** (free tier)
- **No renewal timers** - your bot stays online without manual intervention
- **No credit card required**
- **No Discord account required** for signup
- **24/7 continuous operation**
- **Python support**
- **Auto-restart on crashes**

## Prerequisites

Before deploying, ensure you have:
- Your bot code working locally
- A `requirements.txt` file with all dependencies
- Your environment variables ready (Zoom, Telegram, Google credentials)

## Step 1: Create the Deployment Package

Run this command in your terminal (in the zoom-drive folder):

```bash
python prepare_botkeep_deploy.py
```

This will create `botkeep_deploy.zip` with all necessary files.

## Step 2: Sign Up for Botkeep

1. Go to https://botkeep.cloud/
2. Click "Sign up" or "Get started"
3. Register with your email (no credit card required)
4. Verify your email address

## Step 3: Create a Server

1. Log in to the Botkeep dashboard
2. Click "Create server" or "New deployment"
3. Select the **Free plan** (512 MB RAM, 50% CPU, 1 GB storage)
4. Choose **Python** as the runtime
5. Name your server (e.g., "zoom-drive-bot")
6. Click "Create" or "Deploy"

## Step 4: Upload Your Code

There are two ways to upload to Botkeep:

### Option A: ZIP Upload (Recommended for this project)

1. In the server dashboard, go to the **File Manager** or **Files** tab
2. Upload `botkeep_deploy.zip`
3. Extract the zip file in the file manager
4. Ensure all files are in the root directory (not in a subfolder)

### Option B: GitHub Repository

If you have your code on GitHub:
1. Connect your GitHub account in Botkeep settings
2. Select your repository and branch
3. Botkeep will automatically detect Python and install dependencies

## Step 5: Configure Environment Variables

In the Botkeep dashboard, go to **Environment Variables** or **Secrets** and add:

### Required Zoom Credentials
- `ZOOM_ACCOUNT_ID` - Your Zoom account ID
- `ZOOM_CLIENT_ID` - Your Zoom Server-to-Server OAuth client ID
- `ZOOM_CLIENT_SECRET` - Your Zoom Server-to-Server OAuth client secret
- `ZOOM_USER_ID` - Your Zoom user ID or email

### Required Telegram Credentials
- `TELEGRAM_BOT_TOKEN` - Your Telegram bot token from BotFather
- `TELEGRAM_CHAT_ID` - Your Telegram chat ID (numeric ID)

### Required Google Drive Credentials
- `FOLDER_ID_1` - Google Drive folder ID for first category
- `FOLDER_ID_2` - Google Drive folder ID for second category (if applicable)
- Add more `FOLDER_ID_X` variables for additional categories

### Google OAuth Tokens
You have two options:

**Option 1: JSON string (recommended)**
- `GOOGLE_TOKEN_JSON` - Paste your OAuth token JSON as a single-line string
  - Remove newlines and spaces from the JSON
  - Example: `{"token":"xxx","refresh_token":"yyy","client_id":"zzz","client_secret":"aaa","token_uri":"https://oauth2.googleapis.com/token"}`

**Option 2: Individual fields**
- `GOOGLE_TOKEN` - Your OAuth access token
- `GOOGLE_REFRESH_TOKEN` - Your OAuth refresh token
- `GOOGLE_CLIENT_ID` - Your OAuth client ID
- `GOOGLE_CLIENT_SECRET` - Your OAuth client secret
- `GOOGLE_TOKEN_URI` - Token URI (usually `https://oauth2.googleapis.com/token`)

### Optional Configuration
- `TIMEZONE` - Your timezone (e.g., `UTC`, `America/New_York`)

## Step 6: Set the Startup Command

In the Botkeep dashboard, set the startup command to:

```
python botkeep_start.py
```

This will start the bot in persistent bot mode.

## Step 7: Start the Server

1. Click **Start** or **Deploy** in the Botkeep dashboard
2. Wait for the server to initialize
3. Check the **Logs** tab to see if the bot started successfully

## Step 8: Test the Bot

Send `/run` to your Telegram bot. You should see:
- The bot responds with available recordings
- Inline buttons for approval/categorization
- Progress updates as recordings are processed

## Troubleshooting

### Bot doesn't start
- Check the **Logs** tab in the Botkeep dashboard
- Ensure all environment variables are set correctly
- Verify `requirements.txt` includes all dependencies

### Bot crashes on startup
- Check logs for specific error messages
- Ensure `botkeep_start.py` is in the root directory
- Verify Python version compatibility (Botkeep supports Python 3.9+)

### No response to `/run` command
- Verify `TELEGRAM_BOT_TOKEN` is correct
- Check that your chat ID matches `TELEGRAM_CHAT_ID`
- Ensure the bot is authorized to talk to your chat

### Zoom API errors
- Verify Zoom credentials are correct
- Check that Server-to-Server OAuth is enabled in your Zoom app
- Ensure the user ID/email is valid

### Google Drive upload fails
- Verify Google OAuth tokens are valid
- Check that folder IDs are correct and accessible
- Ensure the token has the necessary scopes

### State files not persisting
- Botkeep provides persistent storage - files should survive restarts
- Check that `processed.json` and `undo_history.json` are being created
- Verify the bot has write permissions

## Monitoring

In the Botkeep dashboard, you can:
- View **real-time logs** to debug issues
- Monitor **CPU and RAM usage**
- **Restart** the bot if needed
- Check **server status** (online/offline)

## Resource Limits

The free Botkeep plan provides:
- **512 MB RAM** - sufficient for most Telegram bots
- **50% CPU** - shared CPU resources
- **1 GB storage** - enough for code and state files
- **No renewal timers** - bot stays online automatically

If you need more resources, you can upgrade to a paid plan directly from the dashboard.

## Support

If you encounter issues:
- Check the Botkeep status page: https://status.botkeep.cloud/
- Review the Botkeep documentation on their website
- Contact Botkeep support through their website

## Security Notes

- Never commit `.env` files or credentials to Git
- Use environment variables for all sensitive data
- Rotate your tokens periodically
- Monitor your bot's activity for unauthorized access

## Post-Deployment Checklist

- [ ] Bot starts successfully
- [ ] `/run` command works
- [ ] Recordings are fetched from Zoom
- [ ] Uploads to Google Drive work
- [ ] State files persist after restart
- [ ] Undo functionality works
- [ ] Logs show no critical errors

---

Your bot is now running 24/7 on Botkeep's servers, downloading Zoom recordings through their network instead of your home WiFi!
