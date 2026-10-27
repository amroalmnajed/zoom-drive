# NexCloud Deployment Guide

This guide explains how to deploy the Zoom-to-Google-Drive Telegram bot to NexCloud's free Telegram bot hosting.

## What is NexCloud?

NexCloud provides free Telegram bot hosting with:
- **256 MB DDR4 RAM**
- **50% CPU share**
- **512 MB NVMe storage**
- **24/7 uptime with no sleep mode**
- **No credit card required**
- **Pterodactyl panel access**
- **SFTP support**
- **Auto-restart on crashes**

## Prerequisites

1. A NexCloud account (sign up via their Discord bot - no credit card)
2. Your Telegram bot token from BotFather
3. Zoom Server-to-Server OAuth credentials
4. Google Drive API credentials

## Step 1: Create the Deployment Package

Run the deployment script to create a zip file with all necessary files:

```bash
python prepare_nexcloud_deploy.py
```

This will create `nexcloud_deploy.zip` in your project directory.

## Step 2: Sign Up for NexCloud

1. Join the NexCloud Discord server (follow the link on their website: https://nexcloud.in/free/telegram)
2. Use their Discord bot to create a free account
3. No credit card required

## Step 3: Create a Telegram Bot Server

1. Log in to the NexCloud Pterodactyl panel
2. Click **Create Server** or **Deploy New Server**
3. Select **Telegram Bot** as the server type
4. Choose **Python** as the runtime
5. Select the **Free** plan (256 MB RAM, 50% CPU, 512 MB storage)
6. Give your server a name (e.g., `zoom-drive-bot`)
7. Click **Create**

## Step 4: Upload Your Files

### Option A: Upload via File Manager
1. Go to your server's **Files** tab
2. Upload `nexcloud_deploy.zip`
3. Extract the zip file
4. Delete the zip file after extraction

### Option B: Upload via SFTP
1. Get your SFTP credentials from the NexCloud panel
2. Use an SFTP client (FileZilla, WinSCP, etc.)
3. Upload `nexcloud_deploy.zip`
4. Extract it on the server
5. Delete the zip file

## Step 5: Configure Environment Variables

NexCloud uses Pterodactyl's startup variables for environment configuration. Go to the **Startup** tab and add these variables:

### Required Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `ZOOM_ACCOUNT_ID` | Your Zoom account ID | `abc123xyz` |
| `ZOOM_CLIENT_ID` | Zoom Server-to-Server OAuth client ID | `your_client_id` |
| `ZOOM_CLIENT_SECRET` | Zoom Server-to-Server OAuth client secret | `your_client_secret` |
| `ZOOM_USER_ID` | Your Zoom user ID/email | `user@example.com` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from BotFather | `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11` |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID for notifications | `123456789` |
| `FOLDER_ID_1` | Google Drive folder ID for category 1 | `1A2B3C4D5E6F7G8H9` |
| `FOLDER_ID_2` | Google Drive folder ID for category 2 | `1A2B3C4D5E6F7G8H9` |

### Google Drive Credentials

You have two options for Google Drive authentication:

#### Option 1: Single JSON string (Recommended)
- `GOOGLE_TOKEN_JSON`: Paste your entire OAuth credentials JSON as a single string
- Use escape characters for quotes if needed

#### Option 2: Individual fields
- `GOOGLE_CLIENT_ID`: OAuth client ID
- `GOOGLE_CLIENT_SECRET`: OAuth client secret
- `GOOGLE_REFRESH_TOKEN`: OAuth refresh token

### Optional Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MAX_DOWNLOAD_ATTEMPTS` | Max retry attempts for downloads | `3` |
| `DOWNLOAD_TIMEOUT` | Download timeout in seconds | `300` |

## Step 6: Configure Startup Command

In the **Startup** tab, set the startup command to:

```
python nexcloud_start.py
```

This runs the NexCloud-specific entry point script that starts the bot.

## Step 7: Start the Server

1. Click **Start** in the Pterodactyl panel
2. Check the **Console** tab for startup logs
3. Look for messages like "Bot started successfully" or similar

## Step 8: Test the Bot

Send `/run` to your Telegram bot. The bot should:
- Respond with a confirmation message
- Begin polling for Zoom recordings
- Download approved recordings directly from Zoom's servers (not your WiFi)
- Upload them to Google Drive
- Update the tracking `.docx` files

## Troubleshooting

### Bot doesn't start
- Check the **Console** tab for error messages
- Verify all environment variables are set correctly
- Ensure `nexcloud_start.py` exists in the file manager
- Check that `requirements.txt` is present and valid

### Dependencies not installed
- NexCloud should automatically install dependencies from `requirements.txt`
- If not, manually run: `pip install -r requirements.txt` in the console

### Bot goes offline
- Check the **Console** tab for crash logs
- NexCloud auto-restarts on crashes by default
- If crashes persist, check RAM usage (256 MB limit)
- Consider reducing the number of concurrent downloads

### Zoom authentication fails
- Verify Zoom credentials are correct
- Check that Server-to-Server OAuth is enabled in Zoom
- Ensure the account ID and user ID match your Zoom account

### Google Drive upload fails
- Verify Google OAuth credentials are valid
- Check that folder IDs are correct and accessible
- Ensure the bot has write permissions to the folders

### Storage limit reached
- You have 512 MB storage
- Check file sizes in the **Files** tab
- Clean up old downloaded recordings if needed
- The bot stores `processed.json` and `undo_history.json` - these should be small

## Maintenance

### Updating the Bot
1. Make changes locally
2. Run `python prepare_nexcloud_deploy.py` to create a new zip
3. Upload the new zip to NexCloud
4. Extract and overwrite existing files
5. Restart the server

### Checking Logs
- Use the **Console** tab in Pterodactyl
- Logs show bot activity, errors, and download progress

### Backups
- NexCloud may or may not provide automatic backups
- Manually backup `processed.json` and `undo_history.json` periodically
- Download these files via SFTP or the file manager

## Advantages of NexCloud

- ✅ **True 24/7 uptime** - no sleep mode
- ✅ **No manual renewal** - hands-off operation
- ✅ **Higher CPU** (50%) compared to other free hosts
- ✅ **Pterodactyl panel** - full control and visibility
- ✅ **SFTP access** - easy file management
- ✅ **Auto-restart** - recovers from crashes automatically
- ✅ **No credit card** - completely free

## Limitations

- 256 MB RAM (may be tight for heavy video processing)
- 512 MB storage (monitor file sizes)
- Free tier may have fair-use policies
- Located in Germany (latency may be higher than local)

## Next Steps

After successful deployment:
1. Test the full workflow with a sample Zoom recording
2. Monitor RAM usage in the Pterodactyl panel
3. Set up regular backups of state files
4. Consider upgrading to a paid plan if you need more resources

## Support

- NexCloud Discord: Link on their website
- Check Pterodactyl console for error messages
- Review this guide for common issues
