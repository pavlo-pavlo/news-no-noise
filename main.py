import os
import json
import yaml
import requests
import feedparser
from datetime import datetime
from google import genai

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]

client = genai.Client(api_key=GEMINI_API_KEY)

with open("sources.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

news = []

for category, sources in config["sources"].items():
    for source in sources:
        feed = feedparser.parse(source["url"])
        for entry in feed.entries[:5]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", "").strip()

            if title and link:
                news.append({
                    "category": category,
                    "source": source["name"],
                    "language": source["language"],
                    "title": title,
                    "summary": summary[:700],
                    "url": link
                })

prompt = f"""
Ты редактор канала "Новости без шума".

Задача:
Выбери 3 самые важные общественно значимые новости из списка ниже.
Переведи всё на русский язык.
Сделай кратко, точно и без искажений.

Строгие правила:
1. Используй только факты из предоставленных новостей.
2. Ничего не придумывай.
3. Не добавляй факты из памяти.
4. Не искажай смысл.
5. Не скрывай важные детали.
6. Не делай собственных выводов и предположений.
7. У каждой новости обязательно укажи источник и ссылку.
8. Если данных недостаточно — не выбирай эту новость.

Полностью исключи:
- спорт;
- футбол;
- теннис;
- баскетбол;
- хоккей;
- чемпионаты;
- World Cup;
- FIFA;
- UEFA;
- шоу-бизнес;
- знаменитостей;
- музыку;
- концерты;
- фестивали;
- кино;
- театр;
- оперу;
- искусство;
- культуру;
- музеи;
- выставки;
- фотографию;
- развлекательные новости.

Выбирай только:
- политику;
- экономику;
- международные отношения;
- безопасность;
- войну;
- чрезвычайные происшествия;
- технологии;
- науку;
- здравоохранение;
- законы;
- миграцию;
- энергетику;
- инфраструктуру;
- образование;
- экологию.

Верни только JSON без markdown:
{{
  "items": [
    {{
      "title_ru": "заголовок на русском",
      "summary_ru": "2 коротких предложения",
      "why_important_ru": "1 короткое предложение",
      "source": "название источника",
      "url": "ссылка",
      "category": "ukraine/russia/spain/valencia/world"
    }}
  ]
}}

Новости:
{json.dumps(news, ensure_ascii=False)}
"""

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=prompt
)

raw = response.text.strip()
raw = raw.replace("```json", "").replace("```", "").strip()

data = json.loads(raw)

now = datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")

message = f"📰 Новости без шума | {now}\n\n"

for i, item in enumerate(data["items"], start=1):
    message += f"{i}️⃣ {item['title_ru']}\n\n"
    message += f"Кратко: {item['summary_ru']}\n\n"
    message += f"Почему важно: {item['why_important_ru']}\n\n"
    message += f"Источник: {item['source']}\n"
    message += f"Ссылка: {item['url']}\n\n"
    message += "────────────\n\n"

if len(message) > 4096:
    message = message[:4000] + "\n\n[Сообщение сокращено]"

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

r = requests.post(
    url,
    json={
        "chat_id": CHANNEL_ID,
        "text": message
    },
    timeout=30
)

print(r.text)
