# notebooklm-research-skill

Deep academic research using Semantic Scholar, arXiv, and Google NotebookLM.

## What it does

1. Uses Claude to decompose the topic into 4 targeted sub-queries (French + English + geographic variants)
2. Searches **Semantic Scholar**, **OpenAlex**, and **arXiv** with each sub-query
3. Scores and ranks all sources by relevance to the original topic (keyword + geographic match dominant)
4. Loads the top sources into a NotebookLM notebook
5. Uses NotebookLM's AI to synthesize and summarize findings
6. Returns structured JSON with title, URL, summary, relevance score, and metadata

## When to use this skill

Call this skill whenever a system needs a literature base. It is the entry point for:
- `research-synthesis-system` (feeds the synthesis pipeline)
- `debate-generation-skill` (provides the sources to debate)
- Any system that needs to know "what does the literature say about X?"

## Inputs

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `topic` | str | required | Research query (e.g., "urban heat islands Montreal") |
| `max_sources` | int | 50 | Max number of sources to return |
| `min_year` | int | 2015 | Earliest publication year to include |
| `peer_reviewed_only` | bool | False | If True, exclude preprints and general web |
| `output_path` | str | None | If set, writes JSON to this file path |

## Outputs

Returns a dict (and optionally writes to JSON):

```json
{
  "topic": "urban heat islands Montreal",
  "total_sources": 47,
  "notebook_id": "abc123",
  "sources": [
    {
      "title": "Urban heat island effects in Canadian cities",
      "url": "https://doi.org/...",
      "summary": "...",
      "relevance_score": 0.91,
      "source_type": "peer-reviewed",
      "year": 2022,
      "citations": 204,
      "authors": ["Smith J.", "Tremblay M."]
    }
  ],
  "synthesis": "Across 47 sources, the literature consistently shows..."
}
```

`relevance_score` is 0–1. Keyword relevance to the original topic is the dominant factor.

| Criterion | Weight | Notes |
|---|---|---|
| Keyword match in **title** | +0.40 | Stop words filtered; accent-insensitive |
| Keyword match in **abstract** | +0.25 | Same keyword set |
| **Geographic** match | +0.15 | Place names from topic (Montréal, Québec, Canada…) |
| Source type | +0.10 | peer-reviewed > preprint > general |
| Citations (normalized) | +0.05 | Quality signal, not dominant |
| Recency (last 5 years) | +0.05 | |

A source mentioning Montréal in its title will score ~2.5× higher than a generic source on the same theme.

## How to call it

### From Python (another system or skill)

```python
import sys
sys.path.insert(0, ".claude/skills/notebooklm-research-skill")
from main import run_research

results = await run_research(
    topic="urban heat islands Montreal",
    max_sources=75,
    min_year=2015,
)
```

### From the command line

```bash
python .claude/skills/notebooklm-research-skill/main.py \
  --topic "urban heat islands Montreal" \
  --max-sources 75 \
  --output results.json
```

### From Claude Code

> "Run notebooklm research on [topic]"

Claude Code will call this skill and return the structured JSON.

## Setup requirements

```bash
pip install notebooklm-py httpx python-dotenv
notebooklm login   # one-time Google auth
```

Optional: add `SEMANTIC_SCHOLAR_API_KEY` to `.env` for higher rate limits (default: 1 req/sec without key).
No API key needed for OpenAlex or arXiv (free public APIs).
Requires `ANTHROPIC_API_KEY` for query decomposition (Claude Haiku).

## Notes

- NotebookLM source processing takes 30s–10min per source. The skill waits automatically.
- Sources that time out (>18s) or fail are skipped silently — at least 5 must load to proceed, otherwise falls back to abstract-based synthesis.
- NotebookLM notebooks created by this skill are named `research-skill-<topic>-<timestamp>` and are NOT deleted after use (you can open them in the NotebookLM UI).
- `notebooklm-py` uses undocumented Google APIs — if it breaks, the skill falls back to returning sources without the NotebookLM synthesis step.
- Sub-queries are generated in both French and English when the topic contains French terms or mentions Quebec/Montreal.
