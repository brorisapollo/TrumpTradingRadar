import base64
import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
import radar

ID = "https://www.trumpstruth.org/statuses/1"
OLD = "https://www.trumpstruth.org/statuses/0"


def feed(items=None):
    if items is None:
        items = '<item><title>Oil policy</title><link>' + ID + '</link><pubDate>Wed, 28 Oct 2026 08:00:00 GMT</pubDate></item>'
    return ('<rss><channel>' + items + '</channel></rss>').encode()


def post(id=ID, text="oil"):
    return {"id": id, "text": text, "link": id, "published": "today",
            "published_at": datetime.now(timezone.utc)}


class MemoryState:
    def __init__(self, seen):
        self.seen = set(seen)
        self.claimed = []
    def read(self):
        return self.seen.copy(), "sha"
    def claim(self, ids):
        new = set(ids) - self.seen
        self.seen.update(new)
        self.claimed.append(new)
        return new


class RadarTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch("radar.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_import_needs_no_credentials(self):
        self.assertTrue(callable(radar.main))

    def test_feed_deduplicates_canonical_url_even_if_guid_changes(self):
        item = '<item><title>oil</title><link>https://trumpstruth.org/statuses/1/</link><guid>different</guid><pubDate>Wed, 28 Oct 2026 08:00:00 GMT</pubDate></item>'
        posts = radar.parse_posts(feed(item * 2))
        self.assertEqual([p["id"] for p in posts], [ID])

    def test_invalid_feeds_fail_closed(self):
        for data in [b"<html/>", b"not xml", feed(""), b'<!DOCTYPE rss><rss/>',
                     feed().replace(b"trumpstruth.org", b"example.com"),
                     feed().replace(b"Wed, 28 Oct 2026 08:00:00 GMT", b"invalid")]:
            with self.subTest(data=data), self.assertRaises(radar.RadarError):
                radar.parse_posts(data)

    def test_source_validation(self):
        self.assertEqual(radar.validated_feed_url("https://trumpstruth.org/feed?limit=500"),
                         "https://www.trumpstruth.org/feed?limit=500")
        for url in ["http://www.trumpstruth.org/feed", "https://example.com/feed",
                    "https://www.trumpstruth.org/feed?start_date=2024-01-01",
                    "https://www.trumpstruth.org/feed?limit=0",
                    "https://user:secret@www.trumpstruth.org/feed",
                    "https://www.trumpstruth.org:bad/feed"]:
            with self.subTest(url=url), self.assertRaises(radar.RadarError):
                radar.validated_feed_url(url)

    def test_stale_and_future_fail(self):
        now = datetime.now(timezone.utc)
        for age in [timedelta(hours=49), timedelta(minutes=-16)]:
            p = post()
            p["published_at"] = now - age
            with self.assertRaises(radar.RadarError):
                radar.validate_freshness([p], now)

    def test_valid_freshness(self):
        radar.validate_freshness([post()])

    def test_state_validation(self):
        for data in ['{}', 'null', '[1]', '[""]', 'bad']:
            with self.subTest(data=data), self.assertRaises(radar.RadarError):
                radar.decode_state(data)

    def test_baseline_does_not_alert(self):
        state, send = MemoryState([]), Mock()
        radar.process_posts([post()], state, send)
        send.assert_not_called()
        self.assertEqual(state.seen, {ID})

    def test_reservation_precedes_send_and_repeat_is_silent(self):
        state = MemoryState([OLD])
        send = Mock(side_effect=lambda message: self.assertIn(ID, state.seen))
        radar.process_posts([post(), post()], state, send)
        radar.process_posts([post()], state, send)
        self.assertEqual(send.call_count, 1)

    def test_failed_save_never_sends(self):
        state, send = MemoryState([OLD]), Mock()
        state.claim = Mock(side_effect=radar.RadarError("save failed"))
        with self.assertRaises(radar.RadarError):
            radar.process_posts([post()], state, send)
        send.assert_not_called()

    def test_partial_send_failure_is_not_replayed(self):
        state = MemoryState([OLD])
        send = Mock(side_effect=radar.RadarError("delivery uncertain"))
        with self.assertRaises(radar.RadarError):
            radar.process_posts([post(), post(ID + "2")], state, send)
        self.assertIn(ID, state.seen)
        self.assertNotIn(ID + "2", state.seen)
        retry = Mock()
        radar.process_posts([post()], state, retry)
        retry.assert_not_called()

    def test_other_runner_claimed_it(self):
        state, send = MemoryState([OLD]), Mock()
        state.claim = Mock(return_value=set())
        radar.process_posts([post()], state, send)
        send.assert_not_called()

    def test_irrelevant_posts_are_saved_without_alerts(self):
        state, send = MemoryState([OLD]), Mock()
        radar.process_posts([post(text="Good morning")], state, send)
        send.assert_not_called()
        self.assertIn(ID, state.seen)

    @patch("radar.time.sleep")
    def test_conflict_reloads_unions_and_preserves_other_ids(self, sleep):
        state = radar.GitHubState("owner/repo", "main", "fake")
        state.read = Mock(side_effect=[({OLD}, "sha1"), ({OLD, "concurrent"}, "sha2")])
        state.request = Mock(side_effect=[None, {"commit": {"sha": "saved"}}])
        self.assertEqual(state.claim({ID}), {ID})
        payload = state.request.call_args.args[1]
        self.assertEqual(payload["sha"], "sha2")
        self.assertEqual(set(json.loads(base64.b64decode(payload["content"]))),
                         {OLD, "concurrent", ID})

    @patch("radar.time.sleep")
    def test_losing_race_does_not_send_again(self, sleep):
        state = radar.GitHubState("owner/repo", "main", "fake")
        state.read = Mock(side_effect=[({OLD}, "sha1"), ({OLD, ID}, "sha2")])
        state.request = Mock(return_value=None)
        self.assertEqual(state.claim({ID}), set())
        self.assertEqual(state.request.call_count, 1)

    @patch("radar.time.sleep")
    def test_conflict_retries_are_bounded(self, sleep):
        state = radar.GitHubState("owner/repo", "main", "fake")
        state.read = Mock(return_value=({OLD}, "sha"))
        state.request = Mock(return_value=None)
        with self.assertRaises(radar.RadarError):
            state.claim({ID})
        self.assertEqual(state.request.call_count, 5)

    def test_ambiguous_write_stops_without_retry(self):
        state = radar.GitHubState("owner/repo", "main", "fake")
        with patch("radar.open_request", side_effect=TimeoutError("secret-token")) as request:
            with self.assertRaises(radar.RadarError) as error:
                state.request("PUT", {})
        self.assertNotIn("secret-token", str(error.exception))
        self.assertEqual(request.call_count, 1)

    def test_missing_remote_state_is_not_treated_as_first_run(self):
        state = radar.GitHubState("owner/repo", "main", "fake")
        error = urllib.error.HTTPError("secret", 404, "secret", {}, None)
        with patch("radar.open_request", side_effect=error):
            with self.assertRaises(radar.RadarError):
                state.read()

    def test_dry_run_has_no_side_effects(self):
        with tempfile.TemporaryDirectory(dir=".") as directory:
            path = Path(directory) / "seen.json"
            path.write_text(json.dumps([OLD]))
            before = path.read_bytes()
            with patch.object(radar, "STATE_FILE", path), \
                 patch("radar.get_posts", return_value=[post()]), \
                 patch("radar.send_telegram") as send, \
                 patch("radar.GitHubState") as state:
                radar.main(["--dry-run"])
                send.assert_not_called()
                state.assert_not_called()
                self.assertEqual(path.read_bytes(), before)

    def test_telegram_ok_false_is_failure(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": "fake"}), \
             patch("radar.open_request", return_value=contextlib.closing(io.BytesIO(b'{"ok":false}'))):
            with self.assertRaises(radar.RadarError):
                radar.send_telegram("test")

    def test_telegram_network_error_redacts_and_never_retries(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": "fake"}), \
             patch("radar.open_request", side_effect=OSError("fake-token")) as request:
            with self.assertRaises(radar.RadarError) as error:
                radar.send_telegram("test")
            self.assertNotIn("fake-token", str(error.exception))
            self.assertEqual(request.call_count, 1)

    def test_keywords_have_word_boundaries(self):
        self.assertFalse(radar.analyze_post("beautiful federalism"))
        self.assertTrue(radar.analyze_post("oil tariff"))


if __name__ == "__main__":
    unittest.main()
