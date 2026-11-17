import hashlib
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from email.utils import parsedate_to_datetime

try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:
    gnewsdecoder = None

import feedparser
import requests

BASE = Path(__file__).resolve().parent
ARCHIVE = BASE / "news.json"
MAX_NEW = 10
MAX_ARCHIVE = 1000
# Bestehende Meldungen schrittweise auf Bilder prüfen, damit der tägliche Lauf schnell bleibt.
MAX_IMAGE_BACKFILL = 4
REQUEST_TIMEOUT = 12
# Fehlgeschlagene Bildprüfungen frühestens nach sieben Tagen wiederholen.
IMAGE_RETRY_DAYS = 3
# Google News nicht mit vielen Dekodierungsanfragen belasten.
GOOGLE_REQUEST_PAUSE = 3
GOOGLE_COOLDOWN_SECONDS = 24 * 3600
GOOGLE_COOLDOWN_FILE = BASE / '.google_news_cooldown.json'

FEEDS = [
    ("https://news.google.com/rss/search?q=Camping+Schweiz&hl=de&gl=CH&ceid=CH:de", "Schweiz"),
    ("https://news.google.com/rss/search?q=Camping+Europa&hl=de&gl=DE&ceid=DE:de", "Europa"),
    ("https://news.google.com/rss/search?q=Wohnmobil+Camping+News&hl=de&gl=CH&ceid=CH:de", "Allgemein"),
    ("https://news.google.com/rss/search?q=Campingplatz+Nachrichten&hl=de&gl=CH&ceid=CH:de", "Allgemein"),
]
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; CampingKompassNews/1.1)"})
# Externe HTTP-Bibliotheken sollen das Actions-Protokoll nicht mit Einzelanfragen füllen.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

google_blocked = False
last_google_request = 0.0


def google_cooldown_active():
    """Ein 429 pausiert weitere Google-News-Anfragen auch über Workflow-Läufe hinweg,
    sofern die kleine Statusdatei durch den bestehenden Git-Commit mitgespeichert wird."""
    try:
        data = json.loads(GOOGLE_COOLDOWN_FILE.read_text(encoding="utf-8"))
        until = datetime.fromisoformat(data["until"])
        return datetime.now(timezone.utc) < until
    except (OSError, ValueError, KeyError, TypeError):
        return False


def note_google_rate_limit():
    global google_blocked
    google_blocked = True
    until = datetime.fromtimestamp(time.time() + GOOGLE_COOLDOWN_SECONDS, timezone.utc)
    GOOGLE_COOLDOWN_FILE.write_text(
        json.dumps({"until": until.isoformat()}, indent=2), encoding="utf-8"
    )
    logging.warning("Google News antwortet mit HTTP 429: weitere Google-Anfragen "
                    "für diesen Lauf gestoppt; frühestens nach 24 Stunden erneut.")


def google_request_gate():
    """Gibt False zurück, sobald ein Google-News-Limit bekannt ist."""
    global last_google_request
    if google_blocked or google_cooldown_active():
        return False
    delay = GOOGLE_REQUEST_PAUSE - (time.monotonic() - last_google_request)
    if delay > 0:
        time.sleep(delay)
    last_google_request = time.monotonic()
    return True



def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def clean_url(url):
    p = urlsplit((url or "").strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    query = [(k, v) for k, v in parse_qsl(p.query)
             if not k.lower().startswith("utm_") and k.lower() not in ("gclid", "fbclid")]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urlencode(query), ""))


def normalize_title(title):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", title.casefold())).strip()


def is_duplicate(item, existing):
    url = item["source_url"]
    title = normalize_title(item["title"])
    for old in existing:
        if clean_url(old.get("source_url")) == url:
            return True
        old_title = normalize_title(old.get("title", ""))
        if title and old_title and SequenceMatcher(None, title, old_title).ratio() >= 0.88:
            return True
    return False


def load_archive():
    if not ARCHIVE.exists():
        return []
    data = json.loads(ARCHIVE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("news.json muss eine JSON-Liste enthalten.")
    return data


def get_date(entry):
    return (entry.get("published") or entry.get("updated") or "").strip()


def date_key(article):
    # Niemals published_at als primären Sortierschlüssel benutzen: das ist nur der Importzeitpunkt.
    value = article.get("source_published") or ""
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError):
            return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def valid_image_url(value, base=""):
    if not isinstance(value, str) or not value.strip():
        return ""
    candidate = urljoin(base, html.unescape(value.strip()))
    parsed = urlsplit(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    # Logos, Pixel und Social-Icons sind keine Artikelbilder.
    if re.search(r"(?:favicon|sprite|tracking|pixel|logo|avatar|icon)[./_?-]", parsed.path, re.I):
        return ""
    return candidate


class ImageMetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []
        self.canonical = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"):
                self.images.append(a.get("content", ""))
        elif tag == "link":
            if "canonical" in (a.get("rel") or "").lower().split():
                self.canonical = a.get("href", "")


def rss_image(entry):
    # Nur tatsächlich zur RSS-Meldung gehörende Medienfelder auslesen.
    for media in entry.get("media_content", []) + entry.get("media_thumbnail", []):
        if isinstance(media, dict):
            url = valid_image_url(media.get("url", ""))
            if url and (not media.get("type") or str(media["type"]).startswith("image/")):
                return url
    for enclosure in entry.get("enclosures", []):
        if isinstance(enclosure, dict) and str(enclosure.get("type", "")).startswith("image/"):
            url = valid_image_url(enclosure.get("href") or enclosure.get("url"))
            if url:
                return url
    # RSS-Auszüge können ein eingebettetes Originalbild enthalten.
    summary = entry.get("summary", "") or entry.get("description", "")
    match = re.search(r"<img\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)", summary, re.I)
    return valid_image_url(match.group(1)) if match else ""


def resolve_original_url(url):
    """Google-News-RSS-Link sparsam zur Verlagsseite auflösen."""
    host = (urlsplit(url).hostname or "").lower()
    if host not in ("news.google.com", "www.news.google.com"):
        return url
    if not google_request_gate():
        return ""

    if gnewsdecoder is not None:
        try:
            result = gnewsdecoder(url, interval=0)
            decoded = result.get("decoded_url", "") if isinstance(result, dict) else ""
            if clean_url(decoded) and (urlsplit(decoded).hostname or "").lower() not in (
                "news.google.com", "www.news.google.com"
            ):
                return decoded
            # Einige Bibliotheksversionen melden HTTP-Fehler im Rückgabewert.
            if "429" in str(result):
                note_google_rate_limit()
        except Exception as exc:
            if "429" in str(exc):
                note_google_rate_limit()
            else:
                logging.info("Google-News-Link nicht dekodiert: %s", str(exc)[:160])
    else:
        logging.warning("googlenewsdecoder fehlt: bitte requirements.txt prüfen.")

    # Keine zweite Google-Anfrage als Fallback: diese hat vorher die Limits verstärkt.
    return ""


def page_image(article_url, known_original_url=""):
    """Liest das tatsächliche Artikel-Hauptbild aus Open-Graph-Metadaten."""
    original_url = known_original_url or resolve_original_url(article_url)
    if not original_url:
        return "", ""
    try:
        response = SESSION.get(original_url, timeout=REQUEST_TIMEOUT, allow_redirects=True,
                               headers={"Accept": "text/html,application/xhtml+xml"})
        response.raise_for_status()
        if "text/html" not in response.headers.get("Content-Type", "").lower():
            return "", original_url
        final_host = (urlsplit(response.url).hostname or "").lower()
        if final_host in ("news.google.com", "www.news.google.com"):
            return "", ""
        parser = ImageMetaParser()
        parser.feed(response.text[:500000])
        for candidate in parser.images:
            image_url = valid_image_url(candidate, response.url)
            if image_url:
                return image_url, response.url
    except (requests.RequestException, UnicodeError, ValueError) as exc:
        logging.info("Artikelbild nicht erreichbar: %s (%s)", original_url, exc)
    return "", original_url


def make_article(entry, region):
    title = clean_text(entry.get("title", ""))
    url = clean_url(entry.get("link", ""))
    if not title or not url:
        return None
    excerpt = clean_text(entry.get("summary", "") or entry.get("description", ""))[:320]
    article_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    image_url = rss_image(entry)
    original_url = ""
    if not image_url:
        image_url, original_url = page_image(url)
    article = {
        "id": article_id,
        "title": title,
        "excerpt": excerpt or "Lies die vollständige Meldung bei der Originalquelle.",
        "content": [],
        "category": "News",
        "region": region,
        "tags": ["Camping", region],
        "source_url": url,
        "source_title": "Originalmeldung bei Google News",
        "source_published": get_date(entry),
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    if image_url:
        article["image_url"] = image_url
    if original_url:
        article["original_url"] = original_url
    # Eine Bildadresse ist keine urheberrechtliche Nutzungsfreigabe.
    return article


def main():
    existing = load_archive()
    known = list(existing)
    if google_cooldown_active():
        logging.warning("Google-News-Pause aktiv. Bestehendes Archiv bleibt erhalten.")
    new_articles = []

    for feed_url, region in FEEDS:
        if not google_request_gate():
            break
        try:
            response = SESSION.get(feed_url, timeout=25)
            if response.status_code == 429:
                note_google_rate_limit()
                break
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            for entry in feed.entries:
                # Duplikate vor dem Abrufen der Bildmetadaten prüfen.
                title = clean_text(entry.get("title", ""))
                url = clean_url(entry.get("link", ""))
                if not title or not url or is_duplicate({"title": title, "source_url": url}, known):
                    continue
                article = make_article(entry, region)
                if article:
                    new_articles.append(article)
                    known.append(article)
                if len(new_articles) >= MAX_NEW:
                    break
        except Exception:
            logging.exception("Feed konnte nicht geladen werden: %s", feed_url)
        if len(new_articles) >= MAX_NEW:
            break

    # Alte Nachrichten schrittweise nachtragen; Fehlschläge später erneut versuchen.
    checked = 0
    now = datetime.now(timezone.utc)
    for article in existing:
        if checked >= MAX_IMAGE_BACKFILL or google_blocked:
            break
        if article.get("image_url"):
            continue
        last_check = article.get("image_last_checked")
        if last_check:
            try:
                dt = datetime.fromisoformat(last_check.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (now - dt).total_seconds() < IMAGE_RETRY_DAYS * 86400:
                    continue
            except (ValueError, TypeError):
                pass
        # Ohne bereits bekannte Verlagsadresse keine erneute Anfrage während der Pause.
        if google_cooldown_active() and not article.get("original_url"):
            break
        image_url, original_url = page_image(
            article.get("source_url", ""), article.get("original_url", "")
        )
        if google_blocked and not original_url:
            break
        checked += 1
        article["image_last_checked"] = now.isoformat()
        if original_url:
            article["original_url"] = original_url
        if image_url:
            article["image_url"] = image_url
            logging.info("Originalbild gefunden: %s", article.get("title", "")[:75])

    combined = new_articles + existing
    unique = []
    seen = set()
    for article in combined:
        url = clean_url(article.get("source_url"))
        if not url or url in seen:
            continue
        seen.add(url)
        unique.append(article)

    unique.sort(key=date_key, reverse=True)
    unique = unique[:MAX_ARCHIVE]
    # Auch ohne neue Meldungen speichern: Reihenfolge und Bildnachträge aktualisieren.
    if unique == load_archive():
        logging.info("Keine Änderungen; Archiv bleibt unverändert.")
        return
    temp = ARCHIVE.with_suffix(".tmp")
    temp.write_text(json.dumps(unique, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(ARCHIVE)
    logging.info("%s neue Meldungen; %s ältere Meldungen auf Bilder geprüft; Archiv chronologisch sortiert.",
                 len(new_articles), checked)


if __name__ == "__main__":
    main()
