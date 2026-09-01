import calendar
import html
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml
from google import genai


CHANNEL_NAME = "Новости шумят"

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()

SENT_NEWS_FILE = Path("data/sent_news.json")
MAX_NEWS_AGE_DAYS = 30
MAX_ENTRIES_PER_SOURCE = 150
MAX_CANDIDATES_PER_CATEGORY = 70
MAX_HISTORY_FOR_GEMINI = 80
ITEMS_PER_CATEGORY = 5
RSS_TIMEOUT_SECONDS = 25
TELEGRAM_TIMEOUT_SECONDS = 30
GEMINI_ATTEMPTS = 3
TELEGRAM_MESSAGE_DELAY_SECONDS = 1.15

MADRID_TZ = ZoneInfo("Europe/Madrid")

CATEGORY_ORDER = [
    "world",
    "ukraine",
    "spain",
    "valencia",
    "lugansk",
    "alchevsk",
]

CATEGORY_LABELS = {
    "world": "🌍 Мир",
    "ukraine": "🇺🇦 Украина",
    "spain": "🇪🇸 Испания",
    "valencia": "🇪🇸 Валенсия",
    "lugansk": "📍 Луганск",
    "alchevsk": "📍 Алчевск",
}

CATEGORY_SCOPE = {
    "world": (
        "Выбирай международные события мирового значения: международная политика, "
        "экономика, войны и безопасность, крупные ЧС, наука, технологии, здоровье, "
        "энергетика и климат. Не выбирай чисто локальные события одной страны, если "
        "они не имеют заметного международного значения."
    ),
    "ukraine": (
        "Выбирай события, непосредственно относящиеся к Украине: государственная политика, "
        "экономика, безопасность и война, дипломатия, инфраструктура, энергетика, законы, "
        "общественно значимые происшествия, наука, технологии и здоровье."
    ),
    "spain": (
        "Выбирай события, непосредственно относящиеся к Испании и имеющие национальное "
        "или крупное межрегиональное значение. Чисто локальную новость выбирай только если "
        "она заметно важна для страны в целом."
    ),
    "valencia": (
        "Выбирай события города Валенсия и провинции Валенсия, а также решения Comunitat "
        "Valenciana, если они непосредственно и существенно затрагивают Валенсию."
    ),
    "lugansk": (
        "Выбирай только события, непосредственно относящиеся к городу Луганску. Не подменяй "
        "город Луганск всей Луганской областью/ЛНР. Региональное событие допускается только "
        "если в материале прямо указано существенное влияние на Луганск."
    ),
    "alchevsk": (
        "Выбирай только события, непосредственно относящиеся к городу Алчевску. Не подменяй "
        "Алчевск всей Луганской областью/ЛНР. Региональное событие допускается только если "
        "в материале прямо указано существенное влияние на Алчевск."
    ),
}

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}

# Предварительный фильтр. Финальная тематическая фильтрация всё равно выполняется Gemini.
EXCLUDED_TITLE_PATTERNS = [
    # Русский / украинский
    r"\bспорт\w*",
    r"\bфутбол\w*",
    r"\bтеннис\w*",
    r"\bтеніс\w*",
    r"\bбаскетбол\w*",
    r"\bхокке\w*",
    r"\bхоке\w*",
    r"\bчемпионат\w*",
    r"\bчемпіонат\w*",
    r"\bтурнир\w*",
    r"\bтурнір\w*",
    r"\bкультур\w*",
    r"\bконцерт\w*",
    r"\bкино\b",
    r"\bкіно\b",
    r"\bтеатр\w*",
    r"\bопера\b",
    r"\bмузе\w*",
    r"\bвыстав\w*",
    r"\bвистав\w*",
    r"\bфестивал\w*",
    r"\bпев(ец|ица|цы|ицы)\b",
    r"\bспіва(к|чка|ки|чки)\b",
    r"\bактер\w*",
    r"\bактёр\w*",
    r"\bактрис\w*",
    # Испанский
    r"\bdeportes?\b",
    r"\bfútbol\b",
    r"\bfutbol\b",
    r"\btenis\b",
    r"\bbaloncesto\b",
    r"\bhockey\b",
    r"\bcampeonato\w*",
    r"\btorneo\w*",
    r"\bcultura\w*",
    r"\bconcierto\w*",
    r"\bcine\b",
    r"\bteatro\b",
    r"\bópera\b",
    r"\bmuseo\w*",
    r"\bexposici[oó]n\w*",
    r"\bfestival\w*",
    r"\bcantante\w*",
    r"\bactor\w*",
    r"\bactriz\w*",
    # Английский
    r"\bsports?\b",
    r"\bfootball\b",
    r"\bsoccer\b",
    r"\btennis\b",
    r"\bbasketball\b",
    r"\bhockey\b",
    r"\bchampionship\w*",
    r"\btournament\w*",
    r"\bculture\b",
    r"\bconcert\w*",
    r"\bcinema\b",
    r"\btheat(re|er)\b",
    r"\bmuseum\w*",
    r"\bexhibition\w*",
    r"\bfestival\w*",
    r"\bcelebrity\w*",
    r"\bsinger\w*",
    r"\bactor\w*",
    r"\bactress\w*",
]

client = genai.Client(api_key=GEMINI_API_KEY)


def clean_text(value):
    """Удалить HTML и лишние пробелы без изменения смысла текста."""
    if value is None:
        return ""

    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def canonical_url(url):
    """Нормализовать URL только для дедупликации; оригинальный URL не меняется."""
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

    sources = config["sources"]

    for category in CATEGORY_ORDER:
        if category not in sources or not isinstance(sources[category], list):
            raise ValueError(f"В sources.yaml отсутствует корректный раздел '{category}'.")

    return sources


def get_digest_window(now_local):
    """
    Вернуть последнее полностью завершённое редакционное окно по Europe/Madrid.

    09:00–17:59 -> утренняя сводка: вчера 18:00 — сегодня 09:00.
    18:00–23:59 -> вечерняя сводка: сегодня 09:00 — сегодня 18:00.
    00:00–08:59 -> предыдущая вечерняя сводка: вчера 09:00 — вчера 18:00.

    Такая логика позволяет безопасно запускать workflow вручную в любое время:
    он не указывает в заголовке ещё не наступившую границу периода.
    """
    today = now_local.date()

    today_09 = datetime(today.year, today.month, today.day, 9, 0, tzinfo=MADRID_TZ)
    today_18 = datetime(today.year, today.month, today.day, 18, 0, tzinfo=MADRID_TZ)

    if now_local < today_09:
        previous_day = today - timedelta(days=1)
        start = datetime(previous_day.year, previous_day.month, previous_day.day, 9, 0, tzinfo=MADRID_TZ)
        end = datetime(previous_day.year, previous_day.month, previous_day.day, 18, 0, tzinfo=MADRID_TZ)
        return "evening", "Вечерняя сводка", start, end

    if now_local < today_18:
        previous_day = today - timedelta(days=1)
        start = datetime(previous_day.year, previous_day.month, previous_day.day, 18, 0, tzinfo=MADRID_TZ)
        end = today_09
        return "morning", "Утренняя сводка", start, end

    start = today_09
    end = today_18
    return "evening", "Вечерняя сводка", start, end


def entry_datetime_utc(entry):
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                timestamp = calendar.timegm(parsed)
                return datetime.fromtimestamp(timestamp, tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass

    for field in ("published", "updated", "created"):
        raw = entry.get(field)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(str(raw))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue

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
                "Mozilla/5.0 (compatible; NewsShumyanBot/2.0; "
                "+https://github.com/pavlo-pavlo/news-no-noise)"
            )
        },
        timeout=RSS_TIMEOUT_SECONDS,
        allow_redirects=True,
    )
    response.raise_for_status()

    feed = feedparser.parse(response.content)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSS не удалось разобрать: {feed.bozo_exception}")

    return feed


def source_matches(source, title, summary):
    haystack = f" {title} {summary} "

    match_regex = source.get("match_regex")
    if match_regex:
        return any(
            re.search(str(pattern), haystack, flags=re.IGNORECASE)
            for pattern in match_regex
        )

    match_any = source.get("match_any")
    if match_any:
        haystack_lower = haystack.lower()
        return any(str(term).lower() in haystack_lower for term in match_any)

    return True


def is_excluded_topic(title):
    title_lower = title.lower()
    return any(re.search(pattern, title_lower, flags=re.IGNORECASE) for pattern in EXCLUDED_TITLE_PATTERNS)


def sent_key(category, normalized_url):
    return f"{category}|{normalized_url}"


def build_sent_keys(sent_news):
    keys = set()

    for item in sent_news:
        category = (item.get("category") or "").strip()
        normalized = item.get("canonical_url") or canonical_url(item.get("url", ""))
        if category and normalized:
            keys.add(sent_key(category, normalized))

    return keys


def collect_candidates_by_category(sources, window_start, window_end, sent_keys):
    candidates_by_category = {category: [] for category in CATEGORY_ORDER}
    seen_by_category = {category: set() for category in CATEGORY_ORDER}

    start_utc = window_start.astimezone(timezone.utc)
    end_utc = window_end.astimezone(timezone.utc)

    feed_cache = {}
    feed_errors = {}
    successful_feed_urls = set()

    for category in CATEGORY_ORDER:
        for source in sources.get(category, []):
            source_name = source.get("name", "Неизвестный источник")
            source_url = source.get("url", "")

            if not source_url:
                continue

            if source_url in feed_errors:
                continue

            if source_url not in feed_cache:
                try:
                    feed_cache[source_url] = fetch_feed(source)
                    successful_feed_urls.add(source_url)
                except Exception as exc:
                    feed_errors[source_url] = f"{source_name}: {exc}"
                    print(f"Ошибка RSS {source_name}: {exc}")
                    continue

            feed = feed_cache[source_url]

            for entry in feed.entries[:MAX_ENTRIES_PER_SOURCE]:
                title = clean_text(entry.get("title", ""))
                link = (entry.get("link", "") or "").strip()
                summary = entry_summary(entry)
                published_at = entry_datetime_utc(entry)
                normalized_link = canonical_url(link)

                if not title or not link or published_at is None:
                    continue

                if not normalized_link:
                    continue

                if not (start_utc <= published_at < end_utc):
                    continue

                if is_excluded_topic(title):
                    continue

                if not source_matches(source, title, summary):
                    continue

                key = sent_key(category, normalized_link)
                if key in sent_keys:
                    continue

                if normalized_link in seen_by_category[category]:
                    continue

                seen_by_category[category].add(normalized_link)

                candidates_by_category[category].append(
                    {
                        "category": category,
                        "source": source_name,
                        "language": source.get("language", ""),
                        "title": title,
                        "summary": summary[:800],
                        "url": link,
                        "canonical_url": normalized_link,
                        "published_at": published_at.isoformat(),
                    }
                )

    for category in CATEGORY_ORDER:
        candidates_by_category[category].sort(
            key=lambda item: item["published_at"],
            reverse=True,
        )
        candidates_by_category[category] = candidates_by_category[category][
            :MAX_CANDIDATES_PER_CATEGORY
        ]

    return candidates_by_category, successful_feed_urls, list(feed_errors.values())


def recent_history_for_gemini(sent_news, category):
    same_category = [
        item
        for item in sent_news
        if isinstance(item, dict) and item.get("category") == category
    ]
    same_category.sort(key=lambda item: item.get("created_at", ""), reverse=True)

    history = []
    for item in same_category[:MAX_HISTORY_FOR_GEMINI]:
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


def build_prompt(category, candidates, sent_news, digest_name, window_start, window_end):
    history = recent_history_for_gemini(sent_news, category)

    candidate_payload = []
    for index, item in enumerate(candidates, start=1):
        candidate_payload.append(
            {
                "candidate_id": f"{category}-{index:03d}",
                "source": item["source"],
                "language": item["language"],
                "title": item["title"],
                "summary": item["summary"],
                "published_at": item["published_at"],
            }
        )

    category_label = CATEGORY_LABELS[category]
    scope_rule = CATEGORY_SCOPE[category]

    prompt = f"""
Ты редактор Telegram-канала «{CHANNEL_NAME}».

Нужно подготовить раздел «{category_label}» для выпуска «{digest_name}».
Выбери РОВНО {ITEMS_PER_CATEGORY} самых важных общественно значимых НОВЫХ событий,
если среди кандидатов есть минимум {ITEMS_PER_CATEGORY} действительно подходящих материалов.
Если подходящих материалов меньше {ITEMS_PER_CATEGORY}, верни все подходящие, но ничего не выдумывай.

Период сводки по времени Europe/Madrid:
{window_start.isoformat()} — {window_end.isoformat()}.

Географическое правило раздела:
{scope_rule}

Строгие редакционные правила:
1. Используй только факты из полей title и summary предоставленных кандидатов.
2. Ничего не придумывай и не добавляй факты из памяти.
3. Не делай собственных выводов, оценок, прогнозов и предположений.
4. Не скрывай существенные факты, которые прямо есть в исходном материале.
5. Заголовок на русском должен точно передавать смысл исходного заголовка, без кликбейта.
6. Краткое описание — максимум 2 коротких предложения на русском языке.
7. Не выбирай один и тот же материал дважды.
8. Если несколько кандидатов описывают одно событие, выбери только один наиболее информативный материал.
9. Учитывай историю ранее опубликованных материалов этого раздела. Не повторяй то же событие без существенного нового развития.
10. Если summary пустой, используй только факты из title и не добавляй подробностей. Если даже этого недостаточно для точного описания — не выбирай материал.
11. В ответе используй только candidate_id из списка. Не создавай новые идентификаторы.
12. Источник и URL программа подставит сама. Ты не должен придумывать или изменять их.
13. Для заявлений сторон вооружённого конфликта, военных ведомств, властей или иных заинтересованных сторон сохраняй атрибуцию. Не превращай неподтверждённое заявление одной стороны в безусловно установленный факт.
14. Отделяй факт события от заявлений о причинах, виновниках, потерях и результатах, если эти детали в исходнике представлены как чьи-то утверждения.

Полностью исключи:
- спорт и спортивные соревнования;
- футбол, теннис, баскетбол, хоккей, чемпионаты и турниры;
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

Критерии важности в порядке приоритета:
1. Масштаб последствий для людей, безопасности, экономики или государственного управления.
2. Новизна и существенность произошедшего изменения.
3. Число людей или территорий, которых событие затрагивает.
4. Решения властей, законов, судов, крупных компаний и международных институтов с реальными последствиями.
5. Для локальных разделов — практическое значение для жителей соответствующего города/региона.

Верни только JSON без Markdown и без дополнительного текста:
{{
  "items": [
    {{
      "candidate_id": "точный candidate_id из списка",
      "title_ru": "точный заголовок на русском",
      "summary_ru": "до двух коротких предложений на русском"
    }}
  ]
}}

Ранее опубликованные материалы этого раздела:
{json.dumps(history, ensure_ascii=False)}

Кандидаты текущего периода:
{json.dumps(candidate_payload, ensure_ascii=False)}
""".strip()

    candidate_map = {
        f"{category}-{index:03d}": item
        for index, item in enumerate(candidates, start=1)
    }

    return prompt, candidate_map


def select_news_with_gemini(category, candidates, sent_news, digest_name, window_start, window_end):
    if not candidates:
        return [], {}

    prompt, candidate_map = build_prompt(
        category=category,
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

            return items[:ITEMS_PER_CATEGORY], candidate_map

        except Exception as exc:
            last_error = exc
            print(
                f"Ошибка Gemini для {category}, попытка "
                f"{attempt}/{GEMINI_ATTEMPTS}: {exc}"
            )

            if attempt < GEMINI_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))

    raise RuntimeError(f"Gemini не смог сформировать раздел {category}: {last_error}")


def validate_selected_items(category, selected_items, candidate_map, sent_keys):
    validated = []
    selected_urls = set()

    for selected in selected_items:
        if not isinstance(selected, dict):
            continue

        candidate_id = (selected.get("candidate_id", "") or "").strip()
        title_ru = clean_text(selected.get("title_ru", ""))
        summary_ru = clean_text(selected.get("summary_ru", ""))

        candidate = candidate_map.get(candidate_id)
        if candidate is None:
            print(f"Gemini вернул неизвестный candidate_id: {candidate_id}")
            continue

        normalized_url = candidate["canonical_url"]
        key = sent_key(category, normalized_url)

        if key in sent_keys or normalized_url in selected_urls:
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
                "category": category,
                "original_title": candidate["title"],
                "published_at": candidate["published_at"],
            }
        )
        selected_urls.add(normalized_url)

        if len(validated) >= ITEMS_PER_CATEGORY:
            break

    return validated


def build_news_message(item):
    """Единый формат Telegram-поста. URL всегда является последней строкой."""
    category_name = CATEGORY_LABELS[item["category"]]
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
        max_summary_length = max(250, 4096 - len(message) + len(summary) - 80)
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


def russian_date(dt):
    months = {
        1: "января",
        2: "февраля",
        3: "марта",
        4: "апреля",
        5: "мая",
        6: "июня",
        7: "июля",
        8: "августа",
        9: "сентября",
        10: "октября",
        11: "ноября",
        12: "декабря",
    }
    return f"{dt.day} {months[dt.month]} {dt.year}"


def build_digest_header(digest_name, window_start, window_end):
    period_start = window_start.strftime("%d.%m %H:%M")
    period_end = window_end.strftime("%d.%m %H:%M")

    return (
        f"📰 {CHANNEL_NAME}\n\n"
        f"{digest_name} — {russian_date(window_end)}\n"
        f"Период: {period_start} — {period_end} (Валенсия)"
    )


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
        f"выпуск: {digest_name} | "
        f"окно: {window_start.isoformat()} — {window_end.isoformat()}"
    )

    sources = load_sources()
    sent_news = load_sent_news()
    sent_keys = build_sent_keys(sent_news)

    candidates_by_category, successful_feed_urls, failed_sources = collect_candidates_by_category(
        sources=sources,
        window_start=window_start,
        window_end=window_end,
        sent_keys=sent_keys,
    )

    print(f"Рабочих уникальных RSS-лент: {len(successful_feed_urls)}")
    print(f"Ошибок RSS-лент: {len(failed_sources)}")

    if not successful_feed_urls:
        raise RuntimeError("Не удалось получить данные ни из одного RSS-источника.")

    for category in CATEGORY_ORDER:
        print(
            f"Кандидаты {CATEGORY_LABELS[category]}: "
            f"{len(candidates_by_category[category])}"
        )

    selected_by_category = {}
    selection_errors = []

    for category in CATEGORY_ORDER:
        candidates = candidates_by_category[category]

        if not candidates:
            selected_by_category[category] = []
            continue

        try:
            selected_raw, candidate_map = select_news_with_gemini(
                category=category,
                candidates=candidates,
                sent_news=sent_news,
                digest_name=digest_name,
                window_start=window_start,
                window_end=window_end,
            )

            selected = validate_selected_items(
                category=category,
                selected_items=selected_raw,
                candidate_map=candidate_map,
                sent_keys=sent_keys,
            )
            selected_by_category[category] = selected

        except Exception as exc:
            selection_errors.append(f"{CATEGORY_LABELS[category]}: {exc}")
            selected_by_category[category] = []
            print(f"Ошибка подготовки раздела {category}: {exc}")

    total_selected = sum(len(items) for items in selected_by_category.values())


    if total_selected == 0:
        print("Для публикации не выбрано ни одной новости.")
        save_sent_news(sent_news)

        alert_parts = [f"⚠️ {CHANNEL_NAME}: в {digest_name.lower()} нет публикаций."]
        if selection_errors:
            alert_parts.append(f"Ошибок Gemini: {len(selection_errors)}")
        if failed_sources:
            alert_parts.append(f"Ошибок RSS: {len(failed_sources)}")
        send_admin_alert("\n".join(alert_parts))
        return

    # Заголовок выпуска отправляется отдельно и не содержит URL.
    telegram_send(
        CHANNEL_ID,
        build_digest_header(digest_name, window_start, window_end),
        enable_preview=False,
    )
    time.sleep(TELEGRAM_MESSAGE_DELAY_SECONDS)

    published_by_category = {category: 0 for category in CATEGORY_ORDER}
    failed_publications = []

    for category in CATEGORY_ORDER:
        for item in selected_by_category[category]:
            message = build_news_message(item)

            try:
                # Каждая новость — отдельное сообщение.
                # URL — последняя строка, preview явно включён.
                telegram_send(CHANNEL_ID, message, enable_preview=True)
            except Exception as exc:
                failed_publications.append(f"{item['url']}: {exc}")
                print(f"Ошибка публикации {item['url']}: {exc}")
                time.sleep(TELEGRAM_MESSAGE_DELAY_SECONDS)
                continue

            sent_news.append(make_sent_record(item, digest_type))
            sent_keys.add(sent_key(category, item["canonical_url"]))
            save_sent_news(sent_news)
            published_by_category[category] += 1

            time.sleep(TELEGRAM_MESSAGE_DELAY_SECONDS)

    total_published = sum(published_by_category.values())
    print(f"Всего опубликовано новостей: {total_published}")

    for category in CATEGORY_ORDER:
        print(
            f"{CATEGORY_LABELS[category]}: "
            f"{published_by_category[category]}/{ITEMS_PER_CATEGORY}"
        )

    warnings = []

    shortages = [
        f"{CATEGORY_LABELS[category]} {published_by_category[category]}/{ITEMS_PER_CATEGORY}"
        for category in CATEGORY_ORDER
        if published_by_category[category] < ITEMS_PER_CATEGORY
    ]
    if shortages:
        warnings.append("Меньше 5 новостей: " + ", ".join(shortages))

    if failed_sources:
        warnings.append(f"Неработающих RSS: {len(failed_sources)}")

    if selection_errors:
        warnings.append(f"Ошибок Gemini: {len(selection_errors)}")

    if failed_publications:
        warnings.append(f"Ошибок Telegram: {len(failed_publications)}")

    if warnings:
        send_admin_alert(
            f"⚠️ {CHANNEL_NAME}: {digest_name.lower()} завершена с предупреждениями.\n"
            + "\n".join(warnings)
        )

    if total_published == 0:
        raise RuntimeError("Telegram не принял ни одну выбранную новость.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error_text = (
            f"❌ {CHANNEL_NAME}: ошибка запуска.\n"
            f"{type(exc).__name__}: {exc}"
        )
        print(error_text)
        send_admin_alert(error_text)
        raise
