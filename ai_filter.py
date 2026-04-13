#!/usr/bin/env python3
"""
ai_filter.py
Claude-powered relevance filtering and action summary generation.

Provides two functions:
  filter_relevant_articles() — scores articles 1-10 for business relevance,
                               keeps only those scoring >= MIN_RELEVANCE_SCORE
  generate_action_summary()  — synthesizes high-relevance signals into a
                               concrete "what to act on" narrative for the team

Both functions degrade gracefully: if ANTHROPIC_API_KEY is not set, articles
pass through unfiltered and no summary is generated.
"""

import json
import logging
import os

log = logging.getLogger(__name__)

MIN_RELEVANCE_SCORE = 6  # Articles scoring below this are filtered out
MODEL = "gemini-2.0-flash"  # Free tier: 1,500 req/day, 1M tokens/day

BUSINESS_CONTEXT = """You are a market intelligence analyst for SatSure, a company that sells:
- Pulpwood and timber market intelligence (crop monitoring, yield forecasting)
- Site suitability assessment services for forestry and agriculture
- Vegetation management solutions for utilities, forestry, and land managers

Our target customers are large agribusinesses, forestry companies, utilities, and land management firms.
We care deeply about:
- Strategic moves by competitors in agri-intelligence, satellite/remote sensing, and precision forestry
- Market shifts in pulpwood, timber, and vegetation management
- Technology investments (satellite, AI, GIS) by our target customers
- Executive appointments that signal strategic direction changes
- Acquisitions, partnerships, or new product launches in our space
- Regulatory changes affecting forest management and land use"""


def _get_client():
    """Return a Gemini GenerativeModel, or None if the key is not set."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        return genai.GenerativeModel(
            model_name=MODEL,
            system_instruction=BUSINESS_CONTEXT,
        )
    except ImportError:
        log.error("google-generativeai package not installed. Run: pip install google-generativeai")
        return None


def filter_relevant_articles(articles: list[dict]) -> list[dict]:
    """
    Score each article for business relevance using Claude.
    Returns only articles scoring >= MIN_RELEVANCE_SCORE, with
    'relevance_score' and 'relevance_reason' fields added.

    If ANTHROPIC_API_KEY is not set, returns all articles unchanged.
    """
    if not articles:
        return articles

    client = _get_client()
    if client is None:
        log.warning("GEMINI_API_KEY not set — skipping relevance filter, passing all %d articles.", len(articles))
        return articles

    # Build a numbered list of article titles for the prompt
    article_list = "\n".join(
        f'{i}. "{a["title"]}" — {a.get("source", "unknown")}'
        for i, a in enumerate(articles)
    )

    prompt = f"""Rate each article for relevance to our business on a scale of 1-10.

Scoring guide:
- 8-10: Directly actionable — competitor move, market shift, tech investment, or exec change in our exact space
- 6-7: Relevant context — adjacent market, supply chain, regulatory, or macro trend we should monitor
- 1-5: Not relevant — generic company news, unrelated products, or too vague to act on

Return ONLY a JSON array. No explanation outside the JSON.
Format: [{{"index": 0, "score": 8, "reason": "one concise line explaining why it matters to us"}}]

Articles:
{article_list}"""

    try:
        response = client.generate_content(prompt)
        raw = response.text.strip()

        # Strip markdown code fences if Claude wrapped the JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        scores = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("Claude returned invalid JSON for relevance scoring: %s", exc)
        return articles
    except Exception as exc:
        log.error("Claude API error during relevance filtering: %s", exc)
        return articles

    # Build a lookup from index → score/reason
    score_map = {item["index"]: item for item in scores}

    kept = []
    filtered_count = 0
    for i, article in enumerate(articles):
        entry = score_map.get(i)
        if entry is None:
            # Claude didn't score this article — include it conservatively
            article["relevance_score"] = 5
            article["relevance_reason"] = "Not scored"
            kept.append(article)
            continue

        score = entry.get("score", 0)
        if score >= MIN_RELEVANCE_SCORE:
            article["relevance_score"] = score
            article["relevance_reason"] = entry.get("reason", "")
            kept.append(article)
        else:
            filtered_count += 1
            log.debug(
                "Filtered (score %d): %s — %s",
                score, article["title"], entry.get("reason", ""),
            )

    log.info(
        "Relevance filter: %d/%d articles kept (threshold=%d), %d filtered out.",
        len(kept), len(articles), MIN_RELEVANCE_SCORE, filtered_count,
    )
    return kept


def generate_action_summary(articles: list[dict]) -> str | None:
    """
    Generate a 3-5 bullet "what to act on" narrative from high-relevance articles.
    Returns a markdown string for inclusion in Slack, or None if generation fails
    or there are no articles.

    Only called for digest mode.
    """
    if not articles:
        return None

    client = _get_client()
    if client is None:
        return None

    # Summarise articles for the prompt — include relevance reason if available
    signal_lines = []
    for a in articles[:30]:  # Cap at 30 to stay within token limits
        reason = a.get("relevance_reason", "")
        line = f'- "{a["title"]}" ({a.get("source", "unknown")})'
        if reason:
            line += f" — {reason}"
        signal_lines.append(line)

    signals_text = "\n".join(signal_lines)

    prompt = f"""Based on today's market signals below, write 3-5 concrete bullet points for our sales and strategy team.

Rules:
- Be specific: name companies, technologies, and trends
- Focus on what we should DO (reach out, monitor, adjust pitch, prepare for)
- Prioritise signals that suggest near-term opportunities or competitive threats
- Keep each bullet to 1-2 sentences max

Market signals:
{signals_text}

Return only the bullet points, starting each with "•". No intro sentence, no conclusion."""

    try:
        response = client.generate_content(prompt)
        summary = response.text.strip()
        log.info("Action summary generated (%d chars).", len(summary))
        return summary
    except Exception as exc:
        log.error("Gemini API error during action summary generation: %s", exc)
        return None
