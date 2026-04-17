# viz-data-fetch-skill

Fetches specific data from open APIs based on visualization requirements.
Part of the idea-first visualization pipeline (step 4b).

## Entry Point

```python
from main import fetch_viz_data, DataRequirement, FetchedData

results = await fetch_viz_data([
    DataRequirement(source="worldbank", params={
        "indicator": "SP.DYN.LE00.IN",
        "countries_iso3": ["NGA", "KEN", "ZAF"],
        "year_start": 1990, "year_end": 2024,
    }),
])
```

## Supported Sources

| Source | Params | Output CSV columns |
|--------|--------|--------------------|
| `worldbank` | indicator, countries_iso3, year_start, year_end | country, iso3, year, value, indicator, label |
| `gbif` | taxon_hints, bbox, max_records | lat, lon, species, year, country |
| `owid` | countries_iso3, columns, year_start, year_end | country, iso3, year, [columns...] |
| `nasa` | lat, lon, year_start, year_end, parameters | year, T2M, PRECTOTCORR |

## Output

All data is returned as CSV strings ready for the Claude sandbox (pandas.read_csv).
