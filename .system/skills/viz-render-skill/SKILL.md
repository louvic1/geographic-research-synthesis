# viz-render-skill

Renders visualizations using Claude API code execution (PNG) and local Folium (HTML).
Part of the idea-first visualization pipeline (step 4c).

## Entry Point

```python
from main import render_visualization, VizIdea, FetchedData, DataRequirement, RenderResult

result = await render_visualization(idea, fetched_data, output_dir, slug, formats=["png", "html"])
```

## Rendering Paths

| Output | Engine | Location |
|--------|--------|----------|
| PNG charts | Claude API `code_execution_20260120` | Anthropic sandbox |
| PNG choropleths | Claude API + bundled GeoJSON | Anthropic sandbox |
| PNG heatmaps | Claude API + cartopy | Anthropic sandbox |
| HTML maps | Folium (heatmap/points) | Local |
| HTML choropleths | Plotly Express | Local |

## Bundled Data

- `data/ne_110m_admin_0_countries.geojson` — Natural Earth 110m country boundaries (~840KB)

## Fallback

If sandbox rendering fails, falls back to local matplotlib rendering (basic charts/scatter).
