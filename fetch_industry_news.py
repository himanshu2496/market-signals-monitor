#!/usr/bin/env python3
"""
fetch_industry_news.py
Monitors pulpwood, site suitability, and vegetation management industry news.
Posts to #market-signals-industry via Slack Incoming Webhook every 2 hours.

Data sources:
  - Google News RSS (multiple queries per topic)
  - PR Newswire RSS (filtered by topic keywords)

Environment variables required:
  SLACK_WEBHOOK_INDUSTRY  — Slack Incoming Webhook URL for #market-signals-industry
  NEWS_API_KEY            — NewsAPI.org key (optional; not used by this script but
                            kept for parity with fetch_company_intel.py)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser
import requests

from ai_filter import filter_relevant_articles

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ── Config paths ──────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOPICS_PATH = os.path.join(BASE_DIR, "config", "topics.json")
SETTINGS_PATH = os.path.join(BASE_DIR, "config", "settings.json")


# ── Loaders ───────────────────────────────────────────────────────────────────
def load_config() -> tuple[dict, dict]:
    with open(TOPICS_PATH) as f:
        topics_cfg = json.load(f)
    with open(SETTINGS_PATH) as f:
        settings = json.load(f)
    return topics_cfg, settings


# ── Date helpers ──────────────────────────────────────────────────────────────
def parse_pub_date(date_str: str | None) -> datetime | None:
    """Parse RSS (RFC 2822) or ISO 8601 date string into a UTC-aware datetime."""
    if not date_str:
        return None
    # ISO 8601 (NewsAPI / some feeds)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # RFC 2822 (RSS standard)
    try:
        dt = parsedate_to_datetime(date_str)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    log.warning("Could not parse date string: %r", date_str)
    return None


def is_within_window(pub_date: datetime | None, lookback_minutes: int) -> bool:
    if pub_date is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    return pub_date >= cutoff


# ── Fetchers ──────────────────────────────────────────────────────────────────
def fetch_google_rss(query: str, lookback_minutes: int, max_results: int) -> list[dict]:
    """Fetch and filter Google News RSS entries within the lookback window."""
    url = (
        f"https://news.google.com/rss/search"
        f"?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    log.info("Google RSS: %s", url)
    try:
        feed = feedparser.parse(url)
    except Exception as exc:
        log.error("feedparser error for %r: %s", query, exc)
        return []

    articles = []
    for entry in feed.entries[:max_results]:
        pub_date = parse_pub_date(getattr(entry, "published", None))
        if not is_within_window(pub_date, lookback_minutes):
            continue
        articles.append({
            "title": (entry.get("title") or "").strip(),
            "url": entry.get("link", ""),
            "source": entry.get("source", {}).get("title", "Google News"),
            "published_at": pub_date,
            "origin": "google_rss",
        })

    log.info("  → %d articles in window for query %r", len(articles), query)
    return articles


def fetch_pr_newswire(rss_url: str, lookback_minutes: int) -> list[dict]:
    """Fetch PR Newswire RSS feed. Returns all entries within the window."""
    log.info("PR Newswire RSS: %s", rss_url)
    try:
        feed = feedparser.parse(rss_url)
    except Exception as exc:
        log.error("PR Newswire fetch error: %s", exc)
        return []

    articles = []
    for entry in feed.entries:
        pub_date = parse_pub_date(getattr(entry, "published", None))
        if not is_within_window(pub_date, lookback_minutes):
            continue
        summary = entry.get("summary", "")
        articles.append({
            "title": (entry.get("title") or "").strip(),
            "url": entry.get("link", ""),
            "source": "PR Newswire",
            "published_at": pub_date,
            "origin": "prnewswire",
            "_summary": summary.lower(),
            "_title_lower": (entry.get("title") or "").lower(),
        })

    log.info("  → %d PR Newswire articles in window", len(articles))
    return articles


def filter_by_keywords(articles: list[dict], keywords: list[str]) -> list[dict]:
    """Keep articles whose title or summary contains at least one keyword."""
    matched = []
    for a in articles:
        combined = a.get("_title_lower", "") + " " + a.get("_summary", "")
        if any(kw.lower() in combined for kw in keywords):
            matched.append(a)
    return matched


def deduplicate(articles: list[dict]) -> list[dict]:
    """Deduplicate by URL, keeping the entry with the most recent published_at."""
    seen: dict[str, dict] = {}
    for a in articles:
        url = a["url"]
        if not url:
            continue
        if url not in seen:
            seen[url] = a
        else:
            existing_dt = seen[url]["published_at"]
            new_dt = a["published_at"]
            if new_dt and (existing_dt is None or new_dt > existing_dt):
                seen[url] = a
    return list(seen.values())


# ── Slack formatting ──────────────────────────────────────────────────────────
def _time_ago(pub_date: datetime | None) -> str:
    if pub_date is None:
        return "unknown time"
    delta = datetime.now(timezone.utc) - pub_date
    total_minutes = int(delta.total_seconds() / 60)
    if total_minutes < 60:
        return f"{total_minutes}m ago"
    hours = total_minutes // 60
    return f"{hours}h ago"


def build_slack_blocks(
    topic_label: str,
    emoji: str,
    articles: list[dict],
    max_articles: int,
) -> list[dict]:
    """Build Slack Block Kit blocks for one topic's articles."""
    sorted_articles = sorted(
        articles,
        key=lambda a: a["published_at"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:max_articles]

    count = len(sorted_articles)
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} {topic_label}  —  {count} new article{'s' if count != 1 else ''}",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    for article in sorted_articles:
        title = article["title"]
        if len(title) > 120:
            title = title[:117] + "..."
        url = article["url"]
        source = article["source"]
        time_label = _time_ago(article["published_at"])
        origin_tag = "📢" if article.get("origin") == "prnewswire" else "📰"

        reason = article.get("relevance_reason", "")
        text = f"{origin_tag} *<{url}|{title}>*\n_{source}_   ·   {time_label}"
        if reason:
            text += f"\n> _{reason}_"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        })

    blocks.append({"type": "divider"})
    return blocks


def build_footer_block(total: int, topic_count: int) -> dict:
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return {
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": (
                    f":robot_face: *Market Signals — Industry*   ·   {run_time}   ·   "
                    f"{total} article{'s' if total != 1 else ''} across {topic_count} topic{'s' if topic_count != 1 else ''}"
                ),
            }
        ],
    }


# ── Slack sender ──────────────────────────────────────────────────────────────
def send_slack(webhook_url: str, blocks: list[dict], max_blocks: int = 47) -> bool:
    """POST Block Kit payload to Slack, splitting into chunks if needed."""
    chunks = [blocks[i:i + max_blocks] for i in range(0, len(blocks), max_blocks)]
    success = True
    for chunk in chunks:
        payload = {"blocks": chunk}
        try:
            resp = requests.post(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
            log.info("Slack message sent (%d blocks).", len(chunk))
        except requests.exceptions.HTTPError as exc:
            log.error("Slack HTTP error: %s — %s", exc, getattr(exc.response, "text", ""))
            success = False
        except requests.exceptions.RequestException as exc:
            log.error("Slack request failed: %s", exc)
            success = False
    return success


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # Support local .env loading (no-op if python-dotenv not installed)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    slack_webhook = os.environ.get("SLACK_WEBHOOK_INDUSTRY", "").strip()
    if not slack_webhook:
        log.error("SLACK_WEBHOOK_INDUSTRY is not set. Aborting.")
        sys.exit(1)

    topics_cfg, settings = load_config()
    lookback = topics_cfg.get("lookback_minutes", 125)
    max_per_topic = topics_cfg.get("max_articles_per_topic", 5)
    max_results = settings["limits"]["google_news_max_results"]
    max_blocks = settings["slack"]["max_blocks_per_message"]
    pr_rss_url = settings["sources"]["prnewswire_rss"]

    # Fetch PR Newswire once for all topics
    pr_articles = fetch_pr_newswire(pr_rss_url, lookback)

    all_blocks: list[dict] = []
    topics_with_articles = 0
    total_articles = 0

    for topic in topics_cfg["topics"]:
        topic_label = topic["label"]
        emoji = topic["emoji"]
        log.info("=== Topic: %s ===", topic_label)

        raw: list[dict] = []

        # Google News RSS (multiple queries)
        for query in topic["queries"]:
            raw.extend(fetch_google_rss(query, lookback, max_results))

        # PR Newswire filtered by topic keywords
        pr_matched = filter_by_keywords(pr_articles, topic.get("pr_newswire_keywords", []))
        raw.extend(pr_matched)

        unique = deduplicate(raw)
        log.info("Topic '%s': %d unique articles before AI filter", topic_label, len(unique))

        relevant = filter_relevant_articles(unique)
        log.info("Topic '%s': %d relevant articles after AI filter", topic_label, len(relevant))

        if relevant:
            topic_blocks = build_slack_blocks(topic_label, emoji, relevant, max_per_topic)
            all_blocks.extend(topic_blocks)
            topics_with_articles += 1
            total_articles += min(len(relevant), max_per_topic)

    if total_articles == 0:
        log.info(
            "No relevant articles found in the last %d minutes. No Slack message sent.",
            lookback,
        )
        return

    all_blocks.append(build_footer_block(total_articles, topics_with_articles))
    send_slack(slack_webhook, all_blocks, max_blocks)


if __name__ == "__main__":
    main()
