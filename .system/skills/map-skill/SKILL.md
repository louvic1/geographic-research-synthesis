# map-skill

Generates professional, publication-quality geographic maps for research topics. Uses Claude to identify relevant geographic features for a study area, plans the best map type based on available data, and renders it as interactive HTML (Folium) and/or static PNG (contextily + matplotlib).

## What it does

1. **Identify features** — Claude Sonnet identifies geographic features relevant to the topic (cities, regions, protected areas, rivers, hotspots) with their bounding box and zoom level
2. **Fetch World Bank snapshot** (optional) — If the topic spans multiple countries, fetches a World Bank indicator for choropleth coloring
3. **Plan map type** — Claude Sonnet decides between `point_map`, `choropleth`, or `heatmap` based on available locations and data
4. **Render** — Folium HTML + contextily PNG (point_map / heatmap), or Plotly choropleth (HTML + PNG)

## When to use this skill

- Standalone: to generate a map for any geographic research topic
- From `data-scout-skill` — called internally via `render_choropleth`; prefer data-scout-skill in the full pipeline as it selects the best data source automatically
- Before `text-writing-skill` if you need a `figures_summary` with a map reference

## Supported map types

| Type | When used | Output |
|------|-----------|--------|
| `point_map` | Named locations from literature (cities, sites, regions) | Folium HTML + contextily PNG |
| `choropleth` | Multi-country comparison with World Bank indicator | Plotly HTML + PNG |
| `heatmap` | Dense point clusters (10+ locations) | Folium HeatMap HTML + PNG |

Feature types supported for point maps: `city`, `region`, `country`, `river`, `protected_area`, `indigenous_territory`, `hotspot`, `site` — each gets a distinct color and marker size.

## Inputs

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `data` | list[dict] | `[]` | Upstream extraction records (used to enrich location context) |
| `topic` | str | required | Research topic — Claude identifies geographic features from this |
| `output_dir` | str | `"./output"` | Directory to save map files |
| `formats` | list[str] | `["png", "html"]` | `"png"`, `"html"`, or both |

## Outputs

```json
{
  "map": {
    "html": "output/urban-heat-islands-montreal-map.html",
    "png":  "output/urban-heat-islands-montreal-map.png",
    "title": "Zone métropolitaine de Montréal — ilots de chaleur urbains",
    "type": "point_map"
  }
}
```

Returns `{}` if no usable geographic data is found.

## How to call it

### From Python

```python
import sys
sys.path.insert(0, ".claude/skills/map-skill")
from main import run_map

result = await run_map(
    data=extraction_records,   # from domain-analysis-skill (optional)
    topic="Urbanisation en Afrique subsaharienne",
    output_dir="research-projects/africa/figures",
    formats=["png", "html"],
)

if result.get("map"):
    print(f"HTML: {result['map']['html']}")
    print(f"PNG:  {result['map']['png']}")
```

### From the command line

```bash
python .claude/skills/map-skill/main.py \
  --topic "Urbanisation en Afrique subsaharienne" \
  --data extracted.json \
  --output-dir figures/ \
  --formats png html
```

## Setup requirements

```bash
pip install anthropic httpx folium plotly pandas geopandas contextily matplotlib python-dotenv
```

Requires `ANTHROPIC_API_KEY` in `.env`. World Bank API is free and open.

On macOS, contextily may require:
```bash
brew install proj gdal
```

## Notes

- Geographic features are identified by Claude from the topic string alone — no `data` input is required for the map to work.
- Location geocoding uses the free Nominatim API (OpenStreetMap), rate-limited to 1 req/sec.
- Bounding box and zoom level are set by Claude for focused, study-area-appropriate framing.
- PNG static maps use contextily basemap tiles (CartoDB Positron) via EPSG:3857 projection.
- If contextily fails (network or projection issues), falls back to a matplotlib scatter plot on a blank coordinate system.
- `data-scout-skill` calls `render_choropleth` from this skill directly when choropleth is the best map type for GBIF or World Bank data.
- Color palette matches `chart-skill` and `pdf-rendering-skill` for visual consistency.
