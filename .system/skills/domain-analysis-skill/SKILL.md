# domain-analysis-skill

Replaces generic data extraction with domain-specific parallel analysis. Classifies sources by thematic domain, then runs specialized agents (environmental, social, economic, spatial, etc.) in parallel to extract domain-relevant metrics, locations, entities, and key findings.

## What it does

1. **Classify** — Claude Haiku tags each source with 1–3 domain labels (title + abstract only)
2. **Extract (parallel)** — Each active domain agent fetches and reads its assigned sources, then calls Claude Sonnet to extract domain-specific data
3. **Merge** — Combines records from all agents, deduplicates by source URL, merges multi-domain tags

Drop-in replacement for `data-extraction-skill.run_extraction()` — same output shape with an added `domain` field.

## When to use this skill

- In place of `data-extraction-skill` when your topic spans multiple thematic domains
- After `notebooklm-research-skill` — pass `research["sources"]` directly
- Before `debate-generation-skill` and `text-writing-skill` — they both accept this skill's output

## Domain agents

| Domain | Focus areas |
|--------|------------|
| `environmental` | Ecosystems, biodiversity, land cover, deforestation, climate effects |
| `social` | Population, demographics, migration, urbanization, inequality, housing |
| `economic` | GDP, trade, employment, investment, poverty, infrastructure |
| `spatial` | Land use, geographic distribution, spatial patterns, zonage, density |
| `political` | Governance, policy, conflict, institutions |
| `health` | Life expectancy, mortality, disease burden, health spending |

## Inputs

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `sources` | list[dict\|str] | required | Source dicts (from notebooklm-research-skill) or URL strings |
| `topic` | str | required | Research topic (used to focus classification and extraction) |
| `output_path` | str | None | If set, saves the JSON result to this path |
| `active_domains` | list[str] | `["environmental", "social", "economic", "spatial"]` | Which agents to run |
| `fallback_texts` | dict[str, str] | `{}` | URL → abstract fallback if fetching fails |

## Outputs

Returns a `list[dict]`, one record per source (deduplicated):

```json
[
  {
    "source": "https://doi.org/...",
    "source_type": "domain_extraction",
    "domain": "environmental",
    "domains": ["environmental", "spatial"],
    "metrics": [
      {"value": "2.3", "unit": "%/an", "context": "taux de déforestation annuel 2010–2020"}
    ],
    "locations": [
      {"name": "Lagos", "type": "city", "country": "Nigeria"}
    ],
    "dates": [
      {"date": "2010–2020", "type": "range", "context": "période d'étude"}
    ],
    "concepts": ["déforestation", "fragmentation forestière"],
    "entities": [
      {"name": "FAO", "type": "organization"}
    ],
    "key_findings": "La déforestation au Nigeria a progressé de 2.3%/an entre 2010 et 2020..."
  }
]
```

## How to call it

### From Python

```python
import sys
sys.path.insert(0, ".claude/skills/domain-analysis-skill")
from main import run_domain_analysis

extractions = await run_domain_analysis(
    sources=research["sources"][:20],
    topic="Urbanisation en Afrique subsaharienne",
    output_path="research-projects/africa-urban/extracted.json",
    active_domains=["environmental", "social", "economic", "spatial"],
)
```

### From the command line

```bash
# From a JSON file of source dicts
python .claude/skills/domain-analysis-skill/main.py \
  --sources research.json \
  --topic "Urbanisation en Afrique subsaharienne" \
  --domains environmental social economic spatial \
  --output extracted.json

# From direct URLs
python .claude/skills/domain-analysis-skill/main.py \
  --sources https://doi.org/... https://example.com/paper \
  --topic "Deforestation Amazon"
```

### Chained from notebooklm-research-skill

```python
research    = await run_research(topic="Urbanisation en Afrique subsaharienne")
extractions = await run_domain_analysis(
    sources=research["sources"],
    topic="Urbanisation en Afrique subsaharienne",
    fallback_texts={s["url"]: s.get("summary", "") for s in research["sources"] if s.get("url")},
)
debate = await run_debate(sources=extractions, topic="Urbanisation en Afrique subsaharienne")
```

## Setup requirements

```bash
pip install anthropic httpx python-dotenv
# Optional for better web extraction:
pip install trafilatura
# Optional for PDF sources:
pip install pdfplumber
```

Requires `ANTHROPIC_API_KEY` in `.env`.

## Notes

- Classification uses Claude Haiku (cheap, fast) — extraction uses Claude Sonnet (higher quality).
- Each domain agent reads up to 8 sources (`MAX_DOMAIN_SOURCES`) to keep prompt size manageable.
- Sources that can't be fetched fall back to their `abstract` or `summary` field automatically.
- A source can appear in multiple domain records if classified under multiple domains — these are merged by URL in the final output.
- The `fallback_texts` dict is especially useful when source URLs are paywalled or rate-limited.
