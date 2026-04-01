# text-writing-skill

Writes scientific research reports in French Markdown. Pure text generation — no layout, no PDF. Receives research context, debate analysis, extracted data, and figure summaries. Returns clean Markdown ready for pdf-rendering-skill.

## What it does

1. Generates a concise French academic title (Claude Haiku, parallel)
2. Builds structured context from research sources, domain extractions, debate, and figure references
3. Calls Claude Sonnet to write the full report body in Markdown
4. Appends an APA reference list if not already present
5. Returns title, full markdown, word count, and keywords

## When to use this skill

- After `debate-generation-skill` (and optionally `domain-analysis-skill`) — feeds all upstream outputs into a coherent report
- Before `pdf-rendering-skill` — produces the Markdown this skill needs
- Standalone: to generate a French research text for any topic with available sources

## Inputs

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `topic` | str | required | Research topic (used for title, keywords, framing) |
| `research` | dict | None | Output of notebooklm-research-skill (contains `sources` list) |
| `extractions` | list[dict] | None | Output of domain-analysis-skill (metrics, locations, concepts by domain) |
| `debate` | dict | None | Output of debate-generation-skill (perspectives, contradictions, gaps) |
| `format` | str | `"academic"` | Report format: `"academic"`, `"article"`, or `"brief"` |
| `figures_summary` | dict | None | Figure metadata to reference inline (from data-scout-skill) |
| `output_path` | str | None | If set, saves the Markdown to this file path |

### Format specs

| Format | Target length | Audience | Structure |
|--------|--------------|----------|-----------|
| `academic` | 2500–3500 words | Academic researchers | Résumé, Introduction, Revue, Résultats, Discussion, Limites, Conclusion, Références |
| `article` | 900–1300 words | Educated public (Medium) | Mise en contexte, Ce que révèle la recherche, Le débat, Ce que ça change, Conclusion |
| `brief` | 350–500 words | Decision-makers / README | Synthèse, Résultats principaux (3 points), Implications, Sources clés |

## Outputs

```json
{
  "title": "Déforestation et fragmentation forestière au bassin du Congo (2000–2023)",
  "markdown": "## Résumé\n...\n## 1. Introduction\n...",
  "word_count": 2847,
  "keywords": ["déforestation", "bassin", "congo", "forêt"]
}
```

If `output_path` is set, the Markdown body is also saved to that file.

## How to call it

### From Python

```python
import sys
sys.path.insert(0, ".claude/skills/text-writing-skill")
from main import run_text_writing

result = await run_text_writing(
    topic="Déforestation dans le bassin du Congo",
    research=research,       # from notebooklm-research-skill
    extractions=extractions, # from domain-analysis-skill (optional)
    debate=debate,           # from debate-generation-skill
    format="academic",
    figures_summary={"chart": {"title": "Évolution forestière", "type": "line_chart"}},
    output_path="research-projects/congo/output/report.md",
)

print(result["title"])
print(f"{result['word_count']} words")
```

### From the command line

```bash
python .claude/skills/text-writing-skill/main.py \
  --topic "Déforestation dans le bassin du Congo" \
  --research research.json \
  --debate debate.json \
  --extracted extracted.json \
  --format academic \
  --output report.md
```

### Full pipeline chain

```python
research    = await run_research(topic="Déforestation bassin du Congo")
extractions = await run_domain_analysis(sources=research["sources"], topic="...")
debate      = await run_debate(sources=extractions, topic="...")
text        = await run_text_writing(topic="...", research=research,
                                     extractions=extractions, debate=debate,
                                     format="academic", output_path="report.md")
pdf         = run_pdf_rendering(title=text["title"], markdown=text["markdown"], ...)
```

## Setup requirements

```bash
pip install anthropic python-dotenv
```

Requires `ANTHROPIC_API_KEY` in `.env`.

## Notes

- Title and report body are generated in parallel (asyncio.gather) for speed.
- Sources are ranked by `relevance_score` and truncated to top 30 (400 chars each).
- Claude model: `claude-sonnet-4-6` for the report body, `claude-haiku-4-5-20251001` for the title.
- All text is written in French regardless of the input language.
- Figures are referenced inline as "(voir Figure 1)", "(comme le montre la Figure 2)", etc.
- Never invents statistics or authors — only uses what is provided in the context.
