#!/usr/bin/env python3
"""
fetch_industry_news.py — Industry topic monitor.

Fetches Google News RSS and PR Newswire for each topic (Pulpwood,
Site Suitability, Vegetation Management), filters with Gemini AI,
generates an action summary, and posts to #market-signals-industry.

Runs every 2 hours via GitHub Actions.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote_plus

import feedparser
import requests

from ai_filter import filter_relevant_articles, generate_action_summary
from gsheets import write_to_sheets

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")


def load_config():
    with open(os.path.join(CONFIG_DIR, "topics.json")) as f:
        topics = json.load(f)
    with open(os.path.join(CONFIG_DIR, "settings.json")) as f:
        settings = json.load(f)
    return topics, settings


def parse_pub_date(date_str: str) -> Optional[datetime]:
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str).astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def is_within_window(dt: Optional[datetime], minutes: int) -> bool:
    if not dt:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return dt >= cutoff


def time_ago(dt: Optional[datetime]) -> str:
    if not dt:
        return "unknown time"
    delta = datetime.now(timezone.utc) - dt
    mins = int(delta.total_seconds() / 60)
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def fetch_google_rss(query: str, lookback_minutes: int) -> list:
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries:
            pub = parse_pub_date(entry.get("published", ""))
            if not is_within_window(pub, lookback_minutes):
                continue
            articles.append({
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "source": entry.get("source", {}).get("title", "Google News"),
                "published": entry.get("published", ""),
                "published_dt": pub,
            })
        return articles
    except Exception as exc:
        log.error("Google RSS failed for '%s': %s", query, exc)
        return []


def fetch_pr_newswire(keywords: list, lookback_minutes: int) -> list:
    url = "https://www.prnewswire.com/rss/news-releases-list.rss"
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries:
            text = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
            if not any(kw.lower() in text for kw in keywords):
                continue
            pub = parse_pub_date(entry.get("published", ""))
            if not is_within_window(pub, lookback_minutes):
                continue
            articles.append({
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "source": "PR Newswire",
                "published": entry.get("published", ""),
                "published_dt": pub,
            })
        return articles
    except Exception as exc:
        log.error("PR Newswire fetch failed: %s", exc)
        return []


def deduplicate(articles: list) -> list:
    seen = {}
    for a in articles:
        url = a.get("url", "")
        if url and url not in seen:
            seen[url] = a
    return list(seen.values())


def build_slack_blocks(
    topic_label: str, emoji: str, summary: str, articles: list, total_scanned: int
) -> list:
    blocks = []
    now_str = datetime.now(timezone.utc).strftime("%b %d, %Y")

    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"{emoji} {topic_label} — {now_str}"},
    })

    if summary:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*What to act on:*\n{summary}"},
        })
        blocks.append({"type": "divider"})

    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"_Supporting signals: {len(articles)} relevant out of {total_scanned} scanned_",
        },
    })

    for a in articles[:12]:
        title = a.get("title", "No title")
        url = a.get("url", "")
        source = a.get("source", "?")
        ago = time_ago(a.get("published_dt"))
        reason = a.get("relevance_reason", "")

        linked = f"<{url}|{title}>" if url else title
        text = f"{linked}\n_{source}_ · {ago}"
        if reason:
            text += f"\n_{reason}_"

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    blocks.append({"type": "divider"})
    return blocks


def send_slack(webhook_url: str, blocks: list):
    for i in range(0, len(blocks), 47):
        chunk = blocks[i : i + 47]
        try:
            resp = requests.post(webhook_url, json={"blocks": chunk}, timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            log.error("Slack post failed: %s", exc)


def main():
    topics_cfg, settings = load_config()
    lookback = settings["lookback"]["summary_minutes"]
    max_per_topic = settings["limits"]["max_articles_per_topic"]
    sheets_cfg = settings["sheets"]

    webhook = os.environ.get("SLACK_WEBHOOK_INDUSTRY", "").strip()
    if not webhook:
        log.error("SLACK_WEBHOOK_INDUSTRY not set.")
        return

    all_blocks = []
    any_found = False

    for topic in topics_cfg["topics"]:
        label = topic["label"]
        emoji = topic.get("emoji", ":newspaper:")
        queries = topic["queries"]
        pr_keywords = topic.get("pr_keywords", [])

        raw = []
        for q in queries:
            raw.extend(fetch_google_rss(q, lookback))
            time.sleep(0.3)
        raw.extend(fetch_pr_newswire(pr_keywords, lookback))
        raw = deduplicate(raw)
        total_scanned = len(raw)
        log.info("Topic '%s': %d raw articles", label, total_scanned)

        if not raw:
            continue

        filtered = filter_relevant_articles(raw)
        if not filtered:
            continue

        any_found = True
        summary = generate_action_summary(filtered)
        blocks = build_slack_blocks(
            label, emoji, summary or "", filtered[:max_per_topic], total_scanned
        )
        all_blocks.extend(blocks)

        write_to_sheets(
            sheet_name=sheets_cfg["name"],
            bullets_tab=sheets_cfg["bullets_tab"],
            articles_tab=sheets_cfg["articles_tab"],
            summary=summary,
            articles=filtered,
            source_label=label,
        )

    if any_found and all_blocks:
        send_slack(webhook, all_blocks)
        log.info("Posted industry signals to Slack.")
    else:
        log.info("No industry signals in the last %d minutes.", lookback)


if __name__ == "__main__":
    main()
