import os
import json
import yaml
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from pathlib import Path
from google import genai

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]

SENT_NEWS_FILE = Path("data/sent_news.json")
MAX_NEWS_AGE_DAYS = 30

client = genai.Client(api_key=GEMINI_API_KEY)


def load_sent_news():
    if not SENT_NEWS_FILE.exists():
        return []

    try:
        with open(SENT_NEWS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_sent_news(items):
    SENT_NEWS_FILE.parent.mkdir(parents=True, exist_ok=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_NEWS_AGE_DAYS)

    cleaned = []
    for item in items:
        try:
            created_at = datetime.fromisoformat(item["created_at"])
            if created_at >= cutoff:
                cleaned.append(item)
        except Exception:
            continue

    with open(SENT_NEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": CHANNEL_ID,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=30,
    )

    print(response.text)
    response.raise_for_status()


with open("sources.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

sent_news = load_sent_news()
sent_urls = {item["url"] for item in sent_news if "url" in item}

news = []

for category, sources in config["sources"].items():
    for source in sources:
        feed = feedparser.parse(source["url"])

        for entry in feed.entries[:8]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", "").strip()

            if not title or not link:
                continue

            if link in sent_urls:
                continue

            news.append(
                {
                    "category": category,
                    "source": source["name"],
                    "language": source["language"],
                    "title": title,
                    "summary": summary[:700],
                    "url": link,
                }
            )

print(f"Новых кандидатов для Gemini: {len(news)}")

if not news:
    print("Нет новых новостей для публикации.")
    save_sent_news(sent_news)
    raise SystemExit(0)

prompt = f"""
Ты редактор Telegram-канала "Новости без шума".

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
9. Если источник или ссылка отсутствует — не выбирай эту новость.
10. Верни ровно 3 новости, если есть минимум 3 подходящие новости.
11. Если подходящих новостей меньше 3 — верни только подходящие.

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
      "summary_ru": "2 коротких предложения только по фактам источника",
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
    contents=prompt,
)

raw = response.text.strip()
raw = raw.replace("```json", "").replace("```", "").strip()

print("Ответ Gemini:")
print(raw)

data = json.loads(raw)
items = data.get("items", [])

if not items:
    print("Gemini не выбрал подходящие новости.")
    save_sent_news(sent_news)
    raise SystemExit(0)

now_madrid = datetime.now(timezone.utc) + timedelta(hours=2)
now_text = now_madrid.strftime("%d.%m.%Y %H:%M")

category_flags = {
    "ukraine": "🇺🇦 Украина",
    "russia": "🇷🇺 Россия",
    "spain": "🇪🇸 Испания",
    "valencia": "🇪🇸 Валенсия",
    "world": "🌍 Мир",
}

message = f"📰 Новости без шума | {now_text} Испания\n\n"

published_urls = []

for index, item in enumerate(items, start=1):
    title = item.get("title_ru", "").strip()
    summary = item.get("summary_ru", "").strip()
    source = item.get("source", "").strip()
    url = item.get("url", "").strip()
    category = item.get("category", "").strip()

    if not title or not summary or not source or not url:
        continue

    if url in sent_urls:
        continue

    category_name = category_flags.get(category, category)

    message += f"{index}️⃣ {category_name}\n"
    message += f"{title}\n\n"
    message += f"{summary}\n\n"
    message += f"Источник: {source}\n"
    message += f"Ссылка: {url}\n\n"
    message += "────────────\n\n"

    published_urls.append(
        {
            "url": url,
            "title": title,
            "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )

if not published_urls:
    print("Нет новых новостей после финальной проверки дублей.")
    save_sent_news(sent_news)
    raise SystemExit(0)

if len(message) > 4096:
    message = message[:4000] + "\n\n[Сообщение сокращено]"

send_telegram_message(message)

sent_news.extend(published_urls)
save_sent_news(sent_news)

print(f"Опубликовано новостей: {len(published_urls)}")
