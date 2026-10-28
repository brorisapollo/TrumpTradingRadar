# Trump Trading Radar

RSS keyword alerts only. No broker, trading API, orders or automatic trades.

## Operation

Existing secrets stay unchanged: `TRUMP_FEED_URL`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`. The built-in Actions `GITHUB_TOKEN` writes the existing
`seen_posts.json` on the default branch; no personal token is needed.
The file remains a JSON list of IDs. Do not delete or reset it.

Scheduled runs use UTC minutes 2, 7, 12, ... 57. Manual runs default to
**dry_run=true**: fetch and validate the feed, analyze, send nothing, write nothing.
To run the regular sender manually, uncheck that input on the default branch.
Other branches cannot run the sender. The verification workflow runs offline tests
on code changes and validates the configured feed on pushes without Telegram secrets.

```sh
python -m unittest discover -s tests -v
python radar.py --dry-run
```

Only the second command needs TRUMP_FEED_URL. There are no third-party Python dependencies.

## Persistence and duplicate protection

The old checkout/commit/push step could fail with `rejected (fetch first)`.
The sender now reads the CURRENT remote file through GitHub's Contents API.
Each alert ID is reserved with the file's current SHA BEFORE contacting Telegram.
Conflicts (409/422) cause a bounded re-read/union/retry, preserving other IDs and code
commits. If another worker already claimed the ID, this worker does not send.
No force push, git reset or stale-checkout overwrite is used.

A single concurrency group serializes manual and scheduled radar runs across
branches. Active runs are never cancelled by a newer run. GitHub may replace
pending runs; the next run checks the current feed. Canonical archive URLs collapse
duplicate RSS entries and GUID changes. All historical IDs remain intact.
Non-relevant posts are saved in a batch. Existing empty state establishes a silent
baseline; missing/corrupt remote state fails closed instead of replaying the feed.

### Delivery semantics: at most one attempt

A reservation means processed/reserved, NOT proof of delivery. If the process
crashes after reserving or Telegram times out/rejects a message, that post is not
automatically retried. This can lose an alert but prevents replay after an uncertain
send. Exactly-once delivery cannot be guaranteed across GitHub and Telegram:
sendMessage has no caller-supplied idempotency key.
Successful delivery is logged as `Alert confirmed: <public archive URL>`.
Failures stop the run, preserving earlier reservations and leaving later posts
unclaimed. No response bodies, secret URLs or tokens are logged. Requests do not
follow redirects. Sends are spaced to avoid bursts.

To recover an uncertain alert: inspect the failed run and Telegram first. Only
after confirming non-delivery, deliberately remove the relevant reservation while
no sender is running, then rerun. Never blindly clear the whole list. Keep Actions
failure notifications enabled. Branch protection can block state writes; the
sender then stops before Telegram. State must stay below GitHub's 1 MB Contents
API inline-content limit; migrate to a transactional store before that limit.

## Feed validation and source limits

Accepted source: HTTPS `www.trumpstruth.org/feed` (bare host is canonicalized),
optionally `?limit=1..500`. Date filters and other sources are rejected so a
historical search or news feed cannot silently replace the live archive.
Validation checks size, XML/RSS structure, stable archive post links, dates,
deduplication and nonempty entries. A feed whose newest post is older than
48 hours or over 15 minutes in the future fails visibly. A quiet account can
therefore need manual review; age alone does not prove an outage.

Trump's Truth is a third-party archive, not affiliated with Trump or Truth Social.
It includes reposts and derived media descriptions/transcripts. The alert says
"Archivbeitrag" rather than attributing every entry directly to Trump.
This source check does not independently authenticate the text or measure upstream
delay. The provider documents a default 100-post window (maximum 500) and caching.
No overlap with saved IDs warns of potentially missed posts; it cannot reconstruct
entries already lost from the window. Keyword matches are topic flags, not market
impact analysis or trading advice.

## Scheduler audit — 2026-10-28

The nine available runs included scheduled successes at:
- 2026-10-27 23:05:22 UTC
- 2026-10-28 02:07:32 UTC
- 2026-10-28 08:21:20 UTC

Those gaps are about 3h02m and 6h14m, despite the former five-minute cron.
These successes ran the temporary Telegram test mode and do not establish feed
health or successful real processing. Run 40569955644 confirms the radar step
succeeded but the persistence step failed with fetch-first. Its five confirmed
alert IDs are already present in seen_posts.json; no historical reset is needed.

The revised cron avoids the exact hour, but is NOT a five-minute SLA. GitHub
documents delays and dropped scheduled events under load, default-branch-only
schedules, and automatic disabling after 60 days of inactivity in public repos.
The observed history cannot identify why particular scheduled events were absent.
For dependable near-real-time monitoring, use an external scheduler/worker and
an independent missed-run monitor; neither is provisioned by this change.
A check running inside this workflow cannot report when the workflow never starts.

## Verification

22 offline regression tests cover reservation-before-send, duplicate entries,
repeat runs, competing claims, conflict union, bounded retries, persistence
failures, ambiguous writes/sends, partial failure, first-run baseline, bad state,
source/date validation, Telegram API rejection, dry-run side effects and keywords.
All passed locally on Python 3.14. GitHub verification uses Python 3.12 and performs
a real read-only fetch using the configured feed secret. It never receives the
Telegram token or writes state. Live sender recovery and real message delivery
are intentionally not tested by sending messages.

## References

- [GitHub scheduled events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub Contents API](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents)
- [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage)
- [Archive FAQ](https://www.trumpstruth.org/faq)
- [Archive background](https://www.trumpstruth.org/about)
