import os
import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

text = "✅ GitHub Actions подключен.\n\nКанал «Новости без шума» готов к работе."

response = requests.post(
    url,
    json={
        "chat_id": CHANNEL_ID,
        "text": text
    },
    timeout=30
)

print(response.text)
