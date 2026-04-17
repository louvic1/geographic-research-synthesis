"""
viz-ideation-skill
Brainstorms the most compelling data visualizations for a geographic research topic.

Idea-first approach: Claude analyzes the topic and generates creative visualization
ideas BEFORE any data is fetched. Each idea includes specific DataRequirements
that tell viz-data-fetch-skill exactly what to retrieve.

Usage:
    ideas = await ideate_visualizations("Espérance de vie en Afrique subsaharienne")
    # Returns 2-3 VizIdea objects with data requirements
"""

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

import anthropic
import pycountry
from dotenv import load_dotenv

load_dotenv()

CLAUDE_MODEL = "claude-sonnet-4-6"
CURRENT_YEAR = datetime.now().year


# ---------------------------------------------------------------------------
# Data structures (shared with viz-data-fetch-skill)
# ---------------------------------------------------------------------------

@dataclass
class DataRequirement:
    source: str             # "worldbank" | "gbif" | "owid" | "nasa" | "open-meteo"
    params: dict            # source-specific parameters
    role: str = "primary"   # "primary" | "secondary"
    description: str = ""


@dataclass
class VizIdea:
    id: str                    # "viz-1", "viz-2"
    category: str              # "chart" | "map" | "choropleth"
    description: str           # what the visualization shows
    title_fr: str              # French title for the figure
    why_compelling: str        # why this viz is relevant to the topic
    data_requirements: list[DataRequirement] = field(default_factory=list)
    fallback_idea: Optional[str] = None


# ---------------------------------------------------------------------------
# Basin expansion (from data-scout-skill)
# ---------------------------------------------------------------------------

_BASIN_COUNTRIES: dict[str, list[dict]] = {
    "amazon": [
        {"name": "Brazil",    "iso2": "BR", "iso3": "BRA"},
        {"name": "Peru",      "iso2": "PE", "iso3": "PER"},
        {"name": "Bolivia",   "iso2": "BO", "iso3": "BOL"},
        {"name": "Colombia",  "iso2": "CO", "iso3": "COL"},
        {"name": "Ecuador",   "iso2": "EC", "iso3": "ECU"},
        {"name": "Venezuela", "iso2": "VE", "iso3": "VEN"},
        {"name": "Guyana",    "iso2": "GY", "iso3": "GUY"},
        {"name": "Suriname",  "iso2": "SR", "iso3": "SUR"},
    ],
    "congo": [
        {"name": "Democratic Republic of Congo", "iso2": "CD", "iso3": "COD"},
        {"name": "Congo",                        "iso2": "CG", "iso3": "COG"},
        {"name": "Cameroon",                     "iso2": "CM", "iso3": "CMR"},
        {"name": "Gabon",                        "iso2": "GA", "iso3": "GAB"},
        {"name": "Central African Republic",     "iso2": "CF", "iso3": "CAF"},
    ],
    "sahel": [
        {"name": "Mali",         "iso2": "ML", "iso3": "MLI"},
        {"name": "Niger",        "iso2": "NE", "iso3": "NER"},
        {"name": "Chad",         "iso2": "TD", "iso3": "TCD"},
        {"name": "Burkina Faso", "iso2": "BF", "iso3": "BFA"},
        {"name": "Senegal",      "iso2": "SN", "iso3": "SEN"},
        {"name": "Mauritania",   "iso2": "MR", "iso3": "MRT"},
    ],
    "mekong": [
        {"name": "China",    "iso2": "CN", "iso3": "CHN"},
        {"name": "Laos",     "iso2": "LA", "iso3": "LAO"},
        {"name": "Thailand", "iso2": "TH", "iso3": "THA"},
        {"name": "Cambodia", "iso2": "KH", "iso3": "KHM"},
        {"name": "Vietnam",  "iso2": "VN", "iso3": "VNM"},
        {"name": "Myanmar",  "iso2": "MM", "iso3": "MMR"},
    ],
    "scandinavia": [
        {"name": "Norway",  "iso2": "NO", "iso3": "NOR"},
        {"name": "Sweden",  "iso2": "SE", "iso3": "SWE"},
        {"name": "Finland", "iso2": "FI", "iso3": "FIN"},
        {"name": "Denmark", "iso2": "DK", "iso3": "DNK"},
        {"name": "Iceland", "iso2": "IS", "iso3": "ISL"},
    ],
    "sub_saharan_africa": [
        {"name": "Nigeria",      "iso2": "NG", "iso3": "NGA"},
        {"name": "Ethiopia",     "iso2": "ET", "iso3": "ETH"},
        {"name": "Kenya",        "iso2": "KE", "iso3": "KEN"},
        {"name": "South Africa", "iso2": "ZA", "iso3": "ZAF"},
        {"name": "Tanzania",     "iso2": "TZ", "iso3": "TZA"},
        {"name": "Ghana",        "iso2": "GH", "iso3": "GHA"},
        {"name": "Senegal",      "iso2": "SN", "iso3": "SEN"},
        {"name": "Mozambique",   "iso2": "MZ", "iso3": "MOZ"},
        {"name": "Cameroon",     "iso2": "CM", "iso3": "CMR"},
        {"name": "Uganda",       "iso2": "UG", "iso3": "UGA"},
        {"name": "Mali",         "iso2": "ML", "iso3": "MLI"},
        {"name": "Chad",         "iso2": "TD", "iso3": "TCD"},
        {"name": "Niger",        "iso2": "NE", "iso3": "NER"},
        {"name": "Burkina Faso", "iso2": "BF", "iso3": "BFA"},
        {"name": "Rwanda",       "iso2": "RW", "iso3": "RWA"},
        {"name": "Zambia",       "iso2": "ZM", "iso3": "ZMB"},
        {"name": "Zimbabwe",     "iso2": "ZW", "iso3": "ZWE"},
        {"name": "Botswana",     "iso2": "BW", "iso3": "BWA"},
        {"name": "Namibia",      "iso2": "NA", "iso3": "NAM"},
        {"name": "Madagascar",   "iso2": "MG", "iso3": "MDG"},
    ],
}

_BASIN_KEYWORDS: dict[str, list[str]] = {
    "amazon":              ["amazon", "amazoni", "amazônia"],
    "congo":               ["congo", "bassin congolais", "forêt congolaise"],
    "sahel":               ["sahel"],
    "mekong":              ["mekong", "mékong"],
    "scandinavia":         ["scandinav", "nordique", "pays nordiques"],
    "sub_saharan_africa":  ["afrique subsaharienne", "sub-saharan", "subsaharienne"],
}


def _detect_basin(topic: str) -> Optional[str]:
    """Detect if topic mentions a known geographic basin/region."""
    topic_lower = topic.lower()
    for basin_key, keywords in _BASIN_KEYWORDS.items():
        if any(kw in topic_lower for kw in keywords):
            return basin_key
    return None


def _enrich_countries_iso3(countries: list[dict]) -> list[dict]:
    """Ensure every country dict has iso3 and iso2 codes using pycountry."""
    for c in countries:
        if c.get("iso3") and c.get("iso2"):
            continue
        name = c.get("name", "")
        if not name:
            continue
        match = None
        try:
            match = pycountry.countries.lookup(name)
        except LookupError:
            try:
                results = pycountry.countries.search_fuzzy(name)
                if results:
                    match = results[0]
            except LookupError:
                pass
        if match:
            c["iso3"] = c.get("iso3") or match.alpha_3
            c["iso2"] = c.get("iso2") or match.alpha_2
    return countries


# ---------------------------------------------------------------------------
# World Bank indicators reference (for the ideation prompt)
# ---------------------------------------------------------------------------

INDICATOR_LABELS: dict[str, str] = {
    "SP.URB.TOTL.IN.ZS": "Population urbaine (% du total)",
    "SP.RUR.TOTL.ZS":    "Population rurale (% du total)",
    "EN.URB.MCTY.TL.ZS": "Pop. agglomérations > 1M (%)",
    "AG.LND.AGRI.ZS":    "Terres agricoles (% superficie terrestre)",
    "AG.LND.ARBL.ZS":    "Terres arables (% superficie terrestre)",
    "AG.LND.FRST.ZS":    "Superficie forestière (% superficie terrestre)",
    "AG.LND.FRST.K2":    "Superficie forestière (km²)",
    "ER.LND.PTLD.ZS":    "Aires protégées terrestres (%)",
    "EN.ATM.CO2E.PC":    "Émissions CO₂ (t/hab.)",
    "EN.ATM.CO2E.KT":    "Émissions CO₂ totales (kt)",
    "EG.USE.PCAP.KG.OE": "Consommation énergétique (kg/hab.)",
    "EN.ATM.GHGT.KT.CE": "Émissions GES totales (kt CO₂ éq.)",
    "SP.POP.TOTL":        "Population totale",
    "SP.POP.GROW":        "Croissance démographique (%/an)",
    "NY.GDP.MKTP.CD":    "PIB (USD courants)",
    "NY.GDP.PCAP.CD":    "PIB par habitant (USD)",
    "SI.POV.GINI":       "Indice de Gini",
    "SP.DYN.LE00.IN":    "Espérance de vie à la naissance (années)",
    "SH.XPD.CHEX.GD.ZS": "Dépenses de santé (% PIB)",
    "SP.DYN.IMRT.IN":    "Mortalité infantile (‰)",
}


# ---------------------------------------------------------------------------
# Ideation prompt
# ---------------------------------------------------------------------------

IDEATION_PROMPT = """Tu es un expert en visualisation de données géographiques. Ton rôle est de proposer les visualisations les plus percutantes et informatives pour un sujet de recherche donné.

## Sujet de recherche
{topic}

## Pays/région identifiés
{countries_info}

## Sources de données disponibles

### 1. World Bank API (indicateurs socioéconomiques/environnementaux)
Indicateurs disponibles:
{indicators_list}
- Format: séries temporelles par pays (1960-{current_year})
- Paramètres requis: indicator (code WB), countries_iso3 (liste ISO3), year_start, year_end

### 2. GBIF (biodiversité)
- Occurrences d'espèces avec coordonnées GPS (lat/lon)
- Recherche par nom d'espèce (taxon) ou par zone géographique (bounding box)
- Paramètres requis: taxon_hints (noms d'espèces) OU bbox (lat_min, lat_max, lon_min, lon_max)
- Idéal pour: heatmaps de distribution, cartes de points

### 3. Our World in Data (OWID) — émissions CO₂/GES
- Colonnes: co2, co2_per_capita, co2_per_gdp, total_ghg, methane, land_use_change_co2, co2_including_luc, temperature_change_from_co2
- Paramètres requis: countries_iso3, columns (liste de noms de colonnes), year_start, year_end

### 4. NASA POWER / Open-Meteo (climat)
- Données annuelles: température moyenne (T2M), précipitations (PRECTOTCORR)
- Pour un point géographique précis (lat/lon)
- Paramètres requis: lat, lon, year_start, year_end

## Ta tâche

Propose exactement {max_ideas} idées de visualisation. Chaque idée doit:
1. Être directement pertinente au sujet de recherche
2. Raconter une histoire claire avec les données
3. Spécifier EXACTEMENT quelles données sont nécessaires (source + paramètres)

Contraintes:
- Au moins 1 idée de type "chart" (graphique: ligne, barres, scatter, etc.)
- Au moins 1 idée de type "map" ou "choropleth" (carte géographique)
- Chaque idée doit utiliser des données de la source la plus appropriée
- Les codes ISO3 des pays doivent être corrects
- Pour les choropleths, inclure TOUS les pays de la région (pas seulement 3-4)
- Pour les charts avec beaucoup de pays, limiter à 8 pays représentatifs

## Format de sortie

Retourne UNIQUEMENT du JSON valide:
```json
[
  {{
    "id": "viz-1",
    "category": "chart",
    "description": "Description de ce que montre la visualisation",
    "title_fr": "Titre français pour la figure",
    "why_compelling": "Pourquoi cette visualisation est pertinente",
    "data_requirements": [
      {{
        "source": "worldbank",
        "params": {{
          "indicator": "SP.DYN.LE00.IN",
          "countries_iso3": ["NGA", "KEN", "ZAF", "ETH", "TZA", "GHA", "SEN", "MOZ"],
          "year_start": 1990,
          "year_end": {current_year}
        }},
        "role": "primary",
        "description": "Espérance de vie pour 8 pays représentatifs"
      }}
    ],
    "fallback_idea": "Graphique en barres si séries temporelles trop clairsemées"
  }},
  {{
    "id": "viz-2",
    "category": "choropleth",
    "description": "Carte choroplèthe de l'espérance de vie par pays",
    "title_fr": "Espérance de vie à la naissance par pays — Afrique subsaharienne",
    "why_compelling": "Révèle les inégalités spatiales en santé",
    "data_requirements": [
      {{
        "source": "worldbank",
        "params": {{
          "indicator": "SP.DYN.LE00.IN",
          "countries_iso3": ["NGA", "KEN", "ZAF", "ETH", ...tous les pays de la région],
          "year_start": 2020,
          "year_end": {current_year}
        }},
        "role": "primary",
        "description": "Dernières valeurs d'espérance de vie pour tous les pays"
      }}
    ],
    "fallback_idea": null
  }}
]
```
"""


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

async def ideate_visualizations(
    topic: str,
    max_ideas: int = 3,
) -> list[VizIdea]:
    """
    Brainstorm the most compelling visualizations for a research topic.
    Returns a list of VizIdea objects with specific DataRequirements.
    """
    client = anthropic.AsyncAnthropic()

    # Detect basin and get country list
    basin = _detect_basin(topic)
    if basin and basin in _BASIN_COUNTRIES:
        countries = list(_BASIN_COUNTRIES[basin])
    else:
        countries = []

    # If no basin detected, let Claude figure out the countries
    countries_info = "Aucun pays spécifique détecté — à toi de déterminer les pays pertinents."
    if countries:
        countries = _enrich_countries_iso3(countries)
        countries_info = json.dumps(countries, ensure_ascii=False, indent=2)

    indicators_list = "\n".join(f"  {k}: {v}" for k, v in INDICATOR_LABELS.items())

    prompt = IDEATION_PROMPT.format(
        topic=topic,
        countries_info=countries_info,
        indicators_list=indicators_list,
        current_year=CURRENT_YEAR,
        max_ideas=max_ideas,
    )

    try:
        msg = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )

        # Extract text from response
        raw = msg.content[0].text.strip()

        # Clean markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        # Try to extract JSON array even if there's text around it
        json_match = re.search(r"\[.*\]", raw, re.DOTALL)
        if json_match:
            raw = json_match.group(0)

        ideas_data = json.loads(raw)

        # Convert to VizIdea objects
        ideas: list[VizIdea] = []
        for item in ideas_data:
            reqs = [
                DataRequirement(
                    source=r["source"],
                    params=r["params"],
                    role=r.get("role", "primary"),
                    description=r.get("description", ""),
                )
                for r in item.get("data_requirements", [])
            ]
            ideas.append(VizIdea(
                id=item["id"],
                category=item["category"],
                description=item["description"],
                title_fr=item["title_fr"],
                why_compelling=item["why_compelling"],
                data_requirements=reqs,
                fallback_idea=item.get("fallback_idea"),
            ))

        # Validate: at least 1 chart and 1 map/choropleth
        has_chart = any(i.category == "chart" for i in ideas)
        has_map = any(i.category in ("map", "choropleth") for i in ideas)
        if not has_chart or not has_map:
            print(f"[viz-ideation] Warning: missing chart={not has_chart}, map={not has_map}")

        print(f"[viz-ideation] Generated {len(ideas)} ideas: {[i.category for i in ideas]}")
        return ideas

    except Exception as e:
        print(f"[viz-ideation] Failed: {e}")
        return _fallback_ideas(topic, countries)


def _fallback_ideas(topic: str, countries: list[dict]) -> list[VizIdea]:
    """Generate basic fallback ideas if Claude fails."""
    iso_codes = [c["iso3"] for c in countries if c.get("iso3")][:8]
    if not iso_codes:
        iso_codes = ["FRA"]  # default

    return [
        VizIdea(
            id="viz-1",
            category="chart",
            description=f"Graphique des principaux indicateurs pour le sujet: {topic}",
            title_fr=f"Indicateurs clés — {topic[:60]}",
            why_compelling="Vue d'ensemble des tendances temporelles",
            data_requirements=[
                DataRequirement(
                    source="worldbank",
                    params={
                        "indicator": "SP.POP.TOTL",
                        "countries_iso3": iso_codes,
                        "year_start": 1990,
                        "year_end": CURRENT_YEAR,
                    },
                    role="primary",
                    description="Population totale comme indicateur par défaut",
                )
            ],
            fallback_idea=None,
        ),
    ]


# ---------------------------------------------------------------------------
# CLI for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    import argparse

    parser = argparse.ArgumentParser(description="Test viz-ideation-skill")
    parser.add_argument("--topic", required=True)
    parser.add_argument("--max-ideas", type=int, default=3)
    args = parser.parse_args()

    async def _test():
        ideas = await ideate_visualizations(args.topic, args.max_ideas)
        for idea in ideas:
            print(f"\n{'='*60}")
            print(f"[{idea.id}] {idea.category}: {idea.title_fr}")
            print(f"  Description: {idea.description}")
            print(f"  Why: {idea.why_compelling}")
            for req in idea.data_requirements:
                print(f"  Data: {req.source} — {req.description}")
                print(f"    Params: {json.dumps(req.params, ensure_ascii=False)}")
            if idea.fallback_idea:
                print(f"  Fallback: {idea.fallback_idea}")

    asyncio.run(_test())
