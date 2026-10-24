"""Quick connectivity test for Telegram bot."""
import config, requests

token = config.TELEGRAM_BOT_TOKEN
chat_id = config.TELEGRAM_CHAT_ID
print(f"Bot token: {token[:20]}...")
print(f"Chat ID: {chat_id}")

r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
if r.status_code == 200:
    info = r.json()["result"]
    print(f"Bot connected: @{info['username']} ({info['first_name']})")
else:
    print(f"Bot error: {r.status_code} {r.text[:200]}")

r2 = requests.post(
    f"https://api.telegram.org/bot{token}/sendMessage",
    json={
        "chat_id": chat_id,
        "text": "✅ <b>zoom-drive is online!</b>\nbot_runtime.py syntax is clean and all fixes are applied:\n• HTTP 308 redirect fix\n• Zoom access_token query-string fix\n• Dynamic session auto-detection\n• Telegram inline-keyboard approval flow\n\nReady to run <code>python bot_runtime.py --days 30</code>",
        "parse_mode": "HTML",
    },
    timeout=10,
)
if r2.status_code == 200:
    print("Telegram test message sent successfully!")
else:
    print(f"Send error: {r2.status_code} {r2.text[:200]}")
