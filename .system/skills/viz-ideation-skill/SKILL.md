# viz-ideation-skill

Brainstorms compelling data visualizations for a geographic research topic.
Part of the idea-first visualization pipeline (step 4a).

## Entry Point

```python
from main import ideate_visualizations, VizIdea, DataRequirement

ideas = await ideate_visualizations("Espérance de vie en Afrique subsaharienne", max_ideas=3)
```

## Output

Returns `list[VizIdea]` — each with category (chart/map/choropleth), French title, and specific DataRequirements for viz-data-fetch-skill.
