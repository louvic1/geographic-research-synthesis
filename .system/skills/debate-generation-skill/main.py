"""
debate-generation-skill
Generates a structured academic debate from a set of research sources.
Identifies perspectives, contradictions, gaps, and assumptions.

Usage:
    python main.py --topic "urban heat islands" \
                   --sources-file extracted.json \
                   --n-perspectives 3 \
                   --output debate.json
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

load_dotenv()

CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_SOURCES_IN_PROMPT = 20
MAX_CHARS_PER_SOURCE = 800


# ---------------------------------------------------------------------------
# Source normalization
# ---------------------------------------------------------------------------

def normalize_sources(sources: list) -> list[dict]:
    """
    Normalize heterogeneous source inputs into a flat list of dicts with
    at minimum: title, text, url, relevance_score.
    Accepts:
    - str  → treated as raw text
    - dict from notebooklm-research-skill (has "summary", "title", "url")
    - dict from data-extraction-skill (has "source", "concepts", "metrics", etc.)
    """
    normalized: list[dict] = []
    for s in sources:
        if isinstance(s, str):
            normalized.append({
                "title": s[:60],
                "text": s,
                "url": "",
                "relevance_score": 0.5,
            })
        elif isinstance(s, dict):
            # Determine text content
            text = (
                s.get("summary")
                or s.get("abstract")
                or _flatten_extraction_record(s)
                or str(s)
            )
            normalized.append({
                "title": s.get("title", s.get("source", "")[:60]),
                "text": text,
                "url": s.get("url", s.get("source", "")),
                "relevance_score": s.get("relevance_score", 0.5),
            })
    return normalized


def _flatten_extraction_record(record: dict) -> str:
    """Convert a data-extraction-skill record to readable text."""
    parts: list[str] = []
    for metric in record.get("metrics", []):
        parts.append(f"Metric: {metric.get('value')} {metric.get('unit')} — {metric.get('context')}")
    for concept in record.get("concepts", []):
        parts.append(f"Concept: {concept}")
    for loc in record.get("locations", []):
        parts.append(f"Location: {loc.get('name')} ({loc.get('type')})")
    for entity in record.get("entities", []):
        parts.append(f"Entity: {entity.get('name')} ({entity.get('type')})")
    return ". ".join(parts)


def select_top_sources(sources: list[dict], max_n: int) -> list[dict]:
    """Sort by relevance_score descending, take top N."""
    return sorted(sources, key=lambda s: s.get("relevance_score", 0), reverse=True)[:max_n]


def build_sources_block(sources: list[dict]) -> str:
    """Build a numbered source block for the prompt."""
    lines: list[str] = []
    for i, s in enumerate(sources, 1):
        text = s["text"][:MAX_CHARS_PER_SOURCE].replace("\n", " ")
        title = s["title"][:80]
        url = s["url"]
        line = f"[{i}] {title}"
        if url:
            line += f" ({url})"
        line += f"\n    {text}"
        lines.append(line)
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

DEBATE_PROMPT = """You are an academic research analyst. Your job is to map the intellectual landscape of a research topic by identifying the main perspectives, contradictions, gaps, and assumptions present across a set of sources.

Topic: {topic}
Number of perspectives to identify: {n_perspectives}

Sources:
{sources_block}

Generate a structured debate analysis. Return ONLY valid JSON with this exact structure:

{{
  "topic": "{topic}",
  "perspectives": [
    {{
      "label": "Short label for this perspective (e.g. 'Green Infrastructure Advocates')",
      "claim": "The core claim or thesis of this perspective",
      "evidence": ["Evidence point 1 from the sources", "Evidence point 2"],
      "sources": ["[1]", "[3]"]
    }}
  ],
  "contradictions": [
    {{
      "claim_a": "Claim from one source or perspective",
      "claim_b": "Contradicting claim from another source",
      "source_a": "[2]",
      "source_b": "[5]",
      "note": "Why these claims appear to contradict (different scale, method, context, etc.)"
    }}
  ],
  "gaps": [
    "Research gap or unanswered question identified across the sources"
  ],
  "assumptions": [
    "Hidden assumption made by most sources in this field"
  ],
  "synthesis": "2-3 sentence synthesis: what is the core intellectual tension, and what would it take to resolve it?"
}}

Rules:
- Identify exactly {n_perspectives} distinct perspectives (not more, not fewer)
- Each perspective must be grounded in at least one source — cite using [N] notation
- Contradictions must cite specific source numbers
- Gaps and assumptions should reflect patterns across multiple sources, not just one
- Do not invent findings — only use what is in the sources
- Return ONLY the JSON object, no explanation or markdown"""


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------

async def generate_debate(
    topic: str,
    sources: list[dict],
    n_perspectives: int,
    client: anthropic.AsyncAnthropic,
) -> dict:
    top_sources = select_top_sources(sources, MAX_SOURCES_IN_PROMPT)
    sources_block = build_sources_block(top_sources)

    prompt = DEBATE_PROMPT.format(
        topic=topic,
        n_perspectives=n_perspectives,
        sources_block=sources_block,
    )

    print(f"[debate] Sending {len(top_sources)} sources to Claude ({CLAUDE_MODEL})...")

    message = await client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[debate] JSON parse error: {e}", file=sys.stderr)
        print(f"[debate] Raw response (first 500 chars):\n{raw[:500]}", file=sys.stderr)
        # Return a minimal fallback structure
        return {
            "topic": topic,
            "perspectives": [],
            "contradictions": [],
            "gaps": [],
            "assumptions": [],
            "synthesis": raw,  # store raw text as synthesis fallback
            "error": "JSON parse failed — raw response stored in synthesis field",
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_debate(
    sources: list,
    topic: str,
    n_perspectives: int = 3,
    output_path: Optional[str] = None,
) -> dict:
    """
    Generate a structured academic debate from a list of sources.
    Returns the debate dict.
    """
    n_perspectives = max(2, min(n_perspectives, 5))

    print(f"\n[debate] Topic: '{topic}'")
    print(f"[debate] {len(sources)} source(s), {n_perspectives} perspectives requested")

    normalized = normalize_sources(sources)
    if not normalized:
        raise ValueError("No sources provided")

    ai_client = anthropic.AsyncAnthropic()
    debate = await generate_debate(topic, normalized, n_perspectives, ai_client)

    if output_path:
        Path(output_path).write_text(json.dumps(debate, indent=2, ensure_ascii=False))
        print(f"[debate] Saved to: {output_path}")

    n_perspectives_found = len(debate.get("perspectives", []))
    n_contradictions = len(debate.get("contradictions", []))
    n_gaps = len(debate.get("gaps", []))
    print(f"[debate] Done — {n_perspectives_found} perspectives, {n_contradictions} contradictions, {n_gaps} gaps")

    return debate


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="debate-generation-skill")
    parser.add_argument("--topic", required=True)
    parser.add_argument("--sources-file", default=None,
                        help="JSON file from notebooklm-research-skill or data-extraction-skill")
    parser.add_argument("--sources-text", nargs="+", default=None,
                        help="Raw text strings as sources (for quick testing)")
    parser.add_argument("--n-perspectives", type=int, default=3)
    parser.add_argument("--output", default=None, help="Output JSON file path")
    args = parser.parse_args()

    if args.sources_file:
        raw = json.loads(Path(args.sources_file).read_text())
        # Handle both list-of-sources and full research output dict
        if isinstance(raw, dict) and "sources" in raw:
            sources = raw["sources"]
        elif isinstance(raw, list):
            sources = raw
        else:
            print("[debate] Unrecognized sources file format", file=sys.stderr)
            sys.exit(1)
    elif args.sources_text:
        sources = args.sources_text
    else:
        print("[debate] Provide --sources-file or --sources-text", file=sys.stderr)
        sys.exit(1)

    debate = asyncio.run(run_debate(
        sources=sources,
        topic=args.topic,
        n_perspectives=args.n_perspectives,
        output_path=args.output,
    ))

    if not args.output:
        print(json.dumps(debate, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
