#!/usr/bin/env python3
"""
ai_filter.py — Gemini-powered relevance scoring and action summary.

filter_relevant_articles(articles) -> list
    Scores each article 1-10. Keeps only those scoring >= 6.
    Adds 'relevance_score' and 'relevance_reason' to each kept article.

generate_action_summary(articles) -> str | None
    Returns 3-5 concrete "what to act on" bullets for the team.

Both degrade gracefully if GEMINI_API_KEY is not set.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

MODEL = "gemini-2.0-flash"
MIN_SCORE = 6

CONTEXT = """You are a market intelligence analyst for SatSure, which sells:
- Pulpwood and timber market intelligence (satellite crop monitoring, yield forecasting)
- Site suitability assessment for forestry and agriculture
- Vegetation management solutions for utilities, forestry, and land managers

Target customers: large agribusinesses, forestry companies, utilities, land managers.
We care about: competitor moves in agri-intelligence/satellite/precision forestry,
pulpwood/timber market shifts, vegetation management trends, strategic exec appointments,
acquisitions/partnerships in our space, regulatory changes affecting forest/land management."""


def _client():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    try:
        from google import genai
        return genai.Client(api_key=key)
    except ImportError:
        log.error("google-genai not installed. Run: pip install google-genai")
        return None


def filter_relevant_articles(articles: list) -> list:
    if not articles:
        return articles
    client = _client()
    if not client:
        log.warning("GEMINI_API_KEY not set — no filtering applied.")
        return articles

    numbered = "\n".join(
        f'{i}. "{a["title"]}" — {a.get("source","?")}'
        for i, a in enumerate(articles)
    )
    prompt = f"""{CONTEXT}

Rate each article 1-10 for relevance to our business.
8-10: Directly actionable (competitor move, market shift, exec change in our space)
6-7: Useful context (adjacent market, regulatory, macro trend)
1-5: Not relevant

Return ONLY a JSON array, no other text:
[{{"index": 0, "score": 8, "reason": "one line why it matters"}}]

Articles:
{numbered}"""

    try:
        resp = client.models.generate_content(model=MODEL, contents=prompt)
        raw = resp.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        scores = json.loads(raw)
    except Exception as exc:
        log.error("Gemini scoring failed: %s", exc)
        return articles

    score_map = {s["index"]: s for s in scores}
    kept, dropped = [], 0
    for i, a in enumerate(articles):
        entry = score_map.get(i)
        if not entry:
            kept.append(a)
            continue
        if entry.get("score", 0) >= MIN_SCORE:
            a["relevance_score"] = entry["score"]
            a["relevance_reason"] = entry.get("reason", "")
            kept.append(a)
        else:
            dropped += 1
    log.info("Relevance filter: kept %d/%d (dropped %d)", len(kept), len(articles), dropped)
    return kept


def generate_action_summary(articles: list) -> Optional[str]:
    if not articles:
        return None
    client = _client()
    if not client:
        return None

    lines = []
    for a in articles[:30]:
        line = f'- "{a["title"]}" ({a.get("source","?")})'
        if a.get("relevance_reason"):
            line += f' — {a["relevance_reason"]}'
        lines.append(line)

    prompt = f"""{CONTEXT}

Based on these market signals, write 3-5 concrete action bullets for our sales and strategy team.
Be specific: name companies, technologies, trends.
Focus on what we should DO (reach out, monitor, adjust pitch, prepare for).
Each bullet max 2 sentences. Start each with "•".
No intro or conclusion — just the bullets.

Signals:
{chr(10).join(lines)}"""

    try:
        resp = client.models.generate_content(model=MODEL, contents=prompt)
        summary = resp.text.strip()
        log.info("Action summary generated (%d chars)", len(summary))
        return summary
    except Exception as exc:
        log.error("Gemini summary failed: %s", exc)
        return None
