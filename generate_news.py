
import hashlib
import html
import json
import logging
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import feedparser
import requests

BASE = Path(__file__).resolve().parent
ARCHIVE = BASE / "news.json"
MAX_NEW = 10
MAX_ARCHIVE = 1000

FEEDS = [
    (
        "https://news.google.com/rss/search?q=Camping+Schweiz"
        "&hl=de&gl=CH&ceid=CH:de",
        "Schweiz",
    ),
    (
        "https://news.google.com/rss/search?q=Camping+Europa"
        "&hl=de&gl=DE&ceid=DE:de",
        "Europa",
    ),
    (
        "https://news.google.com/rss/search?q=Wohnmobil+Camping+News"
        "&hl=de&gl=CH&ceid=CH:de",
        "Allgemein",
    ),
    (
        "https://news.google.com/rss/search?q=Campingplatz+Nachrichten"
        "&hl=de&gl=CH&ceid=CH:de",
        "Allgemein",
    ),
]

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def clean_url(url):
    p = urlsplit((url or "").strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""

    query = [
        (k, v) for k, v in parse_qsl(p.query)
        if not k.lower().startswith("utm_")
        and k.lower() not in ("gclid", "fbclid")
    ]
    return urlunsplit((
        p.scheme.lower(), p.netloc.lower(),
        p.path.rstrip("/"), urlencode(query), ""
    ))


def normalize_title(title):
    return re.sub(r"\s+", " ", re.sub(
        r"[^\w\s]", " ", title.casefold()
    )).strip()


def is_duplicate(item, existing):
    url = item["source_url"]
    title = normalize_title(item["title"])

    for old in existing:
        if clean_url(old.get("source_url")) == url:
            return True

        old_title = normalize_title(old.get("title", ""))
        if title and old_title:
            if SequenceMatcher(None, title, old_title).ratio() >= 0.88:
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
    value = entry.get("published") or entry.get("updated") or ""
    return value.strip()


def make_article(entry, region):
    title = clean_text(entry.get("title", ""))
    url = clean_url(entry.get("link", ""))

    if not title or not url:
        return None

    # Nur den RSS-Auszug verwenden, keinen ganzen Fremdartikel kopieren.
    excerpt = clean_text(
        entry.get("summary", "") or entry.get("description", "")
    )[:320]

    article_id = hashlib.sha256(
        url.encode("utf-8")
    ).hexdigest()[:20]

    return {
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


def main():
    existing = load_archive()
    known = list(existing)
    new_articles = []

    for feed_url, region in FEEDS:
        try:
            response = requests.get(
                feed_url,
                timeout=25,
                headers={"User-Agent": "CampingKompass/1.0"}
            )
            response.raise_for_status()
            feed = feedparser.parse(response.content)

            for entry in feed.entries:
                article = make_article(entry, region)
                if not article or is_duplicate(article, known):
                    continue

                new_articles.append(article)
                known.append(article)

                if len(new_articles) >= MAX_NEW:
                    break

        except Exception:
            logging.exception("Feed konnte nicht geladen werden: %s", feed_url)

        if len(new_articles) >= MAX_NEW:
            break

    if not new_articles:
        logging.info("Keine neuen Artikel gefunden; Archiv bleibt unverändert.")
        return

    combined = new_articles + existing
    unique = []
    seen = set()

    for article in combined:
        url = clean_url(article.get("source_url"))
        if not url or url in seen:
            continue
        seen.add(url)
        unique.append(article)

    temp = ARCHIVE.with_suffix(".tmp")
    temp.write_text(
        json.dumps(unique[:MAX_ARCHIVE], ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    temp.replace(ARCHIVE)

    logging.info("%s neue Links gespeichert.", len(new_articles))


if __name__ == "__main__":
    main()
