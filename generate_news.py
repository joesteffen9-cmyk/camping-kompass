import hashlib
import html
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from email.utils import parsedate_to_datetime

import feedparser
import requests

BASE = Path(__file__).resolve().parent
ARCHIVE = BASE / "news.json"
MAX_NEW = 50
MAX_ARCHIVE = 10000
MAX_VISIBLE = 50
VISIBLE_AGE_DAYS = 90
# Bestehende Meldungen schrittweise auf Bilder prüfen, damit der tägliche Lauf schnell bleibt.
MAX_IMAGE_BACKFILL = 4
REQUEST_TIMEOUT = 8
FEED_TIMEOUT = 10
FEED_WORKERS = 6
# Fehlgeschlagene Bildprüfungen frühestens nach sieben Tagen wiederholen.
IMAGE_RETRY_DAYS = 3
# Direkte RSS-Feeds; weitere geprüfte Quellen können hier ergänzt werden.
# Nur erreichbare Quellen aus dem letzten GitHub-Test beibehalten.
FEEDS = [
    # Bestehende Quellen bleiben erhalten.
    ("https://www.promobil.de/rss/news", "Europa", "promobil"),
    ("https://www.promobil.de/rss/ratgeber", "Europa", "promobil"),
    ("https://www.camping-news.de/rss/", "Europa", "Camping-News"),
    ("https://www.camping-news.de/rss/campingplaetze/", "Europa", "Camping-News"),
    ("https://www.camping-news.de/rss/touristik/", "Europa", "Camping-News"),
    # Weitere europäische Fachmedien werden bei JEDEM Lauf geprüft.
    # Ein vorübergehend ungültiger Feed wird übersprungen.
    # Nicht alle Anbieter garantieren dauerhaft öffentliche RSS-Feeds.
    ("https://www.camperprofessional.de/feed/", "Europa", "Camper Professional"),
    ("https://www.lemondeducampingcar.fr/feed/", "Frankreich", "Le Monde du Camping-Car"),
    ("https://www.vanlifemag.fr/feed/", "Frankreich", "Van Life Magazine"),
    ("https://www.fourgonlesite.com/feed/", "Frankreich", "Fourgon le Site"),
    # Neue RSS-Kandidaten: Erreichbarkeit und Eintraege im GitHub-Protokoll kontrollieren.
    ("https://caravanecamper.it/feed/", "Italien", "Caravan e Camper"),
    ("https://www.camperpress.info/feed/", "Italien", "Camper Press"),
    ("https://www.furgosfera.com/feed/", "Spanien", "Furgosfera"),
    ("https://www.practicalmotorhome.com/feed", "Europa", "Practical Motorhome"),
    ("https://www.caravanguard.co.uk/news/feed/", "Europa", "Caravan Guard"),
    ("https://fpautocaravanismo.pt/feed/", "Portugal", "Federacao Autocaravanismo"),
]

SOURCE_LANGUAGES = {
    "promobil": "de", "Camping-News": "de", "Camper Professional": "de",
    "Le Monde du Camping-Car": "fr", "Van Life Magazine": "fr", "Fourgon le Site": "fr",
    "Caravan e Camper": "it", "Camper Press": "it",
    "Furgosfera": "es",
    "Practical Motorhome": "en", "Caravan Guard": "en",
    "Federacao Autocaravanismo": "pt",
}

# Bei älteren Archivmeldungen ist die Sprache noch nicht gespeichert.
# Bekannte Quellen werden sicher zugeordnet; unbekannte bleiben "und".
def archive_language(article):
    publisher = article.get("source_title", "")
    if publisher in SOURCE_LANGUAGES:
        return SOURCE_LANGUAGES[publisher]
    if publisher == "Originalmeldung bei Google News":
        title = (article.get("title") or "").casefold()
        # Nur klare deutsche Funktionswörter, keine Vermutung aus der Region.
        if re.search(r"\\b(?:der|die|das|und|für|mit|auf|bei|warum|wird|werden|neue|neuen|einem|einer|über|gegen|nicht)\\b", title):
            return "de"
    return "und"


def fetch_feed(spec):
    feed_url, region, publisher = spec
    try:
        # Pro Thread eigene Anfrage statt einer gemeinsam genutzten Session.
        response = requests.get(
            feed_url, timeout=FEED_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CampingKompassNews/1.2)"},
        )
        response.raise_for_status()
        return spec, feedparser.parse(response.content), None
    except (requests.RequestException, ValueError) as exc:
        return spec, None, str(exc)


# Keine alten Beiträge aus längst vergangenen Jahren neu importieren.
MAX_AGE_DAYS = 14
# Neue Artikel ohne nachweisbar erreichbares Bild werden nicht importiert.
REQUIRE_IMAGE_FOR_NEW = True

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; CampingKompassNews/1.1)"})
# Externe HTTP-Bibliotheken sollen das Actions-Protokoll nicht mit Einzelanfragen füllen.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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
        if url and clean_url(old.get("source_url")) == url:
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


def page_image(article_url, known_original_url=""):
    """Liest das tatsächliche Artikel-Hauptbild aus Open-Graph-Metadaten."""
    original_url = known_original_url or article_url
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


def image_is_reachable(url):
    """Prüft, ob eine echte Bildressource statt HTML/Fehlerseite geliefert wird."""
    if not valid_image_url(url):
        return False
    try:
        with SESSION.get(url, timeout=REQUEST_TIMEOUT, stream=True,
                         headers={"Accept": "image/avif,image/webp,image/*"}) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
            return content_type.startswith("image/") and content_type != "image/svg+xml"
    except requests.RequestException:
        return False


# Konservative Zuordnung: Nur eindeutige Begriffe verwenden.
# Bei Unsicherheit bleibt die Meldung unter "News" bzw. "Europa" sichtbar.
TOPIC_RULES = [
    ("Warnungen & Sicherheit", ("rückruf", "recall", "unwetter", "waldbrand", "hochwasser", "überschwemmung", "warnung", "brandgefahr", "sperrung")),
    ("Verkehr & Maut", ("maut", "vignette", "verkehrsregel", "tempolimit", "umweltzone", "autobahn", "führerschein", "parkverbot", "einreisebestimmung")),
    ("Campingplätze", ("campingplatz", "campingplätze", "stellplatz", "campingpark", "camping resort", "camping village")),
    ("Wohnmobile & Wohnwagen", ("wohnmobil", "wohnwagen", "reisemobil", "campervan", "kastenwagen", "fahrzeugmodell")),
    ("Campingtechnik & Zubehör", ("solaranlage", "solarstrom", "powerstation", "campingzubehör", "campingausrüstung", "batterie", "wechselrichter")),
    ("Reiseziele & Tourismus", ("reiseziel", "tourismus", "reiseregion", "sehenswürdigkeit", "urlaubsregion")),
    ("Campingbranche & Veranstaltungen", ("campingmesse", "caravan salon", "fachmesse", "campingbranche", "campingverband", "campingfestival")),
]
COUNTRY_RULES = [
    ("Schweiz", ("schweiz", "schweizer", "tessin", "graubünden", "wallis")),
    ("Deutschland", ("deutschland", "deutsch", "bayern", "schwarzwald")),
    ("Österreich", ("österreich", "tirol", "kärnten", "salzburg")),
    ("Italien", ("italien", "italienisch", "gardasee", "südtirol", "toskana")),
    ("Spanien", ("spanien", "spanisch", "katalonien", "andalusien")),
    ("Frankreich", ("frankreich", "französisch", "provence", "bretagne")),
    ("Portugal", ("portugal", "portugiesisch", "algarve")),
]

def classify_article(title, excerpt, default_region="Europa"):
    text = (title + " " + excerpt).casefold()
    category = next((name for name, words in TOPIC_RULES if any(w in text for w in words)), "News")
    countries = [name for name, words in COUNTRY_RULES if any(w in text for w in words)]
    region = countries[0] if len(countries) == 1 else default_region
    return category, region


def make_article(entry, region, publisher):
    title = clean_text(entry.get("title", ""))
    url = clean_url(entry.get("link", ""))
    if not title or not url:
        return None
    excerpt = clean_text(entry.get("summary", "") or entry.get("description", ""))[:320]
    article_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    image_url = rss_image(entry)
    original_url = ""
    if not image_url:
        # Ein einzelner Abruf der Originalseite für das Open-Graph-Bild.
        image_url, original_url = page_image(url)
    if REQUIRE_IMAGE_FOR_NEW and (not image_url or not image_is_reachable(image_url)):
        logging.info("Meldung ohne abrufbares Hauptbild übersprungen: %s", title[:90])
        return None
    category, detected_region = classify_article(title, excerpt, region)
    article = {
        "id": article_id,
        "title": title,
        "excerpt": excerpt or "Lies die vollständige Meldung bei der Originalquelle.",
        "content": [],
        "category": category,
        "region": detected_region,
        "tags": ["Camping", detected_region],
        "source_url": url,
        "source_title": publisher,
        "language": SOURCE_LANGUAGES.get(publisher, "und"),
        "source_published": get_date(entry),
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    if image_url:
        article["image_url"] = image_url
    if original_url:
        article["original_url"] = original_url
    # Bildrechte werden bewusst NICHT automatisch behauptet.
    # Die Website zeigt ohne Freigabe weiterhin ihre eigene Illustration.
    return article


def main():
    existing = load_archive()
    language_updates = 0
    for article in existing:
        if not article.get("language"):
            article["language"] = archive_language(article)
            language_updates += 1
    logging.info("Sprachkennung bei %s älteren Meldungen ergänzt (und = unbestimmt).", language_updates)
    known = list(existing)
    new_articles = []

    # Alle Quellen zuerst einsammeln; erst DANACH die neuesten Artikel auswählen.
    # So verdrängt die erste Quelle nicht alle übrigen Quellen.
    candidates = []
    diagnostics = {"duplicates": 0, "missing_date": 0, "too_old_or_invalid": 0, "eligible": 0}
    # Feed-Abfragen parallel: eine langsame Quelle blockiert nicht alle anderen.
    with ThreadPoolExecutor(max_workers=FEED_WORKERS) as pool:
        feed_results = list(pool.map(fetch_feed, FEEDS))
    for (feed_url, region, publisher), feed, error in feed_results:
        if error:
            logging.warning("RSS-Feed nicht erreichbar: %s (%s)", feed_url, error)
            continue
        logging.info("RSS-Quelle %s [%s]: %s Einträge gefunden (%s).",
                     publisher, SOURCE_LANGUAGES.get(publisher, "und"), len(feed.entries), feed_url)
        if not feed.entries and getattr(feed, "bozo", False):
            logging.warning("Ungültiger RSS/Atom-Feed: %s (%s)", feed_url,
                            getattr(feed, "bozo_exception", "unbekannt"))
        if not feed.entries:
            logging.warning("RSS-Feed liefert keine Einträge: %s", feed_url)
        source_stats = {"duplicate": 0, "old": 0, "missing_date": 0, "eligible": 0}
        for entry in feed.entries:
            title = clean_text(entry.get("title", ""))
            url = clean_url(entry.get("link", ""))
            if not title or not url:
                continue
            if is_duplicate({"title": title, "source_url": url}, known):
                diagnostics["duplicates"] += 1
                source_stats["duplicate"] += 1
                continue
            published = get_date(entry)
            if not published:
                diagnostics["missing_date"] += 1
                source_stats["missing_date"] += 1
                continue
            published_date = date_key({"source_published": published})
            age = datetime.now(timezone.utc) - published_date
            if published_date.year < 2000 or age.total_seconds() < -86400 or age.days > MAX_AGE_DAYS:
                diagnostics["too_old_or_invalid"] += 1
                source_stats["old"] += 1
                continue
            diagnostics["eligible"] += 1
            source_stats["eligible"] += 1
            candidates.append((published_date, entry, region, publisher))
        logging.info("RSS-Diagnose %s [%s]: %s", publisher,
                     SOURCE_LANGUAGES.get(publisher, "und"), source_stats)

    logging.info("RSS-Prüfung abgeschlossen: %s Quellen, %s neue Kandidaten.",
                 len(FEEDS), len(candidates))
    logging.info("RSS-Diagnose: %s", diagnostics)
    candidates.sort(key=lambda item: item[0], reverse=True)
    # Fairer Sprachmix bei max. 50 Meldungen; je Sprache neueste zuerst.
    from collections import deque
    buckets = {}
    for candidate in candidates:
        lang = SOURCE_LANGUAGES.get(candidate[3], "und")
        buckets.setdefault(lang, deque()).append(candidate)
    language_order = ("de", "fr", "it", "es", "en", "pt", "und")
    balanced_candidates = []
    while any(buckets.values()):
        for lang in language_order:
            if buckets.get(lang):
                balanced_candidates.append(buckets[lang].popleft())
    for _, entry, region, publisher in balanced_candidates:
        title = clean_text(entry.get("title", ""))
        url = clean_url(entry.get("link", ""))
        if is_duplicate({"title": title, "source_url": url}, known):
            continue
        article = make_article(entry, region, publisher)
        if article:
            new_articles.append(article)
            known.append(article)
        if len(new_articles) >= MAX_NEW:
            break

    # Alte Nachrichten schrittweise nachtragen; Fehlschläge später erneut versuchen.
    checked = 0
    now = datetime.now(timezone.utc)
    for article in existing:
        if checked >= MAX_IMAGE_BACKFILL:
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
        # Historische Google-News-Links nicht erneut dekodieren oder abfragen.
        host = (urlsplit(article.get("original_url") or article.get("source_url", "")).hostname or "").lower()
        if host in ("news.google.com", "www.news.google.com"):
            continue
        image_url, original_url = page_image(
            article.get("source_url", ""), article.get("original_url", "")
        )
        checked += 1
        article["image_last_checked"] = now.isoformat()
        if original_url:
            article["original_url"] = original_url
        if image_url:
            article["image_url"] = image_url
            logging.info("Originalbild gefunden: %s", article.get("title", "")[:75])

    # Das bestehende news.json bleibt das einzige Nachrichtenarchiv.
    # Achtung: Bei mehreren tausend Einträgen sollte das Frontend später
    # auf getrennte, paginierte Archivdateien umgestellt werden.
    # Ohne neue Nachrichten werden alte Einträge NICHT wegen ihres Alters gelöscht.
    # Sobald neue Nachrichten hinzukommen, begrenzen wir das Archiv auf 10.000.
    combined = new_articles + existing
    deduplicated = []
    for article in combined:
        if not article.get("source_url"):
            continue
        if is_duplicate(article, deduplicated):
            continue
        deduplicated.append(article)
    deduplicated.sort(key=date_key, reverse=True)
    combined = deduplicated[:MAX_ARCHIVE] if new_articles else deduplicated
    # Die Website sollte höchstens 50 Artikel anzeigen, vorrangig <=90 Tage.
    # Diese Anzeigefilterung erfolgt im Frontend; das Archiv bleibt vollständig.
    if combined == load_archive():
        logging.info("Keine Änderungen; Archiv bleibt unverändert.")
        return
    temp = ARCHIVE.with_suffix(".tmp")
    temp.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(ARCHIVE)
    logging.info("%s neue Meldungen mit Bild; %s ältere Meldungen auf Bilder geprüft; nach Quellendatum sortiert.",
                 len(new_articles), checked)


if __name__ == "__main__":
    main()
