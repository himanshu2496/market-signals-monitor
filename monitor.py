#!/usr/bin/env python3
"""
monitor.py — Unified Market Signals monitor.

Runs every 6 hours. Fetches industry news and company intelligence,
filters with Gemini AI, and:
  - Posts a concise AI-written summary to Slack (no raw articles)
  - Writes full article data + summary to Google Sheets

Channels:
  #market-signals-industry  ← industry topic summary (SLACK_WEBHOOK_INDUSTRY)
  #market-signals-intel     ← company intel summary  (SLACK_WEBHOOK_INTEL)
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


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config():
    with open(os.path.join(CONFIG_DIR, "topics.json")) as f:
        topics = json.load(f)
    with open(os.path.join(CONFIG_DIR, "companies.json")) as f:
        companies = json.load(f)
    with open(os.path.join(CONFIG_DIR, "settings.json")) as f:
        settings = json.load(f)
    return topics, companies, settings


# ─── Date helpers ─────────────────────────────────────────────────────────────

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
    return dt >= datetime.now(timezone.utc) - timedelta(minutes=minutes)


# ─── Fetchers ─────────────────────────────────────────────────────────────────

def fetch_google_rss(query: str, lookback_minutes: int, extra: dict = None) -> list:
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries:
            pub = parse_pub_date(entry.get("published", ""))
            if not is_within_window(pub, lookback_minutes):
                continue
            article = {
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "source": entry.get("source", {}).get("title", "Google News"),
                "published": entry.get("published", ""),
                "published_dt": pub,
            }
            if extra:
                article.update(extra)
            articles.append(article)
        return articles
    except Exception as exc:
        log.error("Google RSS failed for '%s': %s", query, exc)
        return []


def fetch_pr_newswire(keywords: list, lookback_minutes: int, extra: dict = None) -> list:
    try:
        feed = feedparser.parse("https://www.prnewswire.com/rss/news-releases-list.rss")
        articles = []
        for entry in feed.entries:
            text = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
            if not any(kw.lower() in text for kw in keywords):
                continue
            pub = parse_pub_date(entry.get("published", ""))
            if not is_within_window(pub, lookback_minutes):
                continue
            article = {
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "source": "PR Newswire",
                "published": entry.get("published", ""),
                "published_dt": pub,
            }
            if extra:
                article.update(extra)
            articles.append(article)
        return articles
    except Exception as exc:
        log.error("PR Newswire fetch failed: %s", exc)
        return []


def fetch_newsapi_batch(
    companies: list, aliases: dict, lookback_minutes: int, api_key: str
) -> list:
    if not api_key:
        return []
    from_dt = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    query = " OR ".join(
        aliases.get(c, f'"{c}"') for c in companies
    )
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query,
                "from": from_dt,
                "sortBy": "publishedAt",
                "pageSize": 5,
                "apiKey": api_key,
                "language": "en",
            },
            timeout=10,
        )
        resp.raise_for_status()
        articles = []
        for item in resp.json().get("articles", []):
            title = item.get("title", "") or ""
            title_lower = title.lower()
            matched = next(
                (c for c in companies
                 if aliases.get(c, c).strip('"').lower() in title_lower or c.lower() in title_lower),
                "",
            )
            pub = parse_pub_date(item.get("publishedAt", ""))
            articles.append({
                "title": title,
                "url": item.get("url", ""),
                "source": item.get("source", {}).get("name", "NewsAPI"),
                "published": item.get("publishedAt", ""),
                "published_dt": pub,
                "company": matched,
            })
        return articles
    except Exception as exc:
        log.error("NewsAPI batch failed: %s", exc)
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
    try:
        resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=10)
        resp.raise_for_status()
        articles = []
        for hit in resp.json().get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            form_type = src.get("form_type", "Filing")
            pub = parse_pub_date(src.get("file_date", ""))
            articles.append({
                "title": f"{form_type}: {src.get('display_names', company)}",
                "url": (
                    f"https://www.sec.gov/cgi-bin/browse-edgar"
                    f"?action=getcompany&CIK={cik}&type={form_type}&owner=include&count=10"
                ),
                "source": "SEC EDGAR",
                "published": src.get("file_date", ""),
                "published_dt": pub,
                "company": company,
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


# ─── Slack ────────────────────────────────────────────────────────────────────

def send_slack_summary(webhook_url: str, summary: str, label: str, new_sheet_url: Optional[str] = None):
    """Post only the AI summary to Slack — no article list."""
    if not webhook_url or not summary:
        return

    now_str = datetime.now(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")
    text = f"*{label} — {now_str}*\n\n{summary}"

    if new_sheet_url:
        text += f"\n\n_📊 Google Sheet created: {new_sheet_url}_"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "divider"},
    ]
    try:
        resp = requests.post(webhook_url, json={"blocks": blocks}, timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        log.error("Slack post failed: %s", exc)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    topics_cfg, companies_cfg, settings = load_config()

    lookback = settings["lookback"]["summary_minutes"]  # 370 min = ~6hr + buffer
    aliases = companies_cfg.get("aliases", {})
    exec_titles = companies_cfg.get("exec_titles", [])
    edgar_ciks = companies_cfg.get("edgar_ciks", {})
    edgar_cfg = settings["edgar"]
    sheets_cfg = settings["sheets"]

    newsapi_key = os.environ.get("NEWS_API_KEY", "").strip()
    webhook_industry = os.environ.get("SLACK_WEBHOOK_INDUSTRY", "").strip()
    webhook_intel = os.environ.get("SLACK_WEBHOOK_INTEL", "").strip()

    # ── Industry news ──────────────────────────────────────────────────────────
    industry_articles = []
    pr_newswire_fetched = False
    pr_newswire_feed = None

    for topic in topics_cfg["topics"]:
        label = topic["label"]
        for q in topic["queries"]:
            industry_articles.extend(
                fetch_google_rss(q, lookback, extra={"topic": label})
            )
            time.sleep(0.2)

    # PR Newswire — collect all topic keywords in one pass
    all_pr_keywords = [kw for t in topics_cfg["topics"] for kw in t.get("pr_keywords", [])]
    industry_articles.extend(fetch_pr_newswire(all_pr_keywords, lookback, extra={"topic": "Industry"}))

    industry_articles = deduplicate(industry_articles)
    log.info("Industry raw articles: %d", len(industry_articles))

    # ── Company intel ──────────────────────────────────────────────────────────
    company_articles = []
    companies = companies_cfg["priority"]

    for company in companies:
        # General news
        company_articles.extend(
            fetch_google_rss(
                f'"{company}" announcement OR strategy OR acquisition OR partnership OR earnings',
                lookback,
                extra={"company": company},
            )
        )
        # Executive news
        titles_q = " OR ".join(f'"{t}"' for t in exec_titles[:6])
        company_articles.extend(
            fetch_google_rss(
                f'"{company}" ({titles_q})',
                lookback,
                extra={"company": company},
            )
        )
        time.sleep(0.3)

    # PR Newswire — company names
    company_pr_keywords = [aliases.get(c, c).strip('"') for c in companies] + companies
    company_articles.extend(fetch_pr_newswire(company_pr_keywords, lookback))
    # Tag matched company on PR results
    for a in company_articles:
        if not a.get("company"):
            text = a.get("title", "").lower()
            for c in companies:
                alias = aliases.get(c, c).strip('"').lower()
                if alias in text or c.lower() in text:
                    a["company"] = c
                    break

    # NewsAPI in batches of 5
    batch_size = settings["limits"].get("newsapi_page_size", 5)
    if newsapi_key:
        for i in range(0, len(companies), batch_size):
            company_articles.extend(
                fetch_newsapi_batch(companies[i : i + batch_size], aliases, lookback, newsapi_key)
            )
            time.sleep(0.5)

    # SEC EDGAR
    for company in companies:
        cik = edgar_ciks.get(company)
        if not cik:
            continue
        company_articles.extend(
            fetch_edgar_filings(
                company=company,
                cik=cik,
                forms=edgar_cfg["forms"],
                lookback_minutes=lookback,
                user_agent=edgar_cfg["user_agent"],
            )
        )
        time.sleep(edgar_cfg.get("rate_delay_seconds", 0.6))

    company_articles = deduplicate(company_articles)
    log.info("Company raw articles: %d", len(company_articles))

    # ── AI filter + summarise ──────────────────────────────────────────────────
    industry_filtered = filter_relevant_articles(industry_articles)
    company_filtered = filter_relevant_articles(company_articles)

    log.info(
        "After filter — industry: %d, company: %d",
        len(industry_filtered),
        len(company_filtered),
    )

    industry_summary = generate_action_summary(industry_filtered) if industry_filtered else None
    company_summary = generate_action_summary(company_filtered) if company_filtered else None

    # ── Google Sheets ──────────────────────────────────────────────────────────
    all_articles = industry_filtered + company_filtered
    new_sheet_url = None

    if all_articles or industry_summary or company_summary:
        combined_summary = "\n\n".join(
            filter(None, [industry_summary, company_summary])
        )
        new_sheet_url = write_to_sheets(
            sheet_name=sheets_cfg["name"],
            bullets_tab=sheets_cfg["bullets_tab"],
            articles_tab=sheets_cfg["articles_tab"],
            summary=combined_summary or None,
            articles=all_articles,
            source_label="Market Signals",
        )

    # ── Slack ──────────────────────────────────────────────────────────────────
    if industry_summary and webhook_industry:
        send_slack_summary(
            webhook_industry,
            industry_summary,
            "🌿 Industry Signals",
            new_sheet_url,
        )
        log.info("Posted industry summary to Slack.")

    if company_summary and webhook_intel:
        send_slack_summary(
            webhook_intel,
            company_summary,
            "🎯 Company Intelligence",
            new_sheet_url if not industry_summary else None,  # only post URL once
        )
        log.info("Posted company summary to Slack.")

    if not industry_summary and not company_summary:
        log.info("No relevant signals found in the last %d minutes.", lookback)


if __name__ == "__main__":
    main()
