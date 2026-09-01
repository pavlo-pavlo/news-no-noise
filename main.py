import calendar
import html
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml
from google import genai


GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()

SENT_NEWS_FILE = Path("data/sent_news.json")
MAX_NEWS_AGE_DAYS = 30
MAX_ENTRIES_PER_SOURCE = 100
MAX_CANDIDATES_FOR_GEMINI = 300
MAX_HISTORY_FOR_GEMINI = 100
MAX_DIGEST_ITEMS = 3
RSS_TIMEOUT_SECONDS = 25
TELEGRAM_TIMEOUT_SECONDS = 30
GEMINI_ATTEMPTS = 3

MADRID_TZ = ZoneInfo("Europe/Madrid")

CATEGORY_LABELS = {
    "ukraine": "🇺🇦 Украина",
    "russia": "🇷🇺 Россия",
    "spain": "🇪🇸 Испания",
    "valencia": "🇪🇸 Валенсия",
    "world": "🌍 Мир",
}

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}

client = genai.Client(api_key=GEMINI_API_KEY)


def clean_text(value):
    """Remove HTML and collapse whitespace without changing factual content."""
    if value is None:
        return ""

    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def canonical_url(url):
    """Normalize a URL for duplicate detection while preserving the original URL for publishing."""
    url = (url or "").strip()
    if not url:
        return ""

    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return ""

        filtered_query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            key_lower = key.lower()
            if key_lower in TRACKING_QUERY_KEYS:
                continue
            if any(key_lower.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
                continue
            filtered_query.append((key, value))

        path = parts.path or "/"
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                path,
                urlencode(filtered_query, doseq=True),
                "",
            )
        )
    except Exception:
        return ""


def parse_iso_datetime(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def load_sent_news():
    if not SENT_NEWS_FILE.exists():
        return []

    try:
        with SENT_NEWS_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Не удалось прочитать {SENT_NEWS_FILE}: {exc}")
        return []

    if not isinstance(data, list):
        print(f"Некорректный формат {SENT_NEWS_FILE}: ожидался JSON-массив.")
        return []

    return [item for item in data if isinstance(item, dict)]


def save_sent_news(items):
    SENT_NEWS_FILE.parent.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_NEWS_AGE_DAYS)

    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            continue

        created_at = parse_iso_datetime(item.get("created_at"))
        if created_at is None or created_at < cutoff:
            continue

        if not item.get("url"):
            continue

        cleaned.append(item)

    cleaned.sort(key=lambda item: item.get("created_at", ""))

    with SENT_NEWS_FILE.open("w", encoding="utf-8") as file:
        json.dump(cleaned, file, ensure_ascii=False, indent=2)


def telegram_send(chat_id, text, enable_preview=True):
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        api_url,
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": not enable_preview,
        },
        timeout=TELEGRAM_TIMEOUT_SECONDS,
    )

    print(response.text)
    response.raise_for_status()

    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API вернул ошибку: {payload}")

    return payload


def send_admin_alert(text):
    if not ADMIN_CHAT_ID:
        print(f"ADMIN ALERT: {text}")
        return

    try:
        telegram_send(ADMIN_CHAT_ID, text, enable_preview=False)
    except Exception as exc:
        print(f"Не удалось отправить уведомление администратору: {exc}")


def load_sources():
    with open("sources.yaml", "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict) or not isinstance(config.get("sources"), dict):
        raise ValueError("sources.yaml должен содержать объект 'sources'.")

    return config["sources"]


def get_digest_window(now_local):
    """Return the editorial window in Europe/Madrid local time."""
    today = now_local.date()

    morning_end = datetime(
        today.year,
        today.month,
        today.day,
        9,
        0,
        tzinfo=MADRID_TZ,
    )
    evening_end = datetime(
        today.year,
        today.month,
        today.day,
        18,
        0,
        tzinfo=MADRID_TZ,
    )

    if now_local.hour < 13:
        previous_day = today - timedelta(days=1)
        start = datetime(
            previous_day.year,
            previous_day.month,
            previous_day.day,
            18,
            0,
            tzinfo=MADRID_TZ,
        )
        end = morning_end
        digest_type = "morning"
        digest_name = "утренняя"
    else:
        start = morning_end
        end = evening_end
        digest_type = "evening"
        digest_name = "вечерняя"

    return digest_type, digest_name, start, end


def entry_datetime_utc(entry):
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                timestamp = calendar.timegm(parsed)
                return datetime.fromtimestamp(timestamp, tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass

    return None


def entry_summary(entry):
    for field in ("summary", "description"):
        value = clean_text(entry.get(field, ""))
        if value:
            return value

    content = entry.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                value = clean_text(item.get("value", ""))
                if value:
                    return value

    return ""


def fetch_feed(source):
    source_name = source.get("name", "Неизвестный источник")
    source_url = source.get("url", "")

    if not source_url:
        raise ValueError(f"У источника {source_name!r} отсутствует URL.")

    response = requests.get(
        source_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; NewsNoNoiseBot/1.0; "
                "+https://github.com/)"
            )
        },
        timeout=RSS_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    feed = feedparser.parse(response.content)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSS не удалось разобрать: {feed.bozo_exception}")

    return feed


def collect_candidates(sources, window_start, window_end, sent_urls):
    candidates = []
    seen_urls = set()
    working_sources = 0
    failed_sources = []

    start_utc = window_start.astimezone(timezone.utc)
    end_utc = window_end.astimezone(timezone.utc)

    for category, source_list in sources.items():
        if not isinstance(source_list, list):
            continue

        for source in source_list:
            source_name = source.get("name", "Неизвестный источник")

            try:
                feed = fetch_feed(source)
                working_sources += 1
            except Exception as exc:
                failed_sources.append(f"{source_name}: {exc}")
                print(f"Ошибка RSS {source_name}: {exc}")
                continue

            for entry in feed.entries[:MAX_ENTRIES_PER_SOURCE]:
                title = clean_text(entry.get("title", ""))
                link = (entry.get("link", "") or "").strip()
                summary = entry_summary(entry)
                published_at = entry_datetime_utc(entry)
                normalized_link = canonical_url(link)

                if not title or not link or not summary or published_at is None:
                    continue

                if not normalized_link:
                    continue

                if not (start_utc <= published_at < end_utc):
                    continue

                if normalized_link in sent_urls or normalized_link in seen_urls:
                    continue

                seen_urls.add(normalized_link)

                candidates.append(
                    {
                        "category": category,
                        "source": source_name,
                        "language": source.get("language", ""),
                        "title": title,
                        "summary": summary[:1000],
                        "url": link,
                        "canonical_url": normalized_link,
                        "published_at": published_at.isoformat(),
                    }
                )

    candidates.sort(key=lambda item: item["published_at"], reverse=True)
    candidates = candidates[:MAX_CANDIDATES_FOR_GEMINI]

    return candidates, working_sources, failed_sources


def recent_history_for_gemini(sent_news):
    recent = sorted(
        sent_news,
        key=lambda item: item.get("created_at", ""),
        reverse=True,
    )[:MAX_HISTORY_FOR_GEMINI]

    history = []
    for item in recent:
        history.append(
            {
                "title": item.get("title", ""),
                "original_title": item.get("original_title", ""),
                "source": item.get("source", ""),
                "url": item.get("url", ""),
                "created_at": item.get("created_at", ""),
            }
        )

    return history


def build_prompt(candidates, sent_news, digest_name, window_start, window_end):
    history = recent_history_for_gemini(sent_news)

    candidate_payload = []
    for item in candidates:
        candidate_payload.append(
            {
                "category": item["category"],
                "source": item["source"],
                "language": item["language"],
                "title": item["title"],
                "summary": item["summary"],
                "url": item["url"],
                "published_at": item["published_at"],
            }
        )

    return f"""
Ты редактор Telegram-канала «Новости без шума».

Нужно подготовить {digest_name} сводку и выбрать до {MAX_DIGEST_ITEMS} самых важных общественно значимых НОВЫХ событий из предоставленного списка.

Период сводки по времени Europe/Madrid:
{window_start.isoformat()} — {window_end.isoformat()}.

Строгие правила:
1. Используй только факты из поля title и summary предоставленных материалов.
2. Ничего не придумывай и не добавляй факты из памяти.
3. Не делай собственных выводов, оценок, прогнозов и предположений.
4. Не скрывай существенные факты, которые есть в предоставленном материале.
5. Заголовок на русском должен точно передавать смысл исходного заголовка, без кликбейта.
6. Краткое описание — максимум 2 коротких предложения на русском языке.
7. URL в ответе должен быть ТОЧНО скопирован из одного из предоставленных кандидатов. Не создавай и не изменяй URL.
8. Не выбирай один и тот же материал дважды.
9. Если несколько материалов описывают одно и то же событие, выбери только один наиболее информативный материал.
10. Учитывай список ранее опубликованных материалов. Не выбирай повтор того же события, если в новом материале нет существенного нового факта.
11. Если событие уже публиковалось, но произошло существенное новое развитие, его можно выбрать повторно.
12. Если данных материала недостаточно для точного краткого описания, не выбирай его.
13. Верни не более {MAX_DIGEST_ITEMS} материалов. Если действительно важных подходящих событий меньше — верни меньше.

Полностью исключи:
- спорт и спортивные соревнования;
- футбол, теннис, баскетбол, хоккей, FIFA, UEFA, World Cup;
- шоу-бизнес и знаменитостей;
- музыку и концерты;
- фестивали;
- кино;
- театр и оперу;
- искусство и культуру;
- музеи и выставки;
- фотографию;
- развлекательные и светские новости.

Допустимые темы:
- политика;
- экономика;
- международные отношения;
- безопасность;
- война;
- чрезвычайные происшествия;
- технологии;
- наука;
- здравоохранение;
- законы;
- миграция;
- энергетика;
- инфраструктура;
- образование;
- экология.

Верни только JSON следующего вида, без Markdown и без дополнительного текста:
{{
  "items": [
    {{
      "url": "точный URL кандидата",
      "title_ru": "точный заголовок на русском",
      "summary_ru": "до двух коротких предложений на русском"
    }}
  ]
}}

Ранее опубликованные материалы:
{json.dumps(history, ensure_ascii=False)}

Кандидаты текущего периода:
{json.dumps(candidate_payload, ensure_ascii=False)}
""".strip()


def select_news_with_gemini(candidates, sent_news, digest_name, window_start, window_end):
    prompt = build_prompt(
        candidates=candidates,
        sent_news=sent_news,
        digest_name=digest_name,
        window_start=window_start,
        window_end=window_end,
    )

    last_error = None

    for attempt in range(1, GEMINI_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )

            raw = (response.text or "").strip()
            if not raw:
                raise ValueError("Gemini вернул пустой ответ.")

            data = json.loads(raw)
            items = data.get("items", [])
            if not isinstance(items, list):
                raise ValueError("Поле 'items' в ответе Gemini не является массивом.")

            return items[:MAX_DIGEST_ITEMS]

        except Exception as exc:
            last_error = exc
            print(f"Ошибка Gemini, попытка {attempt}/{GEMINI_ATTEMPTS}: {exc}")

            if attempt < GEMINI_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))

    raise RuntimeError(f"Gemini не смог сформировать сводку: {last_error}")


def validate_selected_items(selected_items, candidates, sent_urls):
    candidate_by_url = {item["url"]: item for item in candidates}
    validated = []
    selected_canonical_urls = set()

    for selected in selected_items:
        if not isinstance(selected, dict):
            continue

        selected_url = (selected.get("url", "") or "").strip()
        title_ru = clean_text(selected.get("title_ru", ""))
        summary_ru = clean_text(selected.get("summary_ru", ""))

        candidate = candidate_by_url.get(selected_url)
        if candidate is None:
            print(f"Gemini вернул URL, которого нет среди кандидатов: {selected_url}")
            continue

        normalized_url = candidate["canonical_url"]
        if normalized_url in sent_urls or normalized_url in selected_canonical_urls:
            continue

        if not title_ru or not summary_ru:
            continue

        validated.append(
            {
                "title_ru": title_ru,
                "summary_ru": summary_ru,
                "source": candidate["source"],
                "url": candidate["url"],
                "canonical_url": normalized_url,
                "category": candidate["category"],
                "original_title": candidate["title"],
                "published_at": candidate["published_at"],
            }
        )
        selected_canonical_urls.add(normalized_url)

        if len(validated) >= MAX_DIGEST_ITEMS:
            break

    return validated


def build_news_message(item):
    """Single canonical Telegram post format. The URL is always the final line."""
    category_name = CATEGORY_LABELS.get(item["category"], item["category"])
    title = clean_text(item["title_ru"])
    summary = clean_text(item["summary_ru"])
    source = clean_text(item["source"])
    url = item["url"].strip()

    message = (
        f"{category_name}\n\n"
        f"{title}\n\n"
        f"{summary}\n\n"
        f"Источник: {source}\n\n"
        f"{url}"
    )

    if len(message) > 4096:
        max_summary_length = max(300, 4096 - len(message) + len(summary) - 50)
        summary = summary[:max_summary_length].rstrip()
        if summary and not summary.endswith((".", "!", "?", "…")):
            summary += "…"

        message = (
            f"{category_name}\n\n"
            f"{title}\n\n"
            f"{summary}\n\n"
            f"Источник: {source}\n\n"
            f"{url}"
        )

    if len(message) > 4096:
        raise ValueError("Новостной пост превышает лимит Telegram 4096 символов.")

    return message


def make_sent_record(item, digest_type):
    return {
        "url": item["url"],
        "canonical_url": item["canonical_url"],
        "title": item["title_ru"],
        "original_title": item["original_title"],
        "summary": item["summary_ru"],
        "source": item["source"],
        "category": item["category"],
        "published_at": item["published_at"],
        "digest_type": digest_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    now_local = datetime.now(MADRID_TZ)
    digest_type, digest_name, window_start, window_end = get_digest_window(now_local)

    print(
        f"Запуск: {now_local.isoformat()} | "
        f"сводка: {digest_name} | "
        f"окно: {window_start.isoformat()} — {window_end.isoformat()}"
    )

    sources = load_sources()
    sent_news = load_sent_news()

    sent_urls = set()
    for item in sent_news:
        normalized = item.get("canonical_url") or canonical_url(item.get("url", ""))
        if normalized:
            sent_urls.add(normalized)

    candidates, working_sources, failed_sources = collect_candidates(
        sources=sources,
        window_start=window_start,
        window_end=window_end,
        sent_urls=sent_urls,
    )

    print(f"Рабочих RSS-источников: {working_sources}")
    print(f"Ошибок RSS-источников: {len(failed_sources)}")
    print(f"Новых кандидатов для Gemini: {len(candidates)}")

    if working_sources == 0:
        raise RuntimeError("Не удалось получить данные ни из одного RSS-источника.")

    if failed_sources:
        print("Проблемные источники:")
        for error in failed_sources:
            print(f"- {error}")

    if not candidates:
        print("В указанном временном окне нет новых подходящих материалов.")
        save_sent_news(sent_news)
        return

    selected_items = select_news_with_gemini(
        candidates=candidates,
        sent_news=sent_news,
        digest_name=digest_name,
        window_start=window_start,
        window_end=window_end,
    )

    selected_items = validate_selected_items(
        selected_items=selected_items,
        candidates=candidates,
        sent_urls=sent_urls,
    )

    if not selected_items:
        print("После проверки ответа Gemini не осталось материалов для публикации.")
        save_sent_news(sent_news)
        return

    published_count = 0
    failed_publications = []

    for item in selected_items:
        message = build_news_message(item)

        try:
            # Each news item is sent as a separate Telegram message.
            # The source URL is the final line, and link previews are explicitly enabled.
            telegram_send(CHANNEL_ID, message, enable_preview=True)
        except Exception as exc:
            failed_publications.append(f"{item['url']}: {exc}")
            print(f"Ошибка публикации {item['url']}: {exc}")
            continue

        sent_news.append(make_sent_record(item, digest_type))
        sent_urls.add(item["canonical_url"])
        save_sent_news(sent_news)
        published_count += 1

    print(f"Опубликовано новостей: {published_count}")

    if failed_publications:
        alert = (
            f"⚠️ Новости без шума: часть {digest_name} сводки не опубликована.\n"
            f"Успешно: {published_count}\n"
            f"Ошибок: {len(failed_publications)}"
        )
        send_admin_alert(alert)

    if published_count == 0:
        raise RuntimeError("Telegram не принял ни одну выбранную новость.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error_text = f"❌ Новости без шума: ошибка запуска.\n{type(exc).__name__}: {exc}"
        print(error_text)
        send_admin_alert(error_text)
        raise
