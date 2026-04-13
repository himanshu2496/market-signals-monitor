#!/usr/bin/env python3
"""
fetch_company_intel.py — Company intelligence monitor.

Modes:
  --mode=alerts  : Priority companies, 6-hour lookback, 6-hour AI summary to Slack
  --mode=digest  : All companies, 26-hour lookback, daily digest to Slack at 9am UTC

Sources: Google News RSS, NewsAPI.org, PR Newswire RSS, SEC EDGAR (digest only)
"""
from __future__ import annotations

import argparse
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


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["alerts", "digest"], default="alerts")
    return parser.parse_args()


def load_config():
    with open(os.path.join(CONFIG_DIR, "companies.json")) as f:
        companies = json.load(f)
    with open(os.path.join(CONFIG_DIR, "settings.json")) as f:
        settings = json.load(f)
    return companies, settings


def _search_name(company: str, aliases: dict) -> str:
    """Return alias if available, otherwise full company name in quotes for exact matching."""
    return aliases.get(company, f'"{company}"')


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


def fetch_google_rss_company(company: str, lookback_minutes: int) -> list:
    query = f'"{company}" announcement OR strategy OR acquisition OR partnership OR earnings'
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
                "company": company,
                "tag": "news",
            })
        return articles
    except Exception as exc:
        log.error("Google RSS failed for '%s': %s", company, exc)
        return []


def fetch_google_rss_exec(company: str, exec_titles: list, lookback_minutes: int) -> list:
    titles_q = " OR ".join(f'"{t}"' for t in exec_titles[:6])
    query = f'"{company}" ({titles_q})'
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
                "company": company,
                "tag": "exec",
            })
        return articles
    except Exception as exc:
        log.error("Exec RSS failed for '%s': %s", company, exc)
        return []


def fetch_newsapi_batch(
    companies: list, aliases: dict, lookback_minutes: int, api_key: str, page_size: int = 5
) -> list:
    if not api_key:
        return []
    from_dt = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    query = " OR ".join(_search_name(c, aliases) for c in companies)
    params = {
        "q": query,
        "from": from_dt,
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "apiKey": api_key,
        "language": "en",
    }
    try:
        resp = requests.get("https://newsapi.org/v2/everything", params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        articles = []
        for item in data.get("articles", []):
            pub = parse_pub_date(item.get("publishedAt", ""))
            title = item.get("title", "") or ""
            # Match article to a company
            matched = ""
            title_lower = title.lower()
            for c in companies:
                alias = aliases.get(c, c).strip('"').lower()
                if alias in title_lower or c.lower() in title_lower:
                    matched = c
                    break
            articles.append({
                "title": title,
                "url": item.get("url", ""),
                "source": item.get("source", {}).get("name", "NewsAPI"),
                "published": item.get("publishedAt", ""),
                "published_dt": pub,
                "company": matched,
                "tag": "news",
            })
        return articles
    except Exception as exc:
        log.error("NewsAPI batch failed: %s", exc)
        return []


def fetch_pr_newswire_companies(companies: list, aliases: dict, lookback_minutes: int) -> list:
    url = "https://www.prnewswire.com/rss/news-releases-list.rss"
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries:
            text = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
            matched = ""
            for c in companies:
                alias = aliases.get(c, c).strip('"').lower()
                if alias in text or c.lower() in text:
                    matched = c
                    break
            if not matched:
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
                "company": matched,
                "tag": "pr",
            })
        return articles
    except Exception as exc:
        log.error("PR Newswire fetch failed: %s", exc)
        return []


def fetch_edgar_filings(
    company: str, cik: str, forms: list, lookback_minutes: int, user_agent: str
) -> list:
    from_dt = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%d")
    forms_str = ",".join(forms)
    url = (
        f"https://efts.sec.gov/LATEST/search-index"
        f"?q=%22{cik}%22&forms={forms_str}&dateRange=custom&startdt={from_dt}"
    )
    headers = {"User-Agent": user_agent}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        articles = []
        for hit in data.get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            form_type = src.get("form_type", "Filing")
            filing_url = (
                f"https://www.sec.gov/cgi-bin/browse-edgar"
                f"?action=getcompany&CIK={cik}&type={form_type}&dateb=&owner=include&count=10"
            )
            pub = parse_pub_date(src.get("file_date", ""))
            articles.append({
                "title": f"{form_type}: {src.get('display_names', company)}",
                "url": filing_url,
                "source": "SEC EDGAR",
                "published": src.get("file_date", ""),
                "published_dt": pub,
                "company": company,
                "tag": "sec",
            })
        return articles
    except Exception as exc:
        log.error("EDGAR failed for CIK %s: %s", cik, exc)
        return []


def deduplicate(articles: list) -> list:
    seen = {}
    for a in articles:
        url = a.get("url", "")
        if url and url not in seen:
            seen[url] = a
    return list(seen.values())


def tag_icon(tag: str) -> str:
    return {"pr": "📢", "sec": "📋", "exec": "👤", "news": "📰"}.get(tag, "📰")


def build_slack_blocks(
    summary: str, articles: list, total_scanned: int, mode: str
) -> list:
    blocks = []
    now_str = datetime.now(timezone.utc).strftime("%b %d, %Y")

    if mode == "digest":
        title = f"📊 Market Signals Daily — {now_str}"
    else:
        title = f"🎯 Company Intelligence — {now_str}"

    blocks.append({"type": "header", "text": {"type": "plain_text", "text": title}})

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

    # Group articles by company
    by_company: dict = {}
    for a in articles:
        c = a.get("company") or "Other"
        by_company.setdefault(c, []).append(a)

    max_per_company = 3 if mode == "digest" else 5

    for company, company_articles in sorted(by_company.items()):
        if len(blocks) >= 44:
            break
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{company}*"},
        })
        for a in company_articles[:max_per_company]:
            article_title = a.get("title", "No title")
            url = a.get("url", "")
            source = a.get("source", "?")
            ago = time_ago(a.get("published_dt"))
            icon = tag_icon(a.get("tag", "news"))
            reason = a.get("relevance_reason", "")

            linked = f"<{url}|{article_title}>" if url else article_title
            text = f"{icon} {linked}\n_{source}_ · {ago}"
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
    args = parse_args()
    mode = args.mode

    companies_cfg, settings = load_config()
    aliases = companies_cfg.get("aliases", {})
    exec_titles = companies_cfg.get("exec_titles", [])
    edgar_ciks = companies_cfg.get("edgar_ciks", {})
    sheets_cfg = settings["sheets"]
    edgar_cfg = settings["edgar"]

    if mode == "alerts":
        companies = companies_cfg["priority"]
        lookback = settings["lookback"]["summary_minutes"]
    else:
        companies = companies_cfg["all"]
        lookback = settings["lookback"]["digest_minutes"]

    newsapi_key = os.environ.get("NEWS_API_KEY", "").strip()
    webhook = os.environ.get("SLACK_WEBHOOK_INTEL", "").strip()

    if not webhook:
        log.error("SLACK_WEBHOOK_INTEL not set.")
        return

    log.info("Mode: %s | Companies: %d | Lookback: %d min", mode, len(companies), lookback)

    all_articles = []

    # Google News RSS per company
    for company in companies:
        all_articles.extend(fetch_google_rss_company(company, lookback))
        all_articles.extend(fetch_google_rss_exec(company, exec_titles, lookback))
        time.sleep(0.3)

    # PR Newswire (single fetch, filter by company name)
    all_articles.extend(fetch_pr_newswire_companies(companies, aliases, lookback))

    # NewsAPI in batches of 5
    batch_size = settings["limits"].get("newsapi_page_size", 5)
    if newsapi_key:
        for i in range(0, len(companies), batch_size):
            batch = companies[i : i + batch_size]
            all_articles.extend(
                fetch_newsapi_batch(batch, aliases, lookback, newsapi_key, page_size=5)
            )
            time.sleep(0.5)

    # SEC EDGAR (digest only, to stay within rate limits)
    if mode == "digest":
        for company in companies:
            cik = edgar_ciks.get(company)
            if not cik:
                continue
            filings = fetch_edgar_filings(
                company=company,
                cik=cik,
                forms=edgar_cfg["forms"],
                lookback_minutes=lookback,
                user_agent=edgar_cfg["user_agent"],
            )
            all_articles.extend(filings)
            time.sleep(edgar_cfg.get("rate_delay_seconds", 0.6))

    all_articles = deduplicate(all_articles)
    total_scanned = len(all_articles)
    log.info("Total raw articles: %d", total_scanned)

    if not all_articles:
        log.info("No articles found in the last %d minutes.", lookback)
        return

    filtered = filter_relevant_articles(all_articles)
    log.info("After AI filter: %d articles", len(filtered))

    if not filtered:
        log.info("No relevant articles after filtering.")
        return

    summary = generate_action_summary(filtered)
    blocks = build_slack_blocks(summary or "", filtered, total_scanned, mode)
    send_slack(webhook, blocks)
    log.info("Posted company intel (%s) to Slack.", mode)

    write_to_sheets(
        sheet_name=sheets_cfg["name"],
        bullets_tab=sheets_cfg["bullets_tab"],
        articles_tab=sheets_cfg["articles_tab"],
        summary=summary,
        articles=filtered,
        source_label=f"Company Intel ({mode})",
    )


if __name__ == "__main__":
    main()
