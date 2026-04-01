"""
notebooklm-research-skill
Deep academic research using Semantic Scholar, arXiv, and Google NotebookLM.

Usage:
    python main.py --topic "urban heat islands" --max-sources 50 --output results.json
"""

import asyncio
import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional

import os
import re
import unicodedata

import anthropic
import httpx
from dotenv import load_dotenv

load_dotenv()

SEMANTIC_SCHOLAR_API_KEY = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class Source:
    def __init__(
        self,
        title: str,
        url: str,
        year: Optional[int],
        citations: int,
        authors: list[str],
        abstract: str,
        source_type: str,  # "peer-reviewed" | "preprint" | "general"
    ):
        self.title = title
        self.url = url
        self.year = year
        self.citations = citations
        self.authors = authors
        self.abstract = abstract
        self.source_type = source_type
        self.summary: str = ""
        self.relevance_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "summary": self.summary or self.abstract,
            "relevance_score": round(self.relevance_score, 3),
            "source_type": self.source_type,
            "year": self.year,
            "citations": self.citations,
            "authors": self.authors,
        }


# ---------------------------------------------------------------------------
# Academic search: Semantic Scholar
# ---------------------------------------------------------------------------

async def search_semantic_scholar(
    query: str,
    limit: int = 50,
    min_year: int = 2015,
) -> list[Source]:
    """Search Semantic Scholar for peer-reviewed papers."""
    sources: list[Source] = []
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": min(limit, 100),
        "fields": "title,year,citationCount,authors,externalIds,abstract,openAccessPdf",
        "year": f"{min_year}-",
    }
    headers = {}
    if SEMANTIC_SCHOLAR_API_KEY:
        headers["x-api-key"] = SEMANTIC_SCHOLAR_API_KEY

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[semantic-scholar] search failed: {e}", file=sys.stderr)
            return sources

    for paper in data.get("data", []):
        # Prefer open-access PDF, fall back to DOI, then Semantic Scholar page
        pdf_info = paper.get("openAccessPdf") or {}
        doi = (paper.get("externalIds") or {}).get("DOI")
        if pdf_info.get("url"):
            paper_url = pdf_info["url"]
        elif doi:
            paper_url = f"https://doi.org/{doi}"
        else:
            paper_url = f"https://www.semanticscholar.org/paper/{paper.get('paperId', '')}"

        sources.append(Source(
            title=paper.get("title", "Untitled"),
            url=paper_url,
            year=paper.get("year"),
            citations=paper.get("citationCount", 0),
            authors=[a["name"] for a in (paper.get("authors") or [])],
            abstract=paper.get("abstract", ""),
            source_type="peer-reviewed",
        ))

    print(f"[semantic-scholar] found {len(sources)} papers")
    return sources


# ---------------------------------------------------------------------------
# Academic search: arXiv
# ---------------------------------------------------------------------------

async def search_arxiv(
    query: str,
    limit: int = 30,
    min_year: int = 2015,
) -> list[Source]:
    """Search arXiv for preprints."""
    sources: list[Source] = []
    url = "https://export.arxiv.org/api/query"
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": limit,
        "sortBy": "relevance",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        except Exception as e:
            print(f"[arxiv] search failed: {e}", file=sys.stderr)
            return sources

    ns = "http://www.w3.org/2005/Atom"
    root = ET.fromstring(resp.text)
    for entry in root.findall(f"{{{ns}}}entry"):
        published = entry.findtext(f"{{{ns}}}published", "")
        year = int(published[:4]) if published else None
        if year and year < min_year:
            continue

        paper_url = ""
        for link in entry.findall(f"{{{ns}}}link"):
            if link.get("type") == "application/pdf":
                paper_url = link.get("href", "")
                break
        if not paper_url:
            alt = entry.find(f"{{{ns}}}link[@rel='alternate']")
            paper_url = alt.get("href", "") if alt is not None else ""

        sources.append(Source(
            title=entry.findtext(f"{{{ns}}}title", "").strip(),
            url=paper_url,
            year=year,
            citations=0,  # arXiv doesn't expose citation counts
            authors=[
                a.findtext(f"{{{ns}}}name", "")
                for a in entry.findall(f"{{{ns}}}author")
            ],
            abstract=entry.findtext(f"{{{ns}}}summary", "").strip(),
            source_type="preprint",
        ))

    print(f"[arxiv] found {len(sources)} preprints")
    return sources


# ---------------------------------------------------------------------------
# Query decomposition (Claude Haiku)
# ---------------------------------------------------------------------------

DECOMPOSE_PROMPT = """You are a research librarian. Given a research topic, generate {n} precise academic search queries that will find the most relevant peer-reviewed papers.

Rules:
- Each query must be short (3-6 words max) and specific
- Include geographic terms where relevant (city, region, country names)
- If the topic is in French or concerns Quebec/Montreal/Canada, include at least one French-language query
- Vary the angle: one geographic-specific, one methodological, one thematic
- Avoid stop words and generic terms like "impact" or "study"

Topic: {topic}

Return ONLY a JSON array of strings, e.g.: ["query one", "query two", "query three"]"""


async def decompose_query(topic: str, n: int = 4) -> list[str]:
    """Use Claude Haiku to decompose a topic into targeted sub-queries."""
    try:
        client = anthropic.AsyncAnthropic()
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": DECOMPOSE_PROMPT.format(topic=topic, n=n)}],
        )
        raw = message.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        queries = json.loads(raw)
        if isinstance(queries, list) and all(isinstance(q, str) for q in queries):
            print(f"[research] sub-queries: {queries}")
            return queries[:n]
    except Exception as e:
        print(f"[research] query decomposition failed ({e}), using original topic", file=sys.stderr)
    return [topic]


# ---------------------------------------------------------------------------
# Academic search: OpenAlex
# ---------------------------------------------------------------------------

async def search_openalex(
    query: str,
    limit: int = 25,
    min_year: int = 2015,
) -> list[Source]:
    """Search OpenAlex — better geographic indexing than Semantic Scholar, free."""
    sources: list[Source] = []
    url = "https://api.openalex.org/works"
    params = {
        "search": query,
        "per-page": min(limit, 50),
        "filter": f"publication_year:>{min_year - 1},type:article",
        "select": "title,publication_year,cited_by_count,authorships,doi,abstract_inverted_index,open_access",
        "sort": "relevance_score:desc",
        "mailto": "research@example.com",  # OpenAlex polite pool
    }
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[openalex] search failed: {e}", file=sys.stderr)
            return sources

    for work in data.get("results", []):
        doi = work.get("doi") or ""
        paper_url = doi if doi else ""
        oa_url = (work.get("open_access") or {}).get("oa_url") or ""
        if oa_url:
            paper_url = oa_url

        # OpenAlex stores abstracts as inverted index — reconstruct
        abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))

        authors = [
            a.get("author", {}).get("display_name", "")
            for a in (work.get("authorships") or [])[:5]
        ]

        sources.append(Source(
            title=work.get("title") or "Untitled",
            url=paper_url,
            year=work.get("publication_year"),
            citations=work.get("cited_by_count", 0),
            authors=authors,
            abstract=abstract,
            source_type="peer-reviewed",
        ))

    print(f"[openalex] found {len(sources)} papers for: '{query}'")
    return sources


def _reconstruct_abstract(inverted_index: Optional[dict]) -> str:
    """Reconstruct abstract text from OpenAlex inverted index format."""
    if not inverted_index:
        return ""
    positions: dict[int, str] = {}
    for word, pos_list in inverted_index.items():
        for pos in pos_list:
            positions[pos] = word
    return " ".join(positions[i] for i in sorted(positions))


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------


# Stop words to filter out before keyword matching (French + English)
_STOP_WORDS = {
    # French
    "de", "du", "des", "le", "la", "les", "l", "un", "une", "en", "et", "ou",
    "dans", "sur", "par", "pour", "avec", "au", "aux", "à", "d", "se", "ce",
    "qui", "que", "quoi", "dont", "où", "est", "sont", "a", "ont", "être",
    "leur", "leurs", "il", "elle", "ils", "elles", "nous", "vous", "on",
    "mais", "car", "ni", "si", "car", "lors", "sous", "sans",
    # English
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or",
    "by", "with", "from", "as", "is", "are", "was", "were", "be", "been",
    "its", "their", "this", "that", "these", "those", "it", "its",
    "impact", "study", "analysis", "research", "review", "using",
}


def _normalize(text: str) -> str:
    """Lowercase + strip accents for accent-insensitive matching."""
    return unicodedata.normalize("NFD", text.lower()).encode("ascii", "ignore").decode()


def _extract_keywords(topic: str) -> tuple[set[str], set[str]]:
    """
    Extract two sets from a topic string:
    - keywords: meaningful content words (stop words removed, min 3 chars)
    - geo_terms: geographic proper nouns (capitalized words, known place names)
    """
    # Normalize: remove punctuation, split
    words = re.sub(r"[''\"().,;:!?]", " ", topic).split()

    keywords: set[str] = set()
    geo_terms: set[str] = set()

    # Known geographic terms to always capture (case-insensitive match later)
    known_geo = {
        "montreal", "montréal", "québec", "quebec", "canada", "canadian",
        "laurentides", "montérégie", "lanaudière", "laurentian",
        "greater montreal", "grand montréal", "île de montréal",
    }

    for word in words:
        clean = _normalize(word.strip("'"))
        if len(clean) < 3:
            continue
        if clean in _STOP_WORDS:
            continue
        keywords.add(clean)
        # Geo: starts with capital or is a known geo term
        if word[0].isupper() or clean in known_geo:
            geo_terms.add(clean)

    return keywords, geo_terms


def score_sources(sources: list[Source], topic: str) -> list[Source]:
    """
    Score each source 0–1. Keyword relevance to the original topic is dominant.

    Weights:
    - Keyword match in TITLE   : up to 0.40  (title match = highly on-topic)
    - Keyword match in ABSTRACT: up to 0.25  (abstract match = relevant content)
    - Geographic match         : up to 0.15  (same place names as the topic)
    - Source type              : up to 0.10  (peer-reviewed preferred)
    - Citations (normalized)   : up to 0.05  (quality signal, not dominant)
    - Recency (last 5 years)   :      0.05
    """
    keywords, geo_terms = _extract_keywords(topic)
    max_citations = max((s.citations for s in sources), default=1) or 1
    current_year = datetime.now().year

    for source in sources:
        score = 0.0
        title = _normalize(source.title or "")
        abstract = _normalize(source.abstract or "")

        # --- Keyword match in title (dominant signal) ---
        if keywords:
            title_hits = sum(1 for kw in keywords if kw in title)
            score += 0.40 * min(title_hits / len(keywords), 1.0)

        # --- Keyword match in abstract ---
        if keywords:
            abstract_hits = sum(1 for kw in keywords if kw in abstract)
            score += 0.25 * min(abstract_hits / len(keywords), 1.0)

        # --- Geographic bonus ---
        if geo_terms:
            full_text = title + " " + abstract
            geo_hits = sum(1 for geo in geo_terms if geo in full_text)
            score += 0.15 * min(geo_hits / len(geo_terms), 1.0)

        # --- Source type ---
        type_weights = {"peer-reviewed": 0.10, "preprint": 0.05, "general": 0.02}
        score += type_weights.get(source.source_type, 0.0)

        # --- Citations (capped — avoid citations drowning relevance) ---
        score += 0.05 * min(source.citations / max(max_citations, 1), 1.0)

        # --- Recency ---
        if source.year and source.year >= current_year - 5:
            score += 0.05

        source.relevance_score = round(min(score, 1.0), 3)

    return sorted(sources, key=lambda s: s.relevance_score, reverse=True)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _dedup_key(source: Source) -> tuple[str, str]:
    """
    Returns (url_key, title_key) for deduplication.
    url_key:   normalized URL with query params stripped (catches same DOI from different DBs)
    title_key: accent-normalized, punctuation-stripped, first 60 chars (catches minor title variants)
    """
    # URL key: strip query string and trailing slash
    url = re.sub(r"\?.*$", "", (source.url or "").lower().rstrip("/"))

    # Title key: normalize accents, strip punctuation, collapse spaces, first 60 chars
    title = _normalize(source.title or "")
    title = re.sub(r"[^\w\s]", "", title)
    title = re.sub(r"\s+", " ", title).strip()[:60]

    return url, title


def deduplicate(sources: list[Source]) -> list[Source]:
    seen_urls:   set[str] = set()
    seen_titles: set[str] = set()
    unique: list[Source] = []
    n_dropped = 0

    for s in sources:
        url_key, title_key = _dedup_key(s)
        is_dup = (
            (url_key and url_key in seen_urls)
            or (title_key and title_key in seen_titles)
        )
        if not is_dup:
            if url_key:
                seen_urls.add(url_key)
            if title_key:
                seen_titles.add(title_key)
            unique.append(s)
        else:
            n_dropped += 1

    if n_dropped:
        print(f"[research] deduplicated: removed {n_dropped} duplicate(s)")
    return unique


# ---------------------------------------------------------------------------
# NotebookLM integration
# ---------------------------------------------------------------------------

async def synthesize_with_notebooklm(
    topic: str,
    sources: list[Source],
    max_notebook_sources: int = 20,
) -> tuple[str, str]:
    """
    Create a NotebookLM notebook, add top sources, query for a synthesis.
    Returns (notebook_id, synthesis_text).
    Falls back gracefully if notebooklm-py is unavailable.
    """
    try:
        from notebooklm import NotebookLMClient
    except ImportError:
        print("[notebooklm] notebooklm-py not installed — skipping synthesis", file=sys.stderr)
        return "", _fallback_synthesis(topic, sources)

    # Only load top N sources into NotebookLM (API has source limits)
    top_sources = [s for s in sources if s.url][:max_notebook_sources]
    timestamp = int(time.time())
    notebook_title = f"research-skill-{topic[:40]}-{timestamp}"

    try:
        async with await NotebookLMClient.from_storage() as client:
            # Create notebook
            nb = await client.notebooks.create(notebook_title)
            notebook_id = nb.id
            print(f"[notebooklm] created notebook: {notebook_title} ({notebook_id})")

            # Add sources (sequentially, with per-source timeout)
            loaded = 0
            failed: list[str] = []
            MIN_SOURCES_TO_PROCEED = 5

            for source in top_sources:
                try:
                    await asyncio.wait_for(
                        client.sources.add_url(notebook_id, source.url, wait=False),
                        timeout=18,
                    )
                    loaded += 1
                    print(f"[notebooklm] added {loaded}/{len(top_sources)}: {source.title[:60]}")
                    await asyncio.sleep(1)
                except asyncio.TimeoutError:
                    failed.append(source.url)
                    print(f"[notebooklm] timeout (skipped): {source.url}", file=sys.stderr)
                except Exception as e:
                    failed.append(source.url)
                    print(f"[notebooklm] skipped ({type(e).__name__}): {source.url}", file=sys.stderr)

            if failed:
                print(f"[notebooklm] {len(failed)} source(s) failed to load")

            if loaded == 0:
                print("[notebooklm] no sources loaded — using fallback synthesis", file=sys.stderr)
                return notebook_id, _fallback_synthesis(topic, sources)

            if loaded < MIN_SOURCES_TO_PROCEED:
                print(f"[notebooklm] only {loaded} source(s) loaded (minimum is {MIN_SOURCES_TO_PROCEED}) — using fallback", file=sys.stderr)
                return notebook_id, _fallback_synthesis(topic, sources)

            # Brief wait then poll (don't wait for all sources — proceed once stable)
            print(f"[notebooklm] {loaded} sources loaded, waiting 5s before polling...")
            await asyncio.sleep(5)
            await _wait_for_sources_ready(client, notebook_id, timeout_seconds=300)

            # Query for synthesis
            synthesis_query = (
                f"You are a research assistant synthesizing academic literature on: {topic}. "
                "Provide a comprehensive synthesis of the key themes, findings, consensus, "
                "and disagreements across all the loaded sources. "
                "Structure your response as: 1) Main findings, 2) Key debates, 3) Research gaps."
            )
            result = await client.chat.ask(notebook_id, synthesis_query)
            synthesis = result.text if hasattr(result, "text") else str(result)
            print("[notebooklm] synthesis complete")

            # Also ask for per-source summaries to enrich source objects
            await _enrich_source_summaries(client, notebook_id, sources[:max_notebook_sources])

            return notebook_id, synthesis

    except Exception as e:
        print(f"[notebooklm] error during synthesis: {e}", file=sys.stderr)
        return "", _fallback_synthesis(topic, sources)


async def _wait_for_sources_ready(client, notebook_id: str, timeout_seconds: int = 600):
    """Poll until all sources are READY or timeout."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            sources = await client.sources.list(notebook_id)
            statuses = [s.status for s in sources]
            if all(str(s).upper() in ("READY", "FAILED") for s in statuses):
                ready = sum(1 for s in statuses if str(s).upper() == "READY")
                print(f"[notebooklm] {ready}/{len(statuses)} sources ready")
                return
        except Exception:
            pass
        await asyncio.sleep(15)
    print("[notebooklm] timeout waiting for sources — proceeding anyway", file=sys.stderr)


async def _enrich_source_summaries(client, notebook_id: str, sources: list[Source]):
    """Ask NotebookLM for a one-sentence summary of each source."""
    for source in sources:
        if not source.title:
            continue
        try:
            query = f"In one sentence, what is the main finding of the source titled: '{source.title}'?"
            result = await client.chat.ask(notebook_id, query)
            source.summary = result.text if hasattr(result, "text") else str(result)
        except Exception:
            source.summary = source.abstract[:300] if source.abstract else ""


def _fallback_synthesis(topic: str, sources: list[Source]) -> str:
    """Generate a basic synthesis from abstracts when NotebookLM is unavailable."""
    top = sources[:10]
    abstracts = "\n\n".join(
        f"- {s.title} ({s.year}): {s.abstract[:200]}" for s in top if s.abstract
    )
    return (
        f"[Fallback synthesis — NotebookLM unavailable]\n\n"
        f"Based on {len(sources)} sources found on '{topic}':\n\n"
        f"{abstracts}"
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_research(
    topic: str,
    max_sources: int = 50,
    min_year: int = 2015,
    peer_reviewed_only: bool = False,
    output_path: Optional[str] = None,
) -> dict:
    """
    Full research pipeline. Returns structured dict with sources and synthesis.
    """
    print(f"\n[research] Starting research on: '{topic}'")
    print(f"[research] max_sources={max_sources}, min_year={min_year}, peer_reviewed_only={peer_reviewed_only}\n")

    # 1. Decompose topic into targeted sub-queries (scale with max_sources)
    n_queries = max(4, min(12, max_sources // 25))
    sub_queries = await decompose_query(topic, n=n_queries)

    # 2. Search Semantic Scholar with each sub-query (sequential — 1 req/sec rate limit)
    all_sources: list[Source] = []
    per_query_limit = max(10, max_sources // len(sub_queries))

    for query in sub_queries:
        batch = await search_semantic_scholar(query, limit=per_query_limit, min_year=min_year)
        all_sources.extend(batch)
        await asyncio.sleep(1.1)  # respect 1 req/sec rate limit

    # 3. Search OpenAlex with the most geo-specific sub-query (scale limit with max_sources)
    openalex_limit = min(50, max(25, max_sources // 6))
    openalex_batch = await search_openalex(sub_queries[0], limit=openalex_limit, min_year=min_year)
    all_sources.extend(openalex_batch)

    # 4. arXiv with original topic (preprints, skip if peer_reviewed_only)
    if not peer_reviewed_only:
        arxiv_batch = await search_arxiv(topic, limit=max_sources // 3, min_year=min_year)
        all_sources.extend(arxiv_batch)

    # 2. Deduplicate and score
    all_sources = deduplicate(all_sources)
    all_sources = score_sources(all_sources, topic)
    all_sources = all_sources[:max_sources]

    print(f"\n[research] {len(all_sources)} unique sources after deduplication and scoring")

    # 3. Synthesize with NotebookLM
    notebook_id, synthesis = await synthesize_with_notebooklm(topic, all_sources)

    # 4. Build output
    output = {
        "topic": topic,
        "generated_at": datetime.now().isoformat(),
        "total_sources": len(all_sources),
        "notebook_id": notebook_id,
        "synthesis": synthesis,
        "sources": [s.to_dict() for s in all_sources],
    }

    if output_path:
        Path(output_path).write_text(json.dumps(output, indent=2, ensure_ascii=False))
        print(f"\n[research] Results saved to: {output_path}")

    print(f"\n[research] Done. {len(all_sources)} sources, synthesis length: {len(synthesis)} chars")
    return output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="notebooklm-research-skill")
    parser.add_argument("--topic", required=True, help="Research topic query")
    parser.add_argument("--max-sources", type=int, default=50)
    parser.add_argument("--min-year", type=int, default=2015)
    parser.add_argument("--peer-reviewed-only", action="store_true")
    parser.add_argument("--output", default=None, help="Output JSON file path")
    args = parser.parse_args()

    results = asyncio.run(run_research(
        topic=args.topic,
        max_sources=args.max_sources,
        min_year=args.min_year,
        peer_reviewed_only=args.peer_reviewed_only,
        output_path=args.output,
    ))

    if not args.output:
        print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
