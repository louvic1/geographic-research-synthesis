"""
domain-analysis-skill
Replaces generic data-extraction in the pipeline with domain-specific analysis.

Workflow:
  1. classify_sources()     — Claude Haiku tags each source with 1-3 domain labels
  2. [parallel] domain agents extract/analyze their sources via asyncio.gather:
       - EnvironmentAgent  → ecosystems, land cover, biodiversity, climate effects
       - SocialAgent       → population, demographics, urbanization, inequality
       - EconomicAgent     → GDP, trade, investment, poverty, development
       - SpatialAgent      → land use, geographic distribution, spatial patterns
  3. merge_domain_results() — combines records, deduplicates by URL

Drop-in replacement for data-extraction-skill.run_extraction() — same output shape,
with an added "domain" field on each record.

Usage:
    python main.py --sources source1.pdf https://example.com \\
                   --topic "Urbanisation en Afrique" \\
                   --output extracted.json
"""

import argparse
import asyncio
import json
import re
import sys
from io import StringIO
from pathlib import Path
from typing import Optional

import anthropic
import httpx
from dotenv import load_dotenv

load_dotenv()

CLAUDE_FAST  = "claude-haiku-4-5-20251001"
CLAUDE_SMART = "claude-sonnet-4-6"

MAX_CHUNK_CHARS   = 30_000
MAX_FETCH_RETRIES = 3
MAX_DOMAIN_SOURCES = 8  # max sources per agent call (quality > quantity)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-CA,fr;q=0.9,en-CA;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
}

VALID_DOMAINS = ["environmental", "social", "economic", "spatial", "political", "health"]


# ---------------------------------------------------------------------------
# Domain agent definitions
# ---------------------------------------------------------------------------

DOMAIN_AGENTS: dict[str, dict] = {
    "environmental": {
        "label":    "Agent Environnemental",
        "focus":    "écosystèmes, biodiversité, couverture terrestre, déforestation, dégradation des sols, qualité de l'eau, effets climatiques sur la nature",
        "extract":  [
            "Métriques : taux de déforestation, superficie forestière, biodiversité, émissions, températures, précipitations, qualité de l'eau",
            "Entités   : espèces, écosystèmes, zones protégées, réserves naturelles",
            "Tendances : dégradation environnementale, restauration, changement climatique",
        ],
    },
    "social": {
        "label":    "Agent Social",
        "focus":    "population, démographie, migrations, urbanisation, inégalités, logement, conditions de vie, bidonvilles, accès aux services",
        "extract":  [
            "Métriques : taux d'urbanisation, croissance démographique, flux migratoires, Gini, accès à l'eau/électricité/santé",
            "Entités   : groupes sociaux, ONG, communautés, organisations",
            "Tendances : exclusion sociale, gentrification, étalement urbain, pauvreté",
        ],
    },
    "economic": {
        "label":    "Agent Économique",
        "focus":    "PIB, commerce, emploi, investissement, pauvreté, développement économique, industrialisation, infrastructure",
        "extract":  [
            "Métriques : PIB, taux de croissance, chômage, investissement étranger, pauvreté, revenu par habitant",
            "Entités   : entreprises, institutions financières, gouvernements, banques de développement",
            "Tendances : industrialisation, développement, inégalités économiques",
        ],
    },
    "spatial": {
        "label":    "Agent Spatial",
        "focus":    "utilisation des terres, distribution géographique, patterns spatiaux, cartographie, zonage, densité, fragmentation territoriale",
        "extract":  [
            "Métriques : superficie, densité de population, % changement d'utilisation des terres, distance, connectivité",
            "Lieux     : villes, régions, coordonnées, zones d'étude précises",
            "Tendances : étalement urbain, fragmentation, densification, ségrégation spatiale",
        ],
    },
}


CLASSIFY_PROMPT = """Tu es un expert en classification de sources de recherche géographique.

Sujet de recherche global: {topic}

Pour chacune des sources suivantes, identifie les domaines thématiques principaux.
Domaines disponibles: environmental, social, economic, spatial, political, health

Règles:
- Assigne 1 à 3 domaines par source (les plus pertinents seulement)
- Base-toi sur le titre et le résumé pour classer
- Une source peut appartenir à plusieurs domaines

Sources:
{sources_block}

Retourne UNIQUEMENT du JSON valide:
{{
  "classifications": [
    {{"index": 0, "domains": ["environmental", "spatial"]}},
    {{"index": 1, "domains": ["social", "economic"]}},
    ...
  ]
}}"""


DOMAIN_EXTRACTION_PROMPT = """Tu es un expert en analyse de données géographiques. Tu es l'{agent_label}.

Ton rôle: extraire les informations sur **{focus}** depuis les sources ci-dessous.

Contexte de recherche: {topic}

Pour chaque source, extrait:
{extract_list}

Aussi:
- Toutes les métriques avec valeurs, unités et contexte
- Lieux géographiques mentionnés (villes, régions, pays, coordonnées)
- Dates et périodes d'étude
- Concepts clés relatifs à ton domaine
- Entités importantes (organisations, institutions, méthodes)

Sources à analyser:
{sources_block}

Retourne UNIQUEMENT du JSON valide:
{{
  "records": [
    {{
      "source_index": 0,
      "domain": "{domain}",
      "metrics": [
        {{"value": "2.3", "unit": "%/an", "context": "taux de déforestation annuel 2010-2020"}}
      ],
      "locations": [
        {{"name": "Lagos", "type": "city", "country": "Nigeria"}}
      ],
      "dates": [
        {{"date": "2010-2020", "type": "range", "context": "période d'étude"}}
      ],
      "concepts": ["déforestation", "fragmentation forestière"],
      "entities": [
        {{"name": "FAO", "type": "organization"}}
      ],
      "key_findings": "Résumé en 2-3 phrases des trouvailles principales de cette source pour le domaine {domain}"
    }}
  ]
}}"""


RETRY_SUFFIX = (
    "\n\nIMPORTANT: JSON invalide. Retourne UNIQUEMENT un objet JSON valide et complet. "
    "Si le contenu est long, réduis le nombre d'éléments mais ne coupe jamais le JSON."
)


# ---------------------------------------------------------------------------
# Source fetching (same as data-extraction-skill)
# ---------------------------------------------------------------------------

def _is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def _is_pdf_path(s: str) -> bool:
    return Path(s).suffix.lower() == ".pdf" and Path(s).exists()


def _extract_text_from_pdf(path: str) -> str:
    try:
        import pdfplumber
    except ImportError:
        raise ImportError("pdfplumber required: pip install pdfplumber")
    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


async def _fetch_url_text(url: str) -> str:
    last_error: Exception = RuntimeError("no attempts made")
    for attempt in range(MAX_FETCH_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
                resp = await client.get(url, headers=BROWSER_HEADERS)
                resp.raise_for_status()
                if "pdf" in resp.headers.get("content-type", ""):
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                        f.write(resp.content)
                        return _extract_text_from_pdf(f.name)
                html = resp.text
                try:
                    import trafilatura
                    extracted = trafilatura.extract(html, include_comments=False, include_tables=True)
                    if extracted and len(extracted) > 200:
                        return extracted
                except ImportError:
                    pass
                text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL)
                text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s{3,}", "\n\n", text)
                return text.strip()
        except httpx.HTTPStatusError as e:
            last_error = e
            if e.response.status_code in (403, 429, 503) and attempt < MAX_FETCH_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = e
            if attempt < MAX_FETCH_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise
    raise last_error


async def load_source_text(
    source: str, fallback_text: Optional[str] = None
) -> tuple[str, str]:
    """Return (source_type, text) for a source identifier."""
    if _is_pdf_path(source):
        return "pdf", _extract_text_from_pdf(source)
    elif _is_url(source):
        try:
            text = await _fetch_url_text(source)
            return "url", text
        except Exception as e:
            if fallback_text and len(fallback_text.strip()) > 100:
                print(f"  [fetch] failed ({type(e).__name__}), using abstract fallback")
                return "abstract_fallback", fallback_text
            raise
    else:
        return "text", source


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 > max_chars:
            if current:
                chunks.append(current.strip())
            current = para
        else:
            current = current + "\n\n" + para if current else para
    if current:
        chunks.append(current.strip())
    return chunks


# ---------------------------------------------------------------------------
# Step 1 — Classify sources by domain
# ---------------------------------------------------------------------------

async def classify_sources(
    sources: list[dict],
    topic: str,
    client: anthropic.AsyncAnthropic,
) -> dict[str, list[dict]]:
    """
    Tag each source with domain labels using Claude Haiku.
    Returns {domain: [source_dict, ...]}
    """
    # Build sources block (title + abstract only — no need to fetch full text)
    source_lines: list[str] = []
    for i, s in enumerate(sources):
        title    = s.get("title", "No title")
        abstract = (s.get("abstract") or s.get("summary") or "")[:300]
        url      = s.get("url", "")
        source_lines.append(f"[{i}] {title}\n    URL: {url}\n    {abstract}")
    sources_block = "\n\n".join(source_lines)

    prompt = CLASSIFY_PROMPT.format(topic=topic, sources_block=sources_block)

    current_prompt = prompt
    for attempt in range(3):
        try:
            msg = await client.messages.create(
                model=CLAUDE_FAST,
                max_tokens=1500,
                messages=[{"role": "user", "content": current_prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            result = json.loads(raw)
            break
        except json.JSONDecodeError as e:
            if attempt < 2:
                current_prompt = prompt + RETRY_SUFFIX
            else:
                print(f"[domain:classify] JSON failed: {e} — assigning all to 'social'")
                result = {"classifications": [{"index": i, "domains": ["social"]} for i in range(len(sources))]}
                break
        except Exception as e:
            print(f"[domain:classify] Claude error: {e}")
            result = {"classifications": [{"index": i, "domains": ["social"]} for i in range(len(sources))]}
            break

    # Build domain → sources mapping
    domain_map: dict[str, list[dict]] = {d: [] for d in VALID_DOMAINS}
    for clf in result.get("classifications", []):
        idx = clf.get("index", -1)
        if 0 <= idx < len(sources):
            for domain in clf.get("domains", []):
                if domain in domain_map:
                    domain_map[domain].append({**sources[idx], "_source_index": idx})

    counts = {d: len(v) for d, v in domain_map.items() if v}
    print(f"[domain:classify] {counts}")
    return domain_map


# ---------------------------------------------------------------------------
# Step 2 — Domain agent extraction
# ---------------------------------------------------------------------------

async def _run_domain_agent(
    domain: str,
    sources: list[dict],
    topic: str,
    client: anthropic.AsyncAnthropic,
    fallback_texts: dict[str, str],
) -> list[dict]:
    """Run the specialized domain agent on its assigned sources."""
    if not sources:
        return []

    agent = DOMAIN_AGENTS[domain]
    print(f"\n[domain:{domain}] {len(sources)} source(s) — {agent['label']}")

    # Fetch full text for each source (up to MAX_DOMAIN_SOURCES)
    top_sources = sources[:MAX_DOMAIN_SOURCES]
    source_texts: list[tuple[dict, str, str]] = []

    for s in top_sources:
        url = s.get("url", "")
        fallback = fallback_texts.get(url) if url else None
        identifier = url or s.get("title", "")
        if not identifier:
            continue
        try:
            source_type, text = await load_source_text(identifier, fallback_text=fallback)
            # Take first 8000 chars to keep prompt manageable
            source_texts.append((s, source_type, text[:8000]))
            print(f"  [{domain}] loaded {len(text)} chars ({source_type}) — {identifier[:60]}")
        except Exception as e:
            print(f"  [{domain}] failed to load {identifier[:60]}: {e}")
            # Use abstract as fallback
            abstract = s.get("abstract") or s.get("summary") or ""
            if abstract:
                source_texts.append((s, "abstract", abstract))

    if not source_texts:
        return []

    # Build sources block for agent prompt
    block_parts: list[str] = []
    for i, (src, stype, text) in enumerate(source_texts):
        title   = src.get("title", "Sans titre")
        authors = ", ".join((src.get("authors") or [])[:2])
        year    = src.get("year", "")
        header  = f"--- SOURCE {i} : {title}"
        if authors:
            header += f" — {authors}"
        if year:
            header += f" ({year})"
        header += f" [{stype}] ---"
        block_parts.append(header + "\n" + text)
    sources_block = "\n\n".join(block_parts)

    extract_list = "\n".join(f"  - {e}" for e in agent["extract"])
    prompt = DOMAIN_EXTRACTION_PROMPT.format(
        agent_label=agent["label"],
        focus=agent["focus"],
        topic=topic,
        extract_list=extract_list,
        sources_block=sources_block,
        domain=domain,
    )

    # Call Claude with retry
    current_prompt = prompt
    records: list[dict] = []
    for attempt in range(3):
        try:
            msg = await client.messages.create(
                model=CLAUDE_SMART,
                max_tokens=8000,
                messages=[{"role": "user", "content": current_prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            result = json.loads(raw)
            records = result.get("records", [])
            break
        except json.JSONDecodeError as e:
            if attempt < 2:
                print(f"  [{domain}] JSON error attempt {attempt+1}: {e}")
                current_prompt = prompt + RETRY_SUFFIX
            else:
                print(f"  [{domain}] JSON failed after 3 attempts")
                break
        except Exception as e:
            print(f"  [{domain}] Claude error: {e}")
            break

    # Map source_index back to actual source info + add domain tag
    output: list[dict] = []
    for record in records:
        src_idx = record.get("source_index", -1)
        if 0 <= src_idx < len(source_texts):
            src_dict, _, _ = source_texts[src_idx]
            output.append({
                "source":      src_dict.get("url") or src_dict.get("title", ""),
                "source_type": "domain_extraction",
                "domain":      domain,
                "metrics":     record.get("metrics", []),
                "locations":   record.get("locations", []),
                "dates":       record.get("dates", []),
                "concepts":    record.get("concepts", []),
                "entities":    record.get("entities", []),
                "key_findings": record.get("key_findings", ""),
            })

    print(f"  [{domain}] extracted {len(output)} record(s)")
    return output


# ---------------------------------------------------------------------------
# Step 3 — Merge and deduplicate
# ---------------------------------------------------------------------------

def merge_domain_results(domain_results: list[list[dict]]) -> list[dict]:
    """Merge records from all domain agents, deduplicating by source URL."""
    seen: dict[str, dict] = {}  # url → record (merge multi-domain data)
    all_records: list[dict] = []

    for records in domain_results:
        for record in records:
            url = record.get("source", "")
            if url and url in seen:
                existing = seen[url]
                # Merge domain tags
                existing_domains = existing.get("domains", [existing.get("domain", "")])
                new_domain = record.get("domain", "")
                if new_domain and new_domain not in existing_domains:
                    existing_domains.append(new_domain)
                existing["domains"] = existing_domains
                # Extend lists (deduplicate concepts/entities)
                for field in ("metrics", "locations", "dates"):
                    existing[field] = existing.get(field, []) + record.get(field, [])
                seen_concepts = {c.lower() for c in existing.get("concepts", [])}
                for c in record.get("concepts", []):
                    if c.lower() not in seen_concepts:
                        existing.setdefault("concepts", []).append(c)
                        seen_concepts.add(c.lower())
            else:
                new_record = {**record, "domains": [record.get("domain", "")]}
                if url:
                    seen[url] = new_record
                all_records.append(new_record)

    return list(seen.values()) if seen else all_records


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_domain_analysis(
    sources: list,
    topic: str,
    output_path: Optional[str] = None,
    active_domains: Optional[list[str]] = None,
    fallback_texts: Optional[dict[str, str]] = None,
) -> list[dict]:
    """
    Classify sources by domain and run specialized agents in parallel.
    Drop-in replacement for run_extraction() — returns list[dict].
    """
    if active_domains is None:
        active_domains = ["environmental", "social", "economic", "spatial"]
    if fallback_texts is None:
        fallback_texts = {}

    # Normalize sources to list of dicts
    normalized: list[dict] = []
    for s in sources:
        if isinstance(s, dict):
            normalized.append(s)
        elif isinstance(s, str):
            normalized.append({"url": s, "title": s})

    if not normalized:
        print("[domain] No sources to analyze")
        return []

    print(f"\n[domain] Analyzing {len(normalized)} sources | topic: '{topic}'")
    print(f"[domain] Active agents: {active_domains}")

    client = anthropic.AsyncAnthropic()

    # Step 1: Classify
    print("[domain] Classifying sources by domain...")
    domain_map = await classify_sources(normalized, topic, client)

    # Step 2: Run domain agents in parallel
    active_map = {d: domain_map.get(d, []) for d in active_domains}
    tasks = [
        _run_domain_agent(domain, srcs, topic, client, fallback_texts)
        for domain, srcs in active_map.items()
    ]
    results_per_domain = await asyncio.gather(*tasks)

    # Step 3: Merge
    all_records = merge_domain_results(list(results_per_domain))
    print(f"\n[domain] Total: {len(all_records)} record(s) from {len(active_domains)} agents")

    # Save
    if output_path:
        Path(output_path).write_text(
            json.dumps(all_records, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[domain] Saved → {output_path}")

    return all_records


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="domain-analysis-skill")
    parser.add_argument("--sources", nargs="+", required=True)
    parser.add_argument("--topic",   required=True)
    parser.add_argument("--domains", nargs="+", default=["environmental", "social", "economic", "spatial"],
                        choices=VALID_DOMAINS)
    parser.add_argument("--output",  default=None)
    args = parser.parse_args()

    # Sources can be URLs or a JSON file path with source dicts
    sources = []
    for s in args.sources:
        if Path(s).exists() and s.endswith(".json"):
            data = json.loads(Path(s).read_text())
            if isinstance(data, list):
                sources.extend(data)
            elif isinstance(data, dict) and "sources" in data:
                sources.extend(data["sources"])
        else:
            sources.append(s)

    records = asyncio.run(run_domain_analysis(
        sources=sources,
        topic=args.topic,
        output_path=args.output,
        active_domains=args.domains,
    ))

    if not args.output:
        print(json.dumps(records, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
