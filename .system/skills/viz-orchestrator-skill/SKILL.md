# viz-orchestrator-skill

Coordinates the idea-first visualization pipeline. Replaces data-scout-skill as step 4 of system-1.

## Entry Point

```python
from main import run_viz_pipeline

result = await run_viz_pipeline(
    topic="Espérance de vie en Afrique subsaharienne",
    output_dir="./output/figures",
    formats=["png", "html"],
)
```

## Pipeline

```
ideation (viz-ideation-skill)
    → data fetch (viz-data-fetch-skill)
    → rendering (viz-render-skill)
    → scout.json-compatible output
```

## Output Format

Same as data-scout-skill's `run_data_scout()`:
```python
{
    "chart": str | None,        # path to chart.png
    "chart2": str | None,       # path to chart2.png
    "map": {"png": str, "html": str, "title": str, "type": str} | None,
    "datasets": {},
    "sources_queried": [...],
    "viz_plan": {"ideas": [...], "chart": {...}, "map": {...}},
}
```

## Dependencies

- viz-ideation-skill
- viz-data-fetch-skill
- viz-render-skill
