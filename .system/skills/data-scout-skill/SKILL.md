# data-scout-skill

Orchestrates open data discovery and visualization for a geographic research topic. Fetches data from multiple sources (GBIF, FAOSTAT, World Bank, NASA POWER), scores each dataset by richness, lets Claude plan the best chart + map combination, then renders both figures.

Used as Step 4 of the system-1 pipeline — replaces calling `chart-skill` and `map-skill` separately.

## What it does

1. **Analyze topic** — Claude Haiku extracts geographic context, countries, indicators, lat/lon, whether GBIF / FAOSTAT / World Bank / NASA apply
2. **Fetch all sources (parallel)** — Queries all relevant APIs simultaneously
3. **Score datasets** — Each dataset receives a richness score (0–10) based on n_points, temporal range, and geographic spread
4. **Plan visualizations** — Claude Sonnet reviews the scored datasets and decides the best chart type + map type
5. **Render chart** — Calls `render_chart` from `chart-skill` with the selected dataset
6. **Render map** — Calls `render_choropleth` from `map-skill` or renders point/heatmap from GBIF coordinates

## When to use this skill

- As Step 4 of the research synthesis pipeline (before `text-writing-skill`)
- Whenever you want the best chart + map for a topic without manually choosing a data source
- When GBIF biodiversity data or FAOSTAT forest data may be relevant (not available in `chart-skill` alone)

## Data sources

| Source | Data type | Best for |
|--------|-----------|----------|
| **GBIF** | Species occurrence lat/lon points | Biodiversity, species distribution, ecology |
| **FAOSTAT** | Forest area time series by country | Deforestation, land use, agricultural land |
| **World Bank** | ~20 socioeconomic / environmental indicators | Urban growth, GDP, CO2, population, health |
| **NASA POWER** | Annual temperature + precipitation | Climate topics, heat islands, precipitation trends |

## Inputs

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `topic` | str | required | Research topic — drives all data fetching and planning |
| `output_dir` | str | `"./output"` | Directory to save chart PNG and map HTML/PNG |
| `formats` | list[str] | `["png", "html"]` | `"png"`, `"html"`, or both (applies to map) |

## Outputs

```json
{
  "chart": "output/figures/jaguar-amazon-chart.png",
  "map": {
    "html": "output/figures/jaguar-amazon-map.html",
    "png":  "output/figures/jaguar-amazon-map.png",
    "title": "Distribution du jaguar dans le bassin amazonien",
    "type": "heatmap"
  },
  "viz_plan": {
    "chart": {"source": "faostat", "viz_type": "line_chart", "title": "..."},
    "map":   {"source": "gbif",    "viz_type": "heatmap",   "title": "..."}
  },
  "datasets": {"gbif": {...}, "faostat": {...}, "worldbank": [...], "nasa": null},
  "sources_queried": ["gbif", "faostat", "worldbank"]
}
```

Returns `{}` if no usable data is found. Chart and map keys may be absent if their respective renders fail.

## How to call it

### From Python

```python
import sys
sys.path.insert(0, ".claude/skills/data-scout-skill")
from main import run_data_scout

result = await run_data_scout(
    topic="Jaguar distribution in Amazon basin",
    output_dir="research-projects/amazon-jaguar/output/figures",
    formats=["png", "html"],
)

if result.get("chart"):
    print(f"Chart: {result['chart']}")
if result.get("map"):
    print(f"Map HTML: {result['map']['html']}")
```

### From the command line

```bash
python .claude/skills/data-scout-skill/main.py \
  --topic "Jaguar distribution in Amazon basin" \
  --output-dir figures/

python .claude/skills/data-scout-skill/main.py \
  --topic "Deforestation in Congo basin" \
  --output-dir figures/
```

### In the system-1 pipeline (main.py)

```python
scout_result = await run_data_scout(
    topic=topic,
    output_dir=str(fig_dir),
    formats=["png", "html"],
)
# scout_result["chart"] → path to chart PNG
# scout_result["map"]   → dict with html/png paths
```

## Setup requirements

```bash
pip install anthropic httpx plotly kaleido matplotlib seaborn pandas \
            geopandas contextily folium scipy numpy pyproj python-dotenv
```

Requires `ANTHROPIC_API_KEY` in `.env`. All data APIs (GBIF, FAOSTAT, World Bank, NASA) are free and open.

## Richness scoring

Each dataset is scored 0–10:
- **n_points** (0–3): more data points = higher score
- **temporal_range** (0–2.5): longer time span = higher score
- **geographic_spread** (0–2.5): more countries or regions = higher score
- **completeness** (2.0): fixed bonus for passing the fetch filter

Claude plans the chart and map using the top-scored datasets, matching data type to appropriate viz type (time series → line_chart, occurrence points → heatmap, multi-country → choropleth).

## Notes

- This skill imports `render_chart` and `PALETTE` directly from `chart-skill` and `render_choropleth` from `map-skill` via importlib — both sibling skills must exist in the same `.claude/skills/` directory.
- GBIF queries are limited to 500 occurrence points and require a study area bounding box identified from the topic.
- FAOSTAT covers ~70 countries with a hardcoded ISO3 → area code lookup; missing countries are skipped silently.
- The `viz_plan` field in the output is saved as `scout.json` in the figures directory for checkpoint/resume support.
- Claude Haiku is used for topic analysis; Claude Sonnet for visualization planning.
