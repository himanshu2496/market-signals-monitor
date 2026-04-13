#!/usr/bin/env python3
"""
fetch_company_intel.py
Monitors ~60 global agribusiness companies + their executives for strategic signals.
Posts to #market-signals-intel via Slack Incoming Webhook.

Modes:
  --mode alerts   Every 2 hours: priority tier (10 companies) only.
                  Sources: Google News RSS + NewsAPI + Adzuna Jobs (optional)
  --mode digest   Daily 9am UTC: all 60 companies.
                  Sources: Google News RSS + NewsAPI + PR Newswire RSS + SEC EDGAR + Adzuna Jobs (optional)

Usage:
  python fetch_company_intel.py --mode alerts
  python fetch_company_intel.py --mode digest

Environment variables required:
  SLACK_WEBHOOK_INTEL  — Slack Incoming Webhook URL for #market-signals-intel
  NEWS_API_KEY         — NewsAPI.org key (free tier: 100 req/day)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser
import requests

from ai_filter import filter_relevant_articles, generate_action_summary

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ── Config paths ──────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COMPANIES_PATH = os.path.join(BASE_DIR, "config", "companies.json")
SETTINGS_PATH = os.path.join(BASE_DIR, "config", "settings.json")


# ── CLI ───────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Company & executive intelligence monitor")
    parser.add_argument(
        "--mode",
        choices=["alerts", "digest"],
        required=True,
        help="alerts = priority companies every 2hr; digest = all companies daily",
    )
    return parser.parse_args()


# ── Loaders ───────────────────────────────────────────────────────────────────
def load_config() -> tuple[dict, dict]:
    with open(COMPANIES_PATH) as f:
        companies_cfg = json.load(f)
    with open(SETTINGS_PATH) as f:
        settings = json.load(f)
    return companies_cfg, settings


# ── Date helpers ──────────────────────────────────────────────────────────────
def parse_pub_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
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


def _time_ago(pub_date: datetime | None) -> str:
    if pub_date is None:
        return "unknown time"
    delta = datetime.now(timezone.utc) - pub_date
    total_minutes = int(delta.total_seconds() / 60)
    if total_minutes < 60:
        return f"{total_minutes}m ago"
    hours = total_minutes // 60
    return f"{hours}h ago"


# ── Google News RSS fetchers ──────────────────────────────────────────────────
def fetch_google_rss(query: str, lookback_minutes: int, max_results: int = 15) -> list[dict]:
    url = (
        f"https://news.google.com/rss/search"
        f"?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    log.debug("Google RSS: %s", url)
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
    return articles


def _search_name(company: str, aliases: dict) -> str:
    """
    Return the best search term for a company name.
    Uses alias if defined, otherwise uses the full company name.
    Never splits on spaces — avoids "John" matching for "John Deere".
    """
    return aliases.get(company, company)


def fetch_google_rss_company(company: str, aliases: dict, lookback_minutes: int) -> list[dict]:
    """General company news: strategy, technology, acquisitions, partnerships."""
    name = _search_name(company, aliases)
    query = f'"{name}" announcement OR strategy OR technology OR acquisition OR partnership OR satellite OR invest'
    articles = fetch_google_rss(query, lookback_minutes)
    for a in articles:
        a["signal_type"] = "news"
    log.info("  Company news: %d results for %s", len(articles), company)
    return articles


def fetch_google_rss_exec(company: str, aliases: dict, exec_titles: list[str], lookback_minutes: int) -> list[dict]:
    """
    Surface executive quotes and appointments.
    Google News indexes many LinkedIn exec posts when they gain traction.
    """
    name = _search_name(company, aliases)
    titles_str = " OR ".join(exec_titles[:6])
    query = f'"{name}" {titles_str}'
    articles = fetch_google_rss(query, lookback_minutes)
    for a in articles:
        a["signal_type"] = "executive"
    log.info("  Exec mentions: %d results for %s", len(articles), company)
    return articles


def fetch_google_rss_jobs(company: str, aliases: dict, lookback_minutes: int) -> list[dict]:
    """Strategic hiring signals from news coverage of job appointments."""
    name = _search_name(company, aliases)
    query = f'"{name}" appointed OR "joins as" OR "named chief" OR "named president" OR "promoted to" OR "new head of" OR "new VP"'
    articles = fetch_google_rss(query, lookback_minutes)
    for a in articles:
        a["signal_type"] = "job_posting"
    log.info("  Hiring signals: %d results for %s", len(articles), company)
    return articles


def fetch_adzuna_jobs(
    company: str,
    aliases: dict,
    lookback_minutes: int,
    app_id: str,
    api_key: str,
    country: str = "us",
    timeout: int = 15,
) -> list[dict]:
    """
    Fetch senior job postings from Adzuna API (free tier: 500 req/day).
    Only called if ADZUNA_APP_ID and ADZUNA_API_KEY are set.
    Register free at: https://developer.adzuna.com
    """
    if not app_id or not api_key:
        return []

    name = _search_name(company, aliases)
    params = {
        "app_id": app_id,
        "app_key": api_key,
        "results_per_page": 5,
        "what": f'"{name}" director OR VP OR chief OR head OR president',
        "sort_by": "date",
        "max_days_old": max(1, lookback_minutes // (60 * 24)),
    }
    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
    log.debug("Adzuna: %s", url)

    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("Adzuna error for %s: %s", company, exc)
        return []

    articles = []
    for item in data.get("results", []):
        created = item.get("created")
        pub_date = parse_pub_date(created)
        if not is_within_window(pub_date, lookback_minutes):
            continue
        title = (item.get("title") or "").strip()
        redirect_url = item.get("redirect_url", "")
        company_name = item.get("company", {}).get("display_name", "")
        articles.append({
            "title": f"[JOB] {title} @ {company_name}",
            "url": redirect_url,
            "source": "Adzuna",
            "published_at": pub_date,
            "origin": "adzuna",
            "signal_type": "job_posting",
        })

    log.info("  Adzuna jobs: %d results for %s", len(articles), company)
    return articles


# ── NewsAPI ───────────────────────────────────────────────────────────────────
def fetch_newsapi_batch(
    companies: list[str],
    aliases: dict[str, str],
    lookback_minutes: int,
    api_key: str,
    page_size: int = 5,
    base_url: str = "https://newsapi.org/v2/everything",
    timeout: int = 15,
) -> dict[str, list[dict]]:
    """
    Batch up to `batch_size` companies per NewsAPI request using OR queries.
    Returns a dict mapping each company name to its list of articles.
    """
    if not api_key:
        log.warning("NEWS_API_KEY not set; skipping NewsAPI.")
        return {}

    from_time = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    results: dict[str, list[dict]] = {c: [] for c in companies}

    for i in range(0, len(companies), 5):
        batch = companies[i:i + 5]
        # Use alias if available, fall back to company name
        terms = [aliases.get(c, c) for c in batch]
        query = " OR ".join(f'"{t}"' for t in terms)

        params = {
            "q": query,
            "language": "en",
            "from": from_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sortBy": "publishedAt",
            "pageSize": page_size,
            "apiKey": api_key,
        }

        log.info("NewsAPI batch: %s", ", ".join(batch))
        try:
            resp = requests.get(base_url, params=params, timeout=timeout)
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            log.error("NewsAPI HTTP error: %s", exc)
            continue
        except requests.exceptions.RequestException as exc:
            log.error("NewsAPI request failed: %s", exc)
            continue

        data = resp.json()
        if data.get("status") != "ok":
            log.error("NewsAPI non-ok: %s", data.get("message", "unknown"))
            continue

        for item in data.get("articles", []):
            pub_date = parse_pub_date(item.get("publishedAt"))
            if not is_within_window(pub_date, lookback_minutes):
                continue

            title = (item.get("title") or "").strip()
            url = item.get("url", "")
            source_name = item.get("source", {}).get("name", "NewsAPI")
            description = (item.get("description") or "").strip()
            search_text = (title + " " + description).lower()

            # Attribute article to the most relevant company in the batch
            # Match only on alias or full company name — never on split first word
            for company, alias in zip(batch, terms):
                if alias.lower() in search_text or company.lower() in search_text:
                    results[company].append({
                        "title": title,
                        "url": url,
                        "source": source_name,
                        "published_at": pub_date,
                        "origin": "newsapi",
                        "signal_type": "news",
                    })
                    break

    return results


# ── PR Newswire ───────────────────────────────────────────────────────────────
def fetch_pr_newswire_all(rss_url: str, lookback_minutes: int) -> list[dict]:
    """Fetch all PR Newswire entries in window. Caller filters by company."""
    log.info("Fetching PR Newswire RSS...")
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
        title = (entry.get("title") or "").strip()
        summary = entry.get("summary", "")
        articles.append({
            "title": title,
            "url": entry.get("link", ""),
            "source": "PR Newswire",
            "published_at": pub_date,
            "origin": "prnewswire",
            "signal_type": "press_release",
            "_search_text": (title + " " + summary).lower(),
        })

    log.info("  → %d PR Newswire articles in window", len(articles))
    return articles


def filter_pr_for_company(pr_articles: list[dict], company: str, alias: str | None) -> list[dict]:
    search_terms = [company.lower()]
    if alias:
        search_terms.append(alias.lower())
    return [a for a in pr_articles if any(t in a["_search_text"] for t in search_terms)]


# ── SEC EDGAR ─────────────────────────────────────────────────────────────────
def fetch_edgar_filings(
    company: str,
    cik: str,
    forms: list[str],
    lookback_minutes: int,
    user_agent: str,
    base_url: str,
    max_filings: int = 3,
    rate_delay: float = 0.6,
) -> list[dict]:
    """
    Fetch recent SEC filings for a company using EDGAR EFTS search API.
    Rate-limited to respect SEC's 10 req/sec guideline.
    """
    from_dt = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    forms_str = ",".join(forms)
    params = {
        "q": f'"{company}"',
        "forms": forms_str,
        "dateRange": "custom",
        "startdt": from_dt.strftime("%Y-%m-%d"),
        "enddt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "_source": "hits.hits._source",
        "hits.hits.total.value": "true",
    }

    headers = {"User-Agent": user_agent}
    log.debug("SEC EDGAR: CIK=%s (%s)", cik, company)

    try:
        time.sleep(rate_delay)
        resp = requests.get(base_url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("EDGAR fetch error for %s: %s", company, exc)
        return []

    filings = []
    hits = data.get("hits", {}).get("hits", [])
    for hit in hits[:max_filings]:
        src = hit.get("_source", {})
        form_type = src.get("form_type", "Filing")
        filed_at_str = src.get("file_date") or src.get("period_of_report")
        pub_date = parse_pub_date(filed_at_str + "T00:00:00Z" if filed_at_str else None)

        # Build EDGAR viewer URL from accession number
        accession = src.get("accession_no", "").replace("-", "")
        cik_clean = cik.lstrip("0")
        if accession:
            url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_clean}&type={form_type}&dateb=&owner=include&count=10"
        else:
            url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_clean}&type=8-K&dateb=&owner=include&count=10"

        description = src.get("entity_name", company)
        period = src.get("period_of_report", "")
        title = f"{form_type}: {description}" + (f" ({period})" if period else "")

        filings.append({
            "title": title,
            "url": url,
            "source": "SEC EDGAR",
            "published_at": pub_date,
            "origin": "sec_edgar",
            "signal_type": "sec_filing",
        })

    return filings


# ── Deduplication ─────────────────────────────────────────────────────────────
def deduplicate(articles: list[dict]) -> list[dict]:
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


# ── Article tagging ───────────────────────────────────────────────────────────
def get_signal_tag(article: dict) -> str:
    origin = article.get("origin", "")
    signal_type = article.get("signal_type", "news")
    if origin == "prnewswire" or signal_type == "press_release":
        return "📢"
    if origin == "sec_edgar" or signal_type == "sec_filing":
        return "📋"
    if origin == "linkedin" or signal_type == "linkedin":
        return "🔗"
    if origin == "indeed" or signal_type == "job_posting":
        return "💼"
    if signal_type == "executive":
        return "👤"
    return "📰"


# ── Slack Block Kit builders ──────────────────────────────────────────────────
def build_alert_blocks(company: str, articles: list[dict], max_articles: int) -> list[dict]:
    """Compact Block Kit for a single company real-time alert."""
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
                "text": f"🔔 {company}  —  {count} new signal{'s' if count != 1 else ''}",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    for article in sorted_articles:
        title = article["title"]
        if len(title) > 120:
            title = title[:117] + "..."
        tag = get_signal_tag(article)
        time_label = _time_ago(article["published_at"])
        url = article["url"]
        source = article["source"]
        reason = article.get("relevance_reason", "")

        # Title as its own section for visual weight
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{tag}  *<{url}|{title}>*",
            },
        })

        # Metadata + reason as a context block (smaller grey text)
        context_text = f"*{source}*   ·   {time_label}"
        if reason:
            context_text += f"   ·   _{reason}_"
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": context_text}],
        })

    blocks.append({"type": "divider"})
    return blocks


def build_action_summary_blocks(summary: str) -> list[dict]:
    """Top-of-digest block showing Claude's 'what to act on' bullets."""
    now_str = datetime.now(timezone.utc).strftime("%a %b %-d")
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🎯 What to act on — {now_str}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary},
        },
        {"type": "divider"},
    ]


def build_digest_blocks(
    company_results: list[tuple[str, list[dict]]],
    max_per_company: int,
) -> list[dict]:
    """Daily digest Block Kit covering all companies with articles."""
    now_str = datetime.now(timezone.utc).strftime("%a %b %-d")
    total = sum(
        min(len(articles), max_per_company) for _, articles in company_results
    )
    company_count = len(company_results)

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📊 Market Signals Daily — {now_str}  ·  {total} signals across {company_count} companies",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    for company, articles in company_results:
        sorted_articles = sorted(
            articles,
            key=lambda a: a["published_at"] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )[:max_per_company]

        # Company name as a bold section header
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{company}*",
            },
        })

        for article in sorted_articles:
            title = article["title"]
            if len(title) > 110:
                title = title[:107] + "..."
            tag = get_signal_tag(article)
            time_label = _time_ago(article["published_at"])
            url = article["url"]
            source = article["source"]

            reason = article.get("relevance_reason", "")
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{tag}  <{url}|{title}>"},
            })
            context_text = f"*{source}*   ·   {time_label}"
            if reason:
                context_text += f"   ·   _{reason}_"
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": context_text}],
            })

        blocks.append({"type": "divider"})

    return blocks


def build_footer_block(mode: str) -> dict:
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    label = "Real-Time Alert" if mode == "alerts" else "Daily Digest"
    return {
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f":robot_face: *Market Signals — Intel ({label})*   ·   {run_time}",
            }
        ],
    }


# ── Slack sender ──────────────────────────────────────────────────────────────
def send_slack(webhook_url: str, blocks: list[dict], max_blocks: int = 47) -> bool:
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
    # Support local .env loading
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    args = parse_args()
    mode = args.mode

    slack_webhook = os.environ.get("SLACK_WEBHOOK_INTEL", "").strip()
    news_api_key = os.environ.get("NEWS_API_KEY", "").strip()
    adzuna_app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    adzuna_api_key = os.environ.get("ADZUNA_API_KEY", "").strip()

    if not slack_webhook:
        log.error("SLACK_WEBHOOK_INTEL is not set. Aborting.")
        sys.exit(1)

    companies_cfg, settings = load_config()

    # Determine companies and lookback window for this mode
    if mode == "alerts":
        companies = companies_cfg["priority_tier"]
        lookback_minutes = settings["lookback"]["alert_minutes"]
        max_articles = settings["limits"]["max_articles_per_company_alert"]
        log.info("Mode: ALERTS — %d priority companies, lookback %dmin", len(companies), lookback_minutes)
    else:
        companies = companies_cfg["all_companies"]
        lookback_minutes = int(settings["lookback"]["digest_hours"] * 60)
        max_articles = settings["limits"]["max_articles_per_company_digest"]
        log.info("Mode: DIGEST — %d companies, lookback %dmin", len(companies), lookback_minutes)

    aliases = companies_cfg.get("newsapi_search_aliases", {})
    exec_titles = companies_cfg.get("exec_titles", [])
    edgar_ciks = companies_cfg.get("sec_edgar_ciks", {})
    pr_rss_url = settings["sources"]["prnewswire_rss"]
    edgar_base = settings["sources"]["edgar_efts_base"]
    edgar_user_agent = settings["edgar"]["user_agent"]
    edgar_forms = settings["edgar"]["forms_to_watch"]
    edgar_rate_delay = settings["edgar"]["rate_limit_delay_seconds"]
    max_blocks = settings["slack"]["max_blocks_per_message"]
    newsapi_page_size = settings["limits"]["newsapi_page_size"]

    # Pre-fetch shared resources
    pr_articles = fetch_pr_newswire_all(pr_rss_url, lookback_minutes) if mode == "digest" else []

    # NewsAPI: batch all companies at once
    newsapi_results = fetch_newsapi_batch(
        companies=companies,
        aliases=aliases,
        lookback_minutes=lookback_minutes,
        api_key=news_api_key,
        page_size=newsapi_page_size,
    )

    # Per-company collection
    company_results: list[tuple[str, list[dict]]] = []

    for company in companies:
        log.info("--- Company: %s ---", company)
        raw: list[dict] = []

        # Google News RSS: company news + exec mentions + hiring signals (all modes)
        raw.extend(fetch_google_rss_company(company, aliases, lookback_minutes))
        raw.extend(fetch_google_rss_exec(company, aliases, exec_titles, lookback_minutes))
        raw.extend(fetch_google_rss_jobs(company, aliases, lookback_minutes))

        # Adzuna job postings (if credentials set)
        raw.extend(fetch_adzuna_jobs(company, aliases, lookback_minutes, adzuna_app_id, adzuna_api_key))

        # For digest only: PR Newswire + SEC EDGAR
        if mode == "digest":
            alias = aliases.get(company)
            raw.extend(filter_pr_for_company(pr_articles, company, alias))
            if company in edgar_ciks:
                raw.extend(
                    fetch_edgar_filings(
                        company=company,
                        cik=edgar_ciks[company],
                        forms=edgar_forms,
                        lookback_minutes=lookback_minutes,
                        user_agent=edgar_user_agent,
                        base_url=edgar_base,
                        rate_delay=edgar_rate_delay,
                    )
                )

        # Merge NewsAPI results
        raw.extend(newsapi_results.get(company, []))

        unique = deduplicate(raw)
        log.info("  %d unique articles before AI filter", len(unique))

        if not unique:
            continue

        # AI relevance filter — keeps only articles scoring >= 6
        relevant = filter_relevant_articles(unique)
        log.info("  %d relevant articles after AI filter", len(relevant))

        if not relevant:
            continue

        company_results.append((company, relevant))

        # In alerts mode: post each company immediately as signals are found
        if mode == "alerts":
            blocks = build_alert_blocks(company, relevant, max_articles)
            blocks.append(build_footer_block(mode))
            send_slack(slack_webhook, blocks, max_blocks)

    if not company_results:
        log.info("No relevant signals found. No Slack message sent.")
        return

    # In digest mode: send one consolidated message with action summary at top
    if mode == "digest":
        # Flatten all relevant articles for action summary generation
        all_relevant = [a for _, articles in company_results for a in articles]
        summary = generate_action_summary(all_relevant)

        blocks: list[dict] = []
        if summary:
            blocks.extend(build_action_summary_blocks(summary))

        total_scanned = sum(len(articles) for _, articles in company_results)
        # Add supporting signals header
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Supporting signals — {len(all_relevant)} relevant signals across {len(company_results)} companies*",
            },
        })
        blocks.append({"type": "divider"})

        blocks.extend(build_digest_blocks(company_results, max_articles))
        blocks.append(build_footer_block(mode))
        send_slack(slack_webhook, blocks, max_blocks)


if __name__ == "__main__":
    main()
