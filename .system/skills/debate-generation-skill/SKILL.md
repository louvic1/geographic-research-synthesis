# debate-generation-skill

Takes findings from multiple sources and generates a structured academic debate — perspectives, contradictions, gaps, and assumptions.

## What it does

1. Accepts a list of sources (text, extracted records from data-extraction-skill, or raw findings)
2. Uses Claude to identify distinct perspectives and schools of thought across the sources
3. Constructs a structured debate: each side argues its position with evidence
4. Identifies contradictions, research gaps, and hidden assumptions
5. Returns a debate summary JSON suitable for feeding into report-writing-skill

## When to use this skill

Call this skill after gathering and extracting sources, before writing a report:
- `research-synthesis-system` — adds critical depth before writing
- `report-writing-skill` — consumes this skill's output for the "Debates & Tensions" section
- Standalone: to stress-test any set of findings before publishing

## Inputs

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `sources` | list[dict\|str] | required | Source texts, or records from data-extraction-skill |
| `topic` | str | required | The research topic / central question |
| `n_perspectives` | int | 3 | Number of distinct perspectives to generate (2–5) |
| `output_path` | str | None | If set, writes JSON to this file |

`sources` can be:
- Raw strings (abstracts, passages)
- Dicts with a `"summary"` or `"abstract"` key (output of notebooklm-research-skill)
- Dicts with extracted fields (output of data-extraction-skill)

## Outputs

```json
{
  "topic": "urban heat islands in Montreal",
  "perspectives": [
    {
      "label": "Green Infrastructure Advocates",
      "claim": "Expanding urban vegetation is the most cost-effective mitigation strategy.",
      "evidence": ["Study A found a 1.8°C reduction...", "Model B showed..."],
      "sources": ["https://doi.org/..."]
    },
    {
      "label": "Urban Planning Skeptics",
      "claim": "Land-use regulations are the root cause and must be addressed first.",
      "evidence": ["..."],
      "sources": ["..."]
    }
  ],
  "contradictions": [
    {
      "claim_a": "Green roofs reduce UHI by 2°C",
      "claim_b": "Green roofs have negligible effect at city scale",
      "source_a": "...",
      "source_b": "...",
      "note": "Studies differ in spatial scale (building vs. city)"
    }
  ],
  "gaps": [
    "No longitudinal studies beyond 10 years",
    "Limited data on low-income neighbourhood adaptation capacity"
  ],
  "assumptions": [
    "Most models assume homogeneous urban morphology",
    "Cost-benefit analyses rarely include social equity metrics"
  ],
  "synthesis": "The debate ultimately centres on scale: interventions effective at the building level do not always aggregate to city-scale impact..."
}
```

## How to call it

### From Python

```python
import sys
sys.path.insert(0, ".claude/skills/debate-generation-skill")
from main import run_debate

# From notebooklm-research-skill output
debate = await run_debate(
    sources=research_results["sources"],
    topic="urban heat islands Montreal",
    n_perspectives=3,
)

# From raw text
debate = await run_debate(
    sources=["Finding A: ...", "Finding B: ..."],
    topic="urban heat islands Montreal",
)
```

### From the command line

```bash
python .claude/skills/debate-generation-skill/main.py \
  --topic "urban heat islands Montreal" \
  --sources-file extracted.json \
  --n-perspectives 3 \
  --output debate.json
```

### Chained from previous skills

```python
research = await run_research(topic="urban heat islands")
records  = await run_extraction(sources=[s["url"] for s in research["sources"][:20]])
debate   = await run_debate(sources=records, topic="urban heat islands Montreal")
```

## Setup requirements

```bash
pip install anthropic python-dotenv
```

Requires `ANTHROPIC_API_KEY` in `.env`.

## Notes

- Uses `claude-sonnet-4-6` (not Haiku) — debate requires nuanced reasoning.
- Sources are summarized to fit context if there are more than 20 (truncated to top 20 by relevance_score if available).
- The skill does NOT take a political stance — it maps the intellectual landscape of the literature.
