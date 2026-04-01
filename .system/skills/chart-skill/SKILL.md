# chart-skill

Generates professional, publication-quality data charts for geographic research. Automatically fetches real open data (World Bank, NASA POWER) for any topic, then uses Claude to select the best chart type and render it.

## What it does

1. **Analyze topic** — Claude Haiku extracts geographic context: countries, ISO codes, relevant indicators, lat/lon, year range
2. **Fetch data** — World Bank API (time series by indicator + country) and NASA POWER (climate: temperature + precipitation)
3. **Plan chart** — Claude Sonnet selects the most scientifically appropriate chart type and builds a render plan with French labels
4. **Render chart** — Plotly (primary), seaborn (statistical charts), matplotlib (fallback)

## When to use this skill

- Standalone: to generate a chart for any geographic topic with open data
- From `data-scout-skill` — called internally; prefer data-scout-skill in the full pipeline as it scores and selects the best data source
- Before `text-writing-skill` if you need a `figures_summary` to reference in the report

## Supported chart types

| Type | When used |
|------|-----------|
| `line_chart` | Time series (annual data over multiple years) |
| `bar_chart` | Category comparison (single series) |
| `grouped_bar` | Multi-country or multi-indicator comparison |
| `scatter` | Correlation between two variables |
| `histogram` | Distribution of a single variable |
| `box_plot` | Spread and outliers by group |
| `heatmap_matrix` | Correlation matrix across indicators |

## Data sources

| Source | Data type | Coverage |
|--------|-----------|---------|
| World Bank API | ~20 indicators (urban %, forest cover, GDP, CO2, etc.) | ~200 countries, 1960–present |
| NASA POWER | Temperature (°C), precipitation (mm) | Any lat/lon, annual, 1981–present |

Available World Bank indicators: urban population %, rural %, agricultural land %, forest cover %, protected areas %, CO2 per capita, energy use, GHG emissions, total population, GDP, GDP per capita, Gini index, life expectancy, health spending, infant mortality.

## Inputs

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `data` | any | `[]` | Upstream data (optional — chart-skill fetches its own data from APIs) |
| `topic` | str | `""` | Research topic — used to identify country, indicators, and years |
| `output_dir` | str | `"./output"` | Directory to save chart PNG |
| `formats` | list[str] | `["png"]` | Only `"png"` supported |

## Outputs

```json
{
  "chart": "output/urban-heat-islands-montreal-chart.png",
  "title": "Évolution de la population urbaine au Canada (1990–2024)",
  "type": "line_chart"
}
```

Returns `{}` if no usable data is found or the chart plan is skipped.

## How to call it

### From Python

```python
import sys
sys.path.insert(0, ".claude/skills/chart-skill")
from main import run_chart

result = await run_chart(
    data=[],
    topic="Urbanisation en Afrique subsaharienne",
    output_dir="research-projects/africa/figures",
)

if result.get("chart"):
    print(f"Chart saved: {result['chart']}")
    print(f"Title: {result['title']}")
```

### From the command line

```bash
python .claude/skills/chart-skill/main.py \
  --topic "Urbanisation en Afrique subsaharienne" \
  --output-dir figures/
```

## Setup requirements

```bash
pip install anthropic httpx plotly kaleido matplotlib seaborn pandas python-dotenv
```

Requires `ANTHROPIC_API_KEY` in `.env`. No other API keys needed (World Bank and NASA POWER are free and open).

## Notes

- Claude Haiku is used for topic analysis (fast), Claude Sonnet for chart planning (higher quality).
- Charts are rendered at 300 DPI, 960×460 px (Plotly) — publication ready.
- Color palette matches `map-skill` and `pdf-rendering-skill` for visual consistency across the report.
- If multiple indicators are selected, only the first is used for the chart y-axis (to avoid mixing incompatible units).
- The skill gracefully returns `{}` if data is too sparse — never crashes the pipeline.
- `data-scout-skill` uses this skill internally but adds GBIF and FAOSTAT sources and scores all datasets before choosing the best one for the chart.
