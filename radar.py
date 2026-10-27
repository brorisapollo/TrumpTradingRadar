import os
import json
import re
import html
import hashlib
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

# ==========================================
# TRUMP TRADING RADAR
# Marktübergreifendes Frühwarnsystem
# ==========================================

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
FEED_URL = os.environ["TRUMP_FEED_URL"]

STATE_FILE = Path("seen_posts.json")

# ==========================================
# MARKTKATEGORIEN
# ==========================================

MARKETS = {

    "ÖL & ENERGIE": [
        "oil", "crude", "brent", "wti",
        "opec", "petroleum", "energy",
        "gas", "pipeline", "drilling"
    ],

    "GOLD & ROHSTOFFE": [
        "gold", "silver", "copper",
        "metal", "mining", "commodity",
        "uranium", "lithium"
    ],

    "TECHNOLOGIE & KI": [
        "nvidia", "amd", "apple",
        "microsoft", "google", "alphabet",
        "amazon", "meta", "tesla",
        "semiconductor", "chip",
        "artificial intelligence",
        "technology", "export control"
    ],

    "AKTIEN & BÖRSEN": [
        "stock", "market", "wall street",
        "nasdaq", "dow jones",
        "s&p", "shares", "corporation",
        "earnings", "company"
    ],

    "ZÖLLE & HANDEL": [
        "tariff", "trade", "import",
        "export", "china", "eu",
        "europe", "canada", "mexico",
        "trade deal", "trade war"
    ],

    "ZINSEN & ANLEIHEN": [
        "federal reserve", "fed",
        "powell", "interest rate",
        "inflation", "treasury",
        "bond", "yield", "debt"
    ],

    "WÄHRUNGEN": [
        "dollar", "currency",
        "euro", "yuan", "yen",
        "exchange rate", "forex"
    ],

    "KRYPTOWÄHRUNGEN": [
        "bitcoin", "crypto",
        "ethereum", "blockchain",
        "digital asset", "stablecoin"
    ],

    "GEOPOLITIK": [
        "iran", "israel", "russia",
        "ukraine", "taiwan",
        "war", "sanction",
        "military", "attack",
        "peace deal", "nuclear"
    ],

    "INDUSTRIE & VERTEIDIGUNG": [
        "boeing", "lockheed",
        "defense", "defence",
        "aircraft", "automotive",
        "car manufacturer",
        "steel", "manufacturing"
    ]
}

# ==========================================
# TELEGRAM
# ==========================================

def send_telegram(message):

    url = (
        f"https://api.telegram.org/"
        f"bot{TOKEN}/sendMessage"
    )

    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message[:4000],
        "disable_web_page_preview": "true"
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        method="POST"
    )

    with urllib.request.urlopen(
        request,
        timeout=20
    ) as response:
        response.read()


# ==========================================
# RSS-BEITRÄGE ABRUFEN
# ==========================================

def get_posts():

    request = urllib.request.Request(
        FEED_URL,
        headers={
            "User-Agent": "TrumpTradingRadar/1.0"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=30
    ) as response:
        xml_data = response.read()

    root = ET.fromstring(xml_data)

    posts = []

    for item in root.findall(".//item"):

        title = item.findtext("title", "")
        description = item.findtext(
            "description", ""
        )

        link = item.findtext("link", "")
        guid = item.findtext("guid", "")
        published = item.findtext(
            "pubDate", ""
        )

        content = title + " " + description

        content = re.sub(
            r"<[^>]+>", " ", content
        )

        content = html.unescape(content)
        content = re.sub(
            r"\s+", " ", content
        ).strip()

        post_id = guid or link

        if not post_id:
            post_id = hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()

        posts.append({
            "id": post_id,
            "text": content,
            "link": link,
            "published": published
        })

    return posts


# ==========================================
# MARKTRELEVANZ ANALYSIEREN
# ==========================================

def analyze_post(text):

    text_lower = text.lower()

    detected = []

    for market, keywords in MARKETS.items():

        matches = [
            keyword
            for keyword in keywords
            if re.search(
                r"(?<!\w)" +
                re.escape(keyword) +
                r"(?!\w)",
                text_lower
            )
        ]

        if matches:
            detected.append({
                "market": market,
                "keywords": matches
            })

    return detected


# ==========================================
# ALARM ERSTELLEN
# ==========================================

def create_alert(post, detected):

    market_names = [
        item["market"]
        for item in detected
    ]

    markets_text = "\n".join(
        "• " + market
        for market in market_names
    )

    message = (
        "TRUMP TRADING RADAR\n\n"
        "MARKTRELEVANTE AUSSAGE\n\n"
        f"Beitrag:\n{post['text'][:1700]}\n\n"
        f"Betroffene Themen:\n{markets_text}\n\n"
        f"Veröffentlicht: "
        f"{post['published'] or 'Unbekannt'}\n\n"
        f"Originalquelle:\n{post['link']}\n\n"
        "STATUS: Beobachtungsalarm\n\n"
        "Die tatsächliche Kursreaktion wurde "
        "noch nicht überprüft.\n"
        "Keine automatische Kaufempfehlung."
    )

    return message


# ==========================================
# HAUPTPROGRAMM
# ==========================================

def main():

    posts = get_posts()

    if not posts:
        raise RuntimeError(
            "Keine Beiträge in der Datenquelle gefunden."
        )

    first_run = not STATE_FILE.exists()

    if first_run:
        seen = set()
    else:
        seen = set(
            json.loads(
                STATE_FILE.read_text(
                    encoding="utf-8"
                )
            )
        )

    new_posts = [
        post for post in posts
        if post["id"] not in seen
    ]

    # Beim ersten Start nur Bestand erfassen.
    # Keine alten Nachrichten versenden.

    if not first_run:

        for post in reversed(new_posts):

            detected = analyze_post(
                post["text"]
            )

            if detected:

                alert = create_alert(
                    post, detected
                )

                send_telegram(alert)

                print(
                    "Alarm versendet:",
                    post["id"]
                )

    updated = seen | {
        post["id"]
        for post in posts
    }

    STATE_FILE.write_text(
        json.dumps(
            sorted(updated),
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        f"Überprüfung abgeschlossen. "
        f"{len(new_posts)} neue Beiträge."
    )


if os.environ.get("TEST_TELEGRAM") == "1":
    send_telegram("Trump Trading Radar: Test erfolgreich!")
    raise SystemExit(0)
if __name__ == "__main__":
    main()
