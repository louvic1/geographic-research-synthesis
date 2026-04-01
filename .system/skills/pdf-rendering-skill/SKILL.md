# pdf-rendering-skill

Pure layout engine — no Claude calls, no API keys required. Takes pre-written Markdown text and figure paths, assembles a professional academic PDF with title page, table of contents, inline figures, and styled references section.

## What it does

1. Converts Markdown to HTML (via `python-markdown`)
2. Adds heading IDs and builds a nested table of contents
3. Injects figure images inline at their target section (as base64 data URIs)
4. Assembles a complete HTML document with title page, institution, date, keywords
5. Renders to PDF via WeasyPrint (falls back to saving HTML if WeasyPrint fails)
6. Applies `style.css` from the skill directory for clean typography and layout

## When to use this skill

- As the last step in the pipeline — receives output from `text-writing-skill`
- Any time you need to convert a Markdown report to a professional PDF
- Standalone: to render any `.md` file with optional figures

## Inputs

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `title` | str | required | Report title (appears on title page) |
| `markdown` | str | required | Full Markdown body text (from text-writing-skill) |
| `figures` | dict | `{}` | Figure paths: `{"chart": "path/to/chart.png", "map": {"png": "..."}}` |
| `output_path` | str | `"report.pdf"` | Path to save the PDF |
| `keywords` | list[str] | `[]` | Keywords for title page |
| `format` | str | `"academic"` | `"academic"`, `"article"`, or `"brief"` (changes subtitle) |
| `institution` | str | `"Université de Montréal"` | Institution name on title page |

### Figures dict format

```python
figures = {
    "chart": "output/figures/topic-chart.png",          # PNG path string
    "map":   {"png": "output/figures/topic-map.png",    # dict with png key
              "html": "output/figures/topic-map.html"},
}
```

Figure placement is automatic:
- `chart` is injected after the first `</p>` of the "Résultats" section
- `map` is injected after the first `</p>` of the "Revue de littérature" section
- Unfitted figures are appended in an "Annexe — Figures supplémentaires" section

## Outputs

```json
{
  "pdf_path": "research-projects/topic/output/report.pdf",
  "html_path": "research-projects/topic/output/report.html"
}
```

The HTML file is always saved (useful for debugging or as a fallback). If WeasyPrint fails, `pdf_path` returns the HTML path instead.

## How to call it

### From Python

```python
import sys
sys.path.insert(0, ".claude/skills/pdf-rendering-skill")
from main import run_pdf_rendering

result = run_pdf_rendering(
    title="Déforestation et fragmentation forestière au bassin du Congo",
    markdown=report_markdown,
    figures={
        "chart": "output/figures/congo-chart.png",
        "map":   {"png": "output/figures/congo-map.png"},
    },
    output_path="research-projects/congo/output/report.pdf",
    keywords=["déforestation", "bassin", "congo"],
    format="academic",
    institution="Université de Montréal",
)

print(result["pdf_path"])
```

Note: `run_pdf_rendering` is synchronous (no `await` needed).

### From the command line

```bash
python .claude/skills/pdf-rendering-skill/main.py \
  --title "Déforestation dans le bassin du Congo" \
  --markdown report.md \
  --figures figures.json \
  --output report.pdf \
  --keywords "déforestation, bassin, congo" \
  --format academic
```

`figures.json` format:
```json
{"chart": "figures/chart.png", "map": {"png": "figures/map.png"}}
```

## Setup requirements

```bash
pip install markdown weasyprint python-dotenv
```

No `ANTHROPIC_API_KEY` required — entirely local rendering.

WeasyPrint may need system dependencies on macOS:
```bash
brew install pango
```

## Notes

- This skill is **synchronous** — call it with `run_pdf_rendering(...)`, not `await run_pdf_rendering(...)`.
- Figures are embedded as base64 data URIs so the PDF is fully self-contained.
- Only `.png` files are supported for embedding (`.html` maps are not embedded in PDF).
- The HTML file is always saved alongside the PDF — useful for debugging layout issues.
- `style.css` in the skill directory controls all typography, spacing, and colors. Edit it to customize the look.
- Table of contents is generated automatically from all `##` and `###` headings.
- The references section (`## Références`) is automatically wrapped in a styled div.
