"""Read-only market alerts. No broker integration or order execution."""
import argparse
import base64
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

STATE_FILE = Path("seen_posts.json")
MAX_FEED_BYTES = 5_000_000


class RadarError(Exception):
    """Only fixed, credential-free messages may reach the console."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RadarError("Unexpected HTTP redirect; request stopped.")


def open_request(request, timeout):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RadarError("Required configuration missing: " + name)
    return value


def decode_state(raw):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RadarError("State is not valid JSON; refusing to reset it.") from None
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise RadarError("State must be a list of nonempty IDs; refusing to reset it.")
    return set(value)


class GitHubState:
    """CAS on the latest remote file, never on the checkout's stale state."""
    def __init__(self, repo, branch, token):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise RadarError("Invalid repository configuration.")
        self.url = "https://api.github.com/repos/" + repo + "/contents/seen_posts.json"
        self.branch = branch
        self.token = token

    def request(self, method, payload=None):
        url = self.url
        if method == "GET":
            url += "?" + urllib.parse.urlencode({"ref": self.branch})
        request = urllib.request.Request(
            url, method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json",
                     "User-Agent": "TrumpTradingRadar/2.0",
                     "Cache-Control": "no-cache"})
        try:
            with open_request(request, 30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            error.close()
            if method == "PUT" and error.code in (409, 422):
                return None  # Re-read and recompute, never overwrite newer state.
            raise RadarError("GitHub state request failed (HTTP %d)." % error.code) from None
        except (OSError, ValueError):
            # A timed-out write may have succeeded: stop, never send/retry blindly.
            raise RadarError("GitHub state response uncertain; no alert will be sent.") from None

    def read(self):
        value = self.request("GET")
        try:
            if value["encoding"] != "base64":
                raise ValueError()
            seen = decode_state(base64.b64decode(value["content"]).decode("utf-8"))
            sha = value["sha"]
            if not isinstance(sha, str) or not sha:
                raise ValueError()
            return seen, sha
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise RadarError("Invalid GitHub state response; refusing to reset state.") from None

    def claim(self, ids):
        """Return only IDs durably claimed by THIS invocation."""
        wanted = set(ids)
        for attempt in range(5):
            seen, sha = self.read()
            new = wanted - seen
            if not new:
                return set()
            raw = json.dumps(sorted(seen | new), ensure_ascii=False, indent=2) + "\n"
            result = self.request("PUT", {
                "message": "Reserve processed posts [skip ci]",
                "content": base64.b64encode(raw.encode()).decode(),
                "sha": sha, "branch": self.branch})
            if result is not None:
                if not isinstance(result, dict) or not result.get("commit", {}).get("sha"):
                    raise RadarError("Unconfirmed state write; no alert will be sent.")
                return new
            time.sleep(min(2 ** attempt, 8))
        raise RadarError("State conflicts persisted; no alert will be sent.")


def validated_feed_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if (parsed.scheme != "https" or parsed.hostname not in
                ("www.trumpstruth.org", "trumpstruth.org") or
                parsed.username or parsed.password or parsed.port not in (None, 443) or
                parsed.path.rstrip("/") != "/feed" or parsed.fragment or
                set(query) - {"limit"}):
            raise ValueError()
        if "limit" in query and (len(query["limit"]) != 1 or
                                not 1 <= int(query["limit"][0]) <= 500):
            raise ValueError()
    except ValueError:
        raise RadarError("Feed must be the HTTPS Trump's Truth /feed endpoint; only limit=1..500 is allowed.") from None
    # Canonical host prevents a redirect while keeping the existing secret intact.
    return urllib.parse.urlunsplit(("https", "www.trumpstruth.org", "/feed", parsed.query, ""))


def parse_posts(xml_data):
    if len(xml_data) > MAX_FEED_BYTES or b"<!DOCTYPE" in xml_data.upper():
        raise RadarError("Feed is too large or contains a forbidden DTD.")
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        raise RadarError("Feed is not valid XML.") from None
    if root.tag != "rss" or root.find("channel") is None:
        raise RadarError("Expected an RSS channel, not HTML or another document.")
    posts = {}
    for item in root.findall("./channel/item"):
        link = item.findtext("link", "").strip()
        match = re.fullmatch(r"https://(?:www\.)?trumpstruth\.org/statuses/(\d+)/?", link)
        if not match:
            raise RadarError("Feed contains an unexpected article source.")
        # Stable canonical URL also matches the repository's existing IDs.
        post_id = "https://www.trumpstruth.org/statuses/" + match.group(1)
        content = item.findtext("title", "") + " " + item.findtext("description", "")
        content = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", content))).strip()
        published = item.findtext("pubDate", "").strip()
        try:
            published_at = parsedate_to_datetime(published)
            if published_at.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise RadarError("Feed article has no valid publication date.") from None
        if not content:
            raise RadarError("Feed article has no usable text.")
        posts.setdefault(post_id, {"id": post_id, "text": content, "link": post_id,
                                   "published": published, "published_at": published_at})
    if not posts:
        raise RadarError("Feed contains no posts.")
    return sorted(posts.values(), key=lambda p: p["published_at"])


def get_posts():
    url = validated_feed_url(required("TRUMP_FEED_URL"))
    request = urllib.request.Request(url, headers={"User-Agent": "TrumpTradingRadar/2.0"})
    try:
        with open_request(request, 30) as response:
            data = response.read(MAX_FEED_BYTES + 1)
    except (OSError, ValueError):
        raise RadarError("Feed download failed; no state change or alert.") from None
    return parse_posts(data)


def validate_freshness(posts, now=None):
    now = now or datetime.now(timezone.utc)
    newest = max(p["published_at"] for p in posts)
    age = (now - newest).total_seconds()
    if age < -900:
        raise RadarError("Feed publication date is in the future.")
    if age > 48 * 3600:
        raise RadarError("Feed's newest post is older than 48 hours; check source manually.")
    print("Feed valid: %d unique posts; newest age %.1f hours." % (len(posts), age / 3600))


def send_telegram(message):
    token = required("TELEGRAM_BOT_TOKEN")
    chat_id = required("TELEGRAM_CHAT_ID")
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message[:4000],
                                  "disable_web_page_preview": "true"}).encode()
    request = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage",
                                     data=data, method="POST")
    try:
        with open_request(request, 20) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RadarError("Telegram did not confirm delivery; reservation retained.")
    except (OSError, ValueError):
        raise RadarError("Telegram delivery uncertain or failed; reservation retained. No automatic retry.") from None

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
        "MARKTRELEVANTER ARCHIVBEITRAG\n\n"
        f"Beitrag:\n{post['text'][:1700]}\n\n"
        f"Betroffene Themen:\n{markets_text}\n\n"
        f"Veröffentlicht: "
        f"{post['published'] or 'Unbekannt'}\n\n"
        f"Archivquelle (kann Reposts enthalten):\n{post['link']}\n\n"
        "STATUS: Beobachtungsalarm\n\n"
        "Die tatsächliche Kursreaktion wurde "
        "noch nicht überprüft.\n"
        "Keine automatische Kaufempfehlung."
    )

    return message



def process_posts(posts, state, sender=send_telegram):
    seen, _ = state.read()
    ids = {p["id"] for p in posts}
    if not seen:
        state.claim(ids)  # Explicit empty state bootstraps without old alerts.
        print("Initial baseline saved; no historical alerts sent.")
        return
    if not ids & seen:
        print("::warning::No overlap with saved feed window; posts may have been missed.")
    irrelevant = set()
    sent = 0
    for post in posts:
        if post["id"] in seen:
            continue
        detected = analyze_post(post["text"])
        if not detected:
            irrelevant.add(post["id"])
            continue
        # Reserve ONE alert immediately before sending, not the whole batch.
        if state.claim({post["id"]}):
            sender(create_alert(post, detected))
            sent += 1
            print("Alert confirmed:", post["id"])
            time.sleep(1.1)  # Avoid bursts into the same Telegram chat.
        seen.add(post["id"])
    if irrelevant:
        state.claim(irrelevant)
    print("Check complete: %d alerts confirmed." % sent)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate feed and analyze; no Telegram, no state writes.")
    args = parser.parse_args(argv)
    posts = get_posts()
    validate_freshness(posts)
    if args.dry_run:
        seen = decode_state(STATE_FILE.read_text(encoding="utf-8"))
        relevant = sum(bool(analyze_post(p["text"])) for p in posts if p["id"] not in seen)
        print("Dry run: %d unseen relevant posts; nothing sent or saved." % relevant)
        return
    # Fail before reservations if delivery configuration is absent.
    required("TELEGRAM_BOT_TOKEN")
    required("TELEGRAM_CHAT_ID")
    state = GitHubState(required("GITHUB_REPOSITORY"), required("STATE_BRANCH"),
                        required("GITHUB_TOKEN"))
    process_posts(posts, state)


if __name__ == "__main__":
    try:
        main()
    except RadarError as error:
        print("::error::" + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Never print request objects, response bodies, URLs with credentials or tracebacks.
        print("::error::Unexpected failure; details suppressed to protect credentials.", file=sys.stderr)
        sys.exit(1)
