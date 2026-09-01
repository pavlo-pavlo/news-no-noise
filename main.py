import calendar
import html
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from google import genai


CHANNEL_NAME = "Новости шумят"

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()

SENT_NEWS_FILE = Path("data/sent_news.json")
MAX_NEWS_AGE_DAYS = 30
ITEMS_PER_CATEGORY = 10
MAX_CANDIDATES_PER_CATEGORY = 120
MAX_HISTORY_FOR_GEMINI = 100
MAX_RSS_ENTRIES_PER_SOURCE = 200
RSS_TIMEOUT_SECONDS = 25
HTML_TIMEOUT_SECONDS = 25
TELEGRAM_TIMEOUT_SECONDS = 30
TELEGRAM_ATTEMPTS = 4
GEMINI_ATTEMPTS = 3
TELEGRAM_MESSAGE_DELAY_SECONDS = 1.25

# Если в основном редакционном окне мало материала, берём более широкий
# rolling-window, но никогда не повторяем уже опубликованные URL.
CATEGORY_LOOKBACK_HOURS = {
    "world": 24,
    "ukraine": 24,
    "russia": 24,
    "spain": 24,
    "valencia": 36,
    "lugansk": 48,
    "alchevsk": 48,
}

MADRID_TZ = ZoneInfo("Europe/Madrid")

CATEGORY_ORDER = [
    "world",
    "spain",
    "ukraine",
    "russia",
    "valencia",
    "lugansk",
    "alchevsk",
]

CATEGORY_LABELS = {
    "world": "🌍 Мир",
    "ukraine": "🇺🇦 Украина",
    "russia": "🇷🇺 Россия",
    "spain": "🇪🇸 Испания",
    "valencia": "🇪🇸 Валенсия",
    "lugansk": "📍 Луганск",
    "alchevsk": "📍 Алчевск",
}

CATEGORY_SCOPE = {
    "world": (
        "Выбирай 10 важнейших международных событий: политика, экономика, войны и безопасность, "
        "крупные чрезвычайные происшествия, наука, технологии, здравоохранение, энергетика, климат, "
        "международные законы и миграция. Не заполняй список мелкими локальными событиями одной страны."
    ),
    "ukraine": (
        "Выбирай 10 важнейших событий, непосредственно относящихся к Украине: государственная политика, "
        "экономика, война и безопасность, дипломатия, инфраструктура, энергетика, законы, общественно "
        "значимые происшествия, здравоохранение, образование, наука и технологии."
    ),
    "russia": (
        "Выбирай 10 важнейших событий, непосредственно относящихся к России: федеральная политика, "
        "экономика, безопасность, война и внешняя политика, законы, крупные происшествия, инфраструктура, "
        "энергетика, здравоохранение, образование, наука и технологии. Не заполняй раздел мелкими "
        "региональными событиями, если они не имеют заметного общефедерального значения."
    ),
    "spain": (
        "Выбирай 10 важнейших событий Испании национального или крупного межрегионального значения: "
        "политика, экономика, законы, миграция, безопасность, чрезвычайные происшествия, инфраструктура, "
        "энергетика, здравоохранение, образование, технологии и экология."
    ),
    "valencia": (
        "Это локальная сводка. Выбирай 10 наиболее важных и полезных событий города Валенсия и провинции "
        "Валенсия. Допускаются решения Generalitat Valenciana, если они прямо затрагивают Валенсию. "
        "Для локальной сводки важны также транспорт, дороги, метро, жильё, коммунальные услуги, пожары, "
        "полиция, суды, школы, больницы, строительство, городское управление и крупные происшествия."
    ),
    "lugansk": (
        "Это локальная городская сводка. Выбирай 10 наиболее важных и полезных событий именно города "
        "Луганска. Не подменяй город всей Луганской областью/ЛНР. Для города важны коммунальные услуги, "
        "вода, электричество, транспорт, дороги, происшествия, безопасность, медицина, школы, строительство, "
        "городские решения и экономика. Региональная новость допустима только при прямом влиянии на Луганск."
    ),
    "alchevsk": (
        "Это локальная городская сводка. Выбирай 10 наиболее важных и полезных событий именно Алчевска. "
        "Не подменяй город всей Луганской областью/ЛНР. Для города важны коммунальные услуги, вода, "
        "электричество, отопление, транспорт, дороги, происшествия, безопасность, медицина, школы, "
        "строительство, городские решения и экономика. Региональная новость допустима только при прямом "
        "влиянии на Алчевск."
    ),
}

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "at_medium", "at_campaign"}

EXCLUDED_TITLE_PATTERNS = [
    # Русский / украинский
    r"\bспорт\w*", r"\bфутбол\w*", r"\bтеннис\w*", r"\bтеніс\w*", r"\bбаскетбол\w*",
    r"\bхокке\w*", r"\bхоке\w*", r"\bчемпионат\w*", r"\bчемпіонат\w*", r"\bтурнир\w*",
    r"\bтурнір\w*", r"\bконцерт\w*", r"\bкино\b", r"\bкіно\b", r"\bтеатр\w*", r"\bопера\b",
    r"\bмузе\w*", r"\bвыстав\w*", r"\bвистав\w*", r"\bфестивал\w*", r"\bшоу[- ]?бизнес\w*",
    # Испанский / valenciano
    r"\bdeportes?\b", r"\besports?\b", r"\bfútbol\b", r"\bfutbol\b", r"\btenis\b",
    r"\bbaloncesto\b", r"\bbàsquet\b", r"\bhockey\b", r"\bcampeonato\w*", r"\btorneo\w*",
    r"\bconcierto\w*", r"\bconcert\w*", r"\bcine\b", r"\bcinema\b", r"\bteatro\b", r"\bteatre\b",
    r"\bópera\b", r"\bmuseo\w*", r"\bmuseu\w*", r"\bexposici[oó]n\w*", r"\bfestival\w*",
    # English
    r"\bsports?\b", r"\bfootball\b", r"\bsoccer\b", r"\btennis\b", r"\bbasketball\b",
    r"\bhockey\b", r"\bchampionship\w*", r"\btournament\w*", r"\bconcert\w*", r"\bcinema\b",
    r"\btheat(re|er)\b", r"\bmuseum\w*", r"\bexhibition\w*", r"\bfestival\w*", r"\bcelebrity\w*",
]

LOW_VALUE_TELEGRAM_PATTERNS = [
    r"^доброе\s+(утро|утречко|вечер)",
    r"^хорошего\s+(дня|вечера)",
    r"^подпис(ывайтесь|аться)",
    r"^реклама\b",
    r"^поздравля",
]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 NewsShumyatBot/4.0"
)

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.9,es;q=0.8"})
client = genai.Client(api_key=GEMINI_API_KEY)


def clean_text(value):
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def canonical_url(url):
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return ""
        filtered = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            key_lower = key.lower()
            if key_lower in TRACKING_QUERY_KEYS:
                continue
            if any(key_lower.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
                continue
            filtered.append((key, value))
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path or "/",
                urlencode(filtered, doseq=True),
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
        return []
    return [item for item in data if isinstance(item, dict)]


def save_sent_news(items):
    SENT_NEWS_FILE.parent.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_NEWS_AGE_DAYS)
    cleaned = []
    for item in items:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        created_at = parse_iso_datetime(item.get("created_at"))
        if created_at is None or created_at < cutoff:
            continue
        cleaned.append(item)
    cleaned.sort(key=lambda item: item.get("created_at", ""))
    with SENT_NEWS_FILE.open("w", encoding="utf-8") as file:
        json.dump(cleaned, file, ensure_ascii=False, indent=2)


def telegram_send(chat_id, text, enable_preview=True, parse_mode=None, reply_markup=None):
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    last_error = None
    for attempt in range(1, TELEGRAM_ATTEMPTS + 1):
        try:
            response = session.post(
                api_url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": not enable_preview,
                    **({"parse_mode": parse_mode} if parse_mode else {}),
                    **({"reply_markup": reply_markup} if reply_markup else {}),
                },
                timeout=TELEGRAM_TIMEOUT_SECONDS,
            )
            print(response.text)
            if response.status_code == 429:
                try:
                    retry_after = int(response.json().get("parameters", {}).get("retry_after", 3))
                except Exception:
                    retry_after = 3
                if attempt < TELEGRAM_ATTEMPTS:
                    time.sleep(retry_after + 1)
                    continue
            if 500 <= response.status_code < 600 and attempt < TELEGRAM_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
                continue
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram API вернул ошибку: {payload}")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt < TELEGRAM_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
                continue
            raise
    raise RuntimeError(f"Telegram не принял сообщение: {last_error}")


def telegram_send_photo(chat_id, photo_url, caption, parse_mode=None, reply_markup=None):
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    last_error = None
    for attempt in range(1, TELEGRAM_ATTEMPTS + 1):
        try:
            payload = {
                "chat_id": chat_id,
                "photo": photo_url,
                "caption": caption,
                **({"parse_mode": parse_mode} if parse_mode else {}),
                **({"reply_markup": reply_markup} if reply_markup else {}),
            }
            response = session.post(api_url, json=payload, timeout=TELEGRAM_TIMEOUT_SECONDS)
            print(response.text)

            if response.status_code == 429:
                try:
                    retry_after = int(response.json().get("parameters", {}).get("retry_after", 3))
                except Exception:
                    retry_after = 3
                if attempt < TELEGRAM_ATTEMPTS:
                    time.sleep(retry_after + 1)
                    continue

            if 500 <= response.status_code < 600 and attempt < TELEGRAM_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
                continue

            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API вернул ошибку: {data}")
            return data
        except Exception as exc:
            last_error = exc
            if attempt < TELEGRAM_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
                continue
            raise

    raise RuntimeError(f"Telegram не принял фотографию: {last_error}")


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
    """Вернуть последнее полностью завершённое редакционное окно Europe/Madrid."""
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

    return "evening", "Вечерняя сводка", today_09, today_18


def is_excluded_topic(title):
    title_lower = title.lower()
    return any(re.search(pattern, title_lower, flags=re.IGNORECASE) for pattern in EXCLUDED_TITLE_PATTERNS)


def source_matches(source, title, summary):
    haystack = f" {title} {summary} "
    if source.get("trusted_city_scope"):
        return True
    patterns = source.get("match_regex") or []
    if patterns:
        return any(re.search(str(pattern), haystack, flags=re.IGNORECASE) for pattern in patterns)
    terms = source.get("match_any") or []
    if terms:
        lower = haystack.lower()
        return any(str(term).lower() in lower for term in terms)
    return True


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


def entry_datetime_utc(entry):
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
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


def entry_image_url(entry):
    """Пытается получить изображение прямо из RSS/Atom."""
    for field in ("media_content", "media_thumbnail"):
        values = entry.get(field)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, dict):
                    url = (value.get("url") or "").strip()
                    if url.startswith(("http://", "https://")):
                        return url

    for field in ("enclosures", "links"):
        values = entry.get(field)
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, dict):
                    continue
                media_type = (value.get("type") or "").lower()
                rel = (value.get("rel") or "").lower()
                url = (value.get("href") or value.get("url") or "").strip()
                if url.startswith(("http://", "https://")) and (
                    media_type.startswith("image/") or rel == "enclosure"
                ):
                    return url

    image = entry.get("image")
    if isinstance(image, dict):
        url = (image.get("href") or image.get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            return url

    return ""


def make_candidate(source, category, title, summary, url, published_at, image_url=""):
    title = clean_text(title)
    summary = clean_text(summary)
    normalized = canonical_url(url)
    if not title or not normalized or published_at is None:
        return None
    if is_excluded_topic(title):
        return None
    if not source_matches(source, title, summary):
        return None
    return {
        "category": category,
        "source": source.get("name", "Неизвестный источник"),
        "source_type": source.get("type", "rss"),
        "source_reliability": source.get("reliability", "standard"),
        "language": source.get("language", ""),
        "title": title[:500],
        "summary": summary[:1200],
        "url": url,
        "canonical_url": normalized,
        "published_at": published_at.astimezone(timezone.utc).isoformat(),
        "scope_hint": source.get("trusted_city_scope", ""),
        "image_url": image_url if str(image_url).startswith(("http://", "https://")) else "",
    }


def fetch_rss_candidates(source, category):
    url = source.get("url", "")
    if not url:
        raise ValueError("отсутствует URL")
    response = session.get(url, timeout=RSS_TIMEOUT_SECONDS, allow_redirects=True)
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSS не удалось разобрать: {feed.bozo_exception}")
    result = []
    for entry in feed.entries[:MAX_RSS_ENTRIES_PER_SOURCE]:
        candidate = make_candidate(
            source,
            category,
            entry.get("title", ""),
            entry_summary(entry),
            (entry.get("link", "") or "").strip(),
            entry_datetime_utc(entry),
            entry_image_url(entry),
        )
        if candidate:
            result.append(candidate)
    return result


def telegram_post_title(text):
    text = clean_text(text)
    if not text:
        return ""
    first_line = re.split(r"[\n\r]+", text)[0].strip()
    if len(first_line) < 25:
        first_sentence = re.split(r"(?<=[.!?])\s+", text)[0].strip()
        if len(first_sentence) > len(first_line):
            first_line = first_sentence
    return first_line[:220]


def parse_telegram_page(source, category, page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    candidates = []
    post_ids = []

    for wrap in soup.select("div.tgme_widget_message_wrap"):
        message = wrap.select_one("div.tgme_widget_message")
        if not message:
            continue
        data_post = message.get("data-post", "")
        if "/" not in data_post:
            continue
        channel, post_id_text = data_post.rsplit("/", 1)
        try:
            post_ids.append(int(post_id_text))
        except ValueError:
            pass

        time_node = message.select_one("time[datetime]")
        text_node = message.select_one("div.tgme_widget_message_text")
        if not time_node or not text_node:
            continue

        dt = parse_iso_datetime(time_node.get("datetime"))
        text = clean_text(text_node.get_text(" ", strip=True))
        if dt is None or len(text) < 35:
            continue
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in LOW_VALUE_TELEGRAM_PATTERNS):
            continue

        title = telegram_post_title(text)
        post_url = f"https://t.me/{channel}/{post_id_text}"

        image_url = ""
        photo_node = message.select_one("a.tgme_widget_message_photo_wrap")
        if photo_node:
            style = photo_node.get("style", "")
            match = re.search(r"background-image\s*:\s*url\(['\"]?([^'\")]+)", style, flags=re.IGNORECASE)
            if match:
                image_url = html.unescape(match.group(1)).strip()

        if not image_url:
            thumb_node = message.select_one(".tgme_widget_message_video_thumb")
            if thumb_node:
                style = thumb_node.get("style", "")
                match = re.search(r"background-image\s*:\s*url\(['\"]?([^'\")]+)", style, flags=re.IGNORECASE)
                if match:
                    image_url = html.unescape(match.group(1)).strip()

        candidate = make_candidate(source, category, title, text, post_url, dt, image_url)
        if candidate:
            candidates.append(candidate)

    return candidates, post_ids


def fetch_telegram_candidates(source, category, category_start_utc):
    channel = (source.get("channel") or "").strip().lstrip("@")
    if not channel:
        raise ValueError("у Telegram-источника отсутствует channel")

    max_pages = int(source.get("max_pages", 6))
    url = f"https://t.me/s/{channel}"
    all_candidates = []
    seen_urls = set()

    for _ in range(max_pages):
        response = session.get(url, timeout=HTML_TIMEOUT_SECONDS, allow_redirects=True)
        response.raise_for_status()
        page_candidates, post_ids = parse_telegram_page(source, category, response.text)

        for item in page_candidates:
            if item["canonical_url"] not in seen_urls:
                seen_urls.add(item["canonical_url"])
                all_candidates.append(item)

        if not post_ids:
            break

        oldest_id = min(post_ids)
        oldest_dt = None
        if page_candidates:
            oldest_dt = min(parse_iso_datetime(item["published_at"]) for item in page_candidates)

        if oldest_dt is not None and oldest_dt <= category_start_utc:
            break

        url = f"https://t.me/s/{channel}?before={oldest_id}"
        time.sleep(0.15)

    return all_candidates


def jsonld_values(data, key):
    found = []
    if isinstance(data, dict):
        if key in data:
            found.append(data[key])
        for value in data.values():
            found.extend(jsonld_values(value, key))
    elif isinstance(data, list):
        for value in data:
            found.extend(jsonld_values(value, key))
    return found


def article_metadata(article_url, fallback_title=""):
    response = session.get(article_url, timeout=HTML_TIMEOUT_SECONDS, allow_redirects=True)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    title = ""
    for selector, attr in [
        ('meta[property="og:title"]', "content"),
        ('meta[name="twitter:title"]', "content"),
    ]:
        node = soup.select_one(selector)
        if node and node.get(attr):
            title = clean_text(node.get(attr))
            break
    if not title:
        h1 = soup.find("h1")
        title = clean_text(h1.get_text(" ", strip=True)) if h1 else clean_text(fallback_title)

    summary = ""
    for selector, attr in [
        ('meta[property="og:description"]', "content"),
        ('meta[name="description"]', "content"),
        ('meta[name="twitter:description"]', "content"),
    ]:
        node = soup.select_one(selector)
        if node and node.get(attr):
            summary = clean_text(node.get(attr))
            if summary:
                break

    published_at = None
    for selector, attr in [
        ('meta[property="article:published_time"]', "content"),
        ('meta[name="article:published_time"]', "content"),
        ('meta[name="date"]', "content"),
        ('time[datetime]', "datetime"),
    ]:
        node = soup.select_one(selector)
        if node and node.get(attr):
            published_at = parse_iso_datetime(node.get(attr))
            if published_at:
                break

    if published_at is None:
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or script.get_text())
            except Exception:
                continue
            for value in jsonld_values(data, "datePublished"):
                published_at = parse_iso_datetime(value)
                if published_at:
                    break
            if published_at:
                break

    image_url = ""
    for selector, attr in [
        ('meta[property="og:image"]', "content"),
        ('meta[property="og:image:secure_url"]', "content"),
        ('meta[name="twitter:image"]', "content"),
    ]:
        node = soup.select_one(selector)
        if node and node.get(attr):
            candidate_image = html.unescape(str(node.get(attr))).strip()
            if candidate_image.startswith(("http://", "https://")):
                image_url = candidate_image
                break

    return title, summary, published_at, image_url


def fetch_html_candidates(source, category):
    index_url = source.get("url", "")
    if not index_url:
        raise ValueError("отсутствует URL")
    response = session.get(index_url, timeout=HTML_TIMEOUT_SECONDS, allow_redirects=True)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    include_regex = source.get("include_regex")
    allowed_domains = set(source.get("allowed_domains") or [])
    if not allowed_domains:
        allowed_domains.add(urlsplit(index_url).netloc.lower())

    discovered = []
    seen = set()
    discover_limit = int(source.get("discover_limit", 35))

    selector = source.get("link_selector") or "a[href]"
    for anchor in soup.select(selector):
        href = (anchor.get("href") or "").strip()
        title = clean_text(anchor.get_text(" ", strip=True))
        if len(title) < 25:
            continue
        link = urljoin(index_url, href)
        parsed = urlsplit(link)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in allowed_domains:
            continue
        if include_regex and not re.search(str(include_regex), link, flags=re.IGNORECASE):
            continue
        normalized = canonical_url(link)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        discovered.append((link, title))
        if len(discovered) >= discover_limit:
            break

    result = []
    for link, fallback_title in discovered:
        try:
            title, summary, published_at, image_url = article_metadata(link, fallback_title)
        except Exception as exc:
            print(f"Не удалось прочитать статью {link}: {exc}")
            continue
        if published_at is None and source.get("assume_current_if_no_date"):
            published_at = datetime.now(timezone.utc)
        candidate = make_candidate(source, category, title, summary, link, published_at, image_url)
        if candidate:
            result.append(candidate)
        time.sleep(0.08)
    return result


def fetch_source_candidates(source, category, category_start_utc):
    source_type = (source.get("type") or "rss").lower()
    if source_type == "rss":
        return fetch_rss_candidates(source, category)
    if source_type == "telegram":
        return fetch_telegram_candidates(source, category, category_start_utc)
    if source_type == "html":
        return fetch_html_candidates(source, category)
    raise ValueError(f"неизвестный тип источника: {source_type}")


def collect_candidates_by_category(sources, window_start, window_end, sent_keys):
    result = {category: [] for category in CATEGORY_ORDER}
    source_errors = []
    seen = {category: set() for category in CATEGORY_ORDER}

    end_utc = window_end.astimezone(timezone.utc)
    base_start_utc = window_start.astimezone(timezone.utc)

    for category in CATEGORY_ORDER:
        lookback = timedelta(hours=CATEGORY_LOOKBACK_HOURS[category])
        category_start_utc = min(base_start_utc, end_utc - lookback)

        print(
            f"[{category}] сбор кандидатов: "
            f"{category_start_utc.isoformat()} — {end_utc.isoformat()}"
        )

        for source in sources.get(category, []):
            name = source.get("name", "Неизвестный источник")
            try:
                items = fetch_source_candidates(source, category, category_start_utc)
            except Exception as exc:
                error = f"{CATEGORY_LABELS[category]} / {name}: {exc}"
                source_errors.append(error)
                print(f"Ошибка источника: {error}")
                continue

            accepted = 0
            for item in items:
                published_at = parse_iso_datetime(item.get("published_at"))
                if published_at is None or not (category_start_utc <= published_at < end_utc):
                    continue
                normalized = item["canonical_url"]
                if sent_key(category, normalized) in sent_keys:
                    continue
                if normalized in seen[category]:
                    continue
                seen[category].add(normalized)
                result[category].append(item)
                accepted += 1

            print(f"[{category}] {name}: принято {accepted}")

        result[category].sort(key=lambda item: item["published_at"], reverse=True)
        result[category] = result[category][:MAX_CANDIDATES_PER_CATEGORY]
        print(f"[{category}] всего кандидатов: {len(result[category])}")

    return result, source_errors


def recent_history_for_gemini(sent_news, category):
    same = [item for item in sent_news if item.get("category") == category]
    same.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return [
        {
            "title": item.get("title", ""),
            "source": item.get("source", ""),
            "url": item.get("url", ""),
            "created_at": item.get("created_at", ""),
        }
        for item in same[:MAX_HISTORY_FOR_GEMINI]
    ]


def build_prompt(category, candidates, sent_news, digest_name, window_start, window_end):
    payload = []
    candidate_map = {}
    for index, item in enumerate(candidates, start=1):
        candidate_id = f"{category}-{index}"
        candidate_map[candidate_id] = item
        payload.append(
            {
                "candidate_id": candidate_id,
                "source": item["source"],
                "source_type": item["source_type"],
                "source_reliability": item["source_reliability"],
                "title": item["title"],
                "summary": item["summary"],
                "url": item["url"],
                "published_at": item["published_at"],
                "scope_hint": item.get("scope_hint", ""),
            }
        )

    history = recent_history_for_gemini(sent_news, category)
    start_text = window_start.strftime("%d.%m.%Y %H:%M")
    end_text = window_end.strftime("%d.%m.%Y %H:%M")

    prompt = f"""
Ты выпускающий редактор Telegram-канала «{CHANNEL_NAME}».

Раздел: {CATEGORY_LABELS[category]}
Выпуск: {digest_name}
Основное редакционное окно: {start_text} — {end_text}, Europe/Madrid.
Для достижения полноценной локальной сводки система могла добавить более ранние, но ещё не публиковавшиеся материалы.

Географическая и редакционная задача:
{CATEGORY_SCOPE[category]}

Нужно выбрать РОВНО {ITEMS_PER_CATEGORY} РАЗНЫХ событий, если в списке есть как минимум {ITEMS_PER_CATEGORY} действительно подходящих событий.

Строгие правила:
1. Используй только предоставленные кандидаты. Никаких фактов из памяти или внешних знаний.
2. Не придумывай цифры, имена, обстоятельства, источники или ссылки.
3. Не меняй candidate_id.
4. Не выбирай спорт, шоу-бизнес, кино, театр, концерты, фестивали, музеи и развлекательные материалы.
5. Не выбирай два материала об одном и том же событии. Если несколько источников описывают одно событие — выбери один наиболее информативный.
6. По возможности не бери больше 3 новостей из одного источника, чтобы сводка была разнообразной.
7. Для Telegram-источника с reliability=community не превращай мнение, вопрос подписчика или слух в установленный факт. Выбирай только сообщения с конкретным фактическим событием/объявлением. Если подтверждение ограничено самим каналом, формулируй осторожно: «местный канал сообщает...», «по сообщению канала...». 
8. Для source_type=telegram с scope_hint названием города можно считать коммунальное или уличное сообщение локальным, даже если название города не повторено. Но если в тексте явно указан другой город/район, не относись к нему как к новости данного города.
9. Заголовок на русском: короткий, фактический, без кликбейта.
10. summary_ru: 1–3 коротких предложения, только по фактам кандидата.
11. Если подходящих событий объективно меньше {ITEMS_PER_CATEGORY}, верни меньше, но ничего не выдумывай.
12. События из списка уже опубликованных ниже не повторяй, если это не существенное новое развитие. URL уже отфильтрованы программно; список нужен для смысловой дедупликации.

Верни ТОЛЬКО JSON без markdown:
{{
  "items": [
    {{
      "candidate_id": "world-1",
      "title_ru": "Краткий заголовок",
      "summary_ru": "Краткое фактическое описание."
    }}
  ]
}}

Уже опубликованные события раздела:
{json.dumps(history, ensure_ascii=False)}

Кандидаты:
{json.dumps(payload, ensure_ascii=False)}
"""
    return prompt, candidate_map


def parse_gemini_json(raw):
    raw = (raw or "").strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def select_category_news(category, candidates, sent_news, digest_name, window_start, window_end):
    if not candidates:
        return []

    prompt, candidate_map = build_prompt(
        category, candidates, sent_news, digest_name, window_start, window_end
    )

    best = []
    last_error = None

    for attempt in range(1, GEMINI_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            data = parse_gemini_json(response.text)
            raw_items = data.get("items", []) if isinstance(data, dict) else []
            validated = []
            seen_ids = set()

            for selected in raw_items:
                if not isinstance(selected, dict):
                    continue
                candidate_id = str(selected.get("candidate_id", "")).strip()
                if candidate_id in seen_ids or candidate_id not in candidate_map:
                    continue
                title_ru = clean_text(selected.get("title_ru", ""))
                summary_ru = clean_text(selected.get("summary_ru", ""))
                if not title_ru or not summary_ru:
                    continue
                source_item = candidate_map[candidate_id]
                validated.append(
                    {
                        **source_item,
                        "candidate_id": candidate_id,
                        "title_ru": title_ru[:350],
                        "summary_ru": summary_ru[:1000],
                    }
                )
                seen_ids.add(candidate_id)
                if len(validated) >= ITEMS_PER_CATEGORY:
                    break

            if len(validated) > len(best):
                best = validated
            if len(best) >= ITEMS_PER_CATEGORY or len(candidates) < ITEMS_PER_CATEGORY:
                return best[:ITEMS_PER_CATEGORY]

            # Повторная попытка нужна именно для добора до 10, если кандидатов достаточно.
            prompt += (
                f"\n\nПредыдущий ответ дал только {len(validated)} подходящих элементов. "
                f"В кандидатах есть {len(candidates)} материалов. Пересмотри выбор и, если это возможно без "
                f"выдумывания и дублей, верни ровно {ITEMS_PER_CATEGORY} разных событий."
            )
            time.sleep(1.5)

        except Exception as exc:
            last_error = exc
            print(f"Gemini {category}, попытка {attempt}: {exc}")
            if attempt < GEMINI_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))

    if best:
        return best[:ITEMS_PER_CATEGORY]
    raise RuntimeError(f"Gemini не сформировал раздел {category}: {last_error}")


def build_category_header(category, count):
    label = CATEGORY_LABELS[category]
    return f"<b>{html.escape(label)} — {count} главных новостей</b>"


def build_source_keyboard(item):
    url = (item.get("url") or "").strip()
    if not url:
        return None
    return {
        "inline_keyboard": [
            [{"text": "🔗 Открыть источник", "url": url}]
        ]
    }


def build_news_caption(item, index, total):
    """Полноценный русскоязычный пост; помещается в caption sendPhoto."""
    # Обрезаем ДО HTML-экранирования, чтобы никогда не разрезать HTML entity.
    title_raw = clean_text(item.get("title_ru", ""))[:220]
    summary_raw = clean_text(item.get("summary_ru", ""))[:520]
    source_raw = clean_text(item.get("source", ""))[:80]

    title = html.escape(title_raw)
    summary = html.escape(summary_raw)
    source = html.escape(source_raw)

    parts = [f"<b>{index}/{total}. {title}</b>"]
    if summary:
        parts.append(summary)
    if source:
        parts.append(f"Источник: {source}")

    return "\n\n".join(parts)


def build_news_text(item, index, total):
    title = html.escape(clean_text(item.get("title_ru", ""))[:350])
    summary = html.escape(clean_text(item.get("summary_ru", ""))[:1200])
    source = html.escape(clean_text(item.get("source", ""))[:90])

    parts = [f"<b>{index}/{total}. {title}</b>"]
    if summary:
        parts.append(summary)
    if source:
        parts.append(f"Источник: {source}")
    return "\n\n".join(parts)


def publish_news_item(item, index, total):
    keyboard = build_source_keyboard(item)
    image_url = (item.get("image_url") or "").strip()

    if image_url.startswith(("http://", "https://")):
        try:
            return telegram_send_photo(
                CHANNEL_ID,
                image_url,
                build_news_caption(item, index, total),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as exc:
            print(f"Фото не отправлено, публикую текстом: {exc}")

    return telegram_send(
        CHANNEL_ID,
        build_news_text(item, index, total),
        enable_preview=False,
        parse_mode="HTML",
        reply_markup=keyboard,
    )

def build_digest_header(digest_name, window_start, window_end):
    return (
        f"📰 {CHANNEL_NAME}\n"
        f"{digest_name}\n"
        f"{window_start.strftime('%d.%m %H:%M')} — {window_end.strftime('%d.%m %H:%M')} · Europe/Madrid\n\n"
        f"По 10 главных новостей: Мир · Испания · Украина · Россия · Валенсия · Луганск · Алчевск"
    )


def record_published(sent_news, item, digest_type):
    sent_news.append(
        {
            "url": item["url"],
            "canonical_url": item["canonical_url"],
            "title": item["title_ru"],
            "original_title": item["title"],
            "summary": item["summary_ru"],
            "source": item["source"],
            "category": item["category"],
            "published_at": item["published_at"],
            "digest_type": digest_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    save_sent_news(sent_news)


def main():
    now_local = datetime.now(MADRID_TZ)
    digest_type, digest_name, window_start, window_end = get_digest_window(now_local)

    print(f"{CHANNEL_NAME}: {digest_name}")
    print(f"Основное окно: {window_start.isoformat()} — {window_end.isoformat()}")

    sources = load_sources()
    sent_news = load_sent_news()
    sent_keys = build_sent_keys(sent_news)

    candidates_by_category, source_errors = collect_candidates_by_category(
        sources, window_start, window_end, sent_keys
    )

    selected_by_category = {}
    selection_errors = []

    for category in CATEGORY_ORDER:
        try:
            selected = select_category_news(
                category,
                candidates_by_category[category],
                sent_news,
                digest_name,
                window_start,
                window_end,
            )
            selected_by_category[category] = selected
            print(
                f"{CATEGORY_LABELS[category]}: выбрано {len(selected)}/{ITEMS_PER_CATEGORY} "
                f"из {len(candidates_by_category[category])} кандидатов"
            )
        except Exception as exc:
            selected_by_category[category] = []
            selection_errors.append(f"{CATEGORY_LABELS[category]}: {exc}")
            print(selection_errors[-1])

    total_selected = sum(len(items) for items in selected_by_category.values())
    if total_selected == 0:
        details = "\n".join(selection_errors + source_errors[:10])
        send_admin_alert(f"❌ {CHANNEL_NAME}: сводка не сформирована.\n{details[:3000]}")
        raise SystemExit(1)

    # Заголовок выпуска отдельным сообщением без web preview.
    telegram_send(CHANNEL_ID, build_digest_header(digest_name, window_start, window_end), enable_preview=False)
    time.sleep(TELEGRAM_MESSAGE_DELAY_SECONDS)

    published_by_category = {category: 0 for category in CATEGORY_ORDER}
    telegram_errors = []

    for category in CATEGORY_ORDER:
        items = selected_by_category[category]
        if not items:
            continue

        try:
            telegram_send(
                CHANNEL_ID,
                build_category_header(category, len(items)),
                enable_preview=False,
                parse_mode="HTML",
            )
            time.sleep(TELEGRAM_MESSAGE_DELAY_SECONDS)
        except Exception as exc:
            error = f"{CATEGORY_LABELS[category]} / заголовок раздела: {exc}"
            telegram_errors.append(error)
            print(f"Ошибка Telegram: {error}")

        for index, item in enumerate(items, start=1):
            try:
                publish_news_item(item, index, len(items))
                published_by_category[category] += 1
                record_published(sent_news, item, digest_type)
                time.sleep(TELEGRAM_MESSAGE_DELAY_SECONDS)
            except Exception as exc:
                error = f"{CATEGORY_LABELS[category]} / {item.get('title_ru', '')[:80]}: {exc}"
                telegram_errors.append(error)
                print(f"Ошибка Telegram: {error}")

    missing = [
        f"{CATEGORY_LABELS[category]} {published_by_category[category]}/{ITEMS_PER_CATEGORY}"
        for category in CATEGORY_ORDER
        if published_by_category[category] < ITEMS_PER_CATEGORY
    ]

    if missing or telegram_errors or selection_errors:
        parts = [f"⚠️ {CHANNEL_NAME}: {digest_name.lower()} завершена с предупреждениями."]
        if missing:
            parts.append("Меньше 10 новостей: " + ", ".join(missing))
        if telegram_errors:
            parts.append(f"Ошибок Telegram: {len(telegram_errors)}")
        if selection_errors:
            parts.append(f"Ошибок отбора: {len(selection_errors)}")
        send_admin_alert("\n".join(parts))

    print("Итог:")
    for category in CATEGORY_ORDER:
        print(f"  {CATEGORY_LABELS[category]}: {published_by_category[category]}/{ITEMS_PER_CATEGORY}")

    # Ошибки отдельных источников считаем диагностикой, а не падением всей сводки.
    if source_errors:
        print("Ошибки источников:")
        for error in source_errors:
            print(f"  - {error}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL: {exc}")
        send_admin_alert(f"❌ {CHANNEL_NAME}: критическая ошибка\n{type(exc).__name__}: {exc}")
        raise
