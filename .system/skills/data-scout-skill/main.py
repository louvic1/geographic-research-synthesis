"""
data-scout-skill
Hunts the best available open data for a geographic research topic,
scores each dataset by richness, then renders the best chart + map.

New data sources (beyond chart-skill / map-skill defaults):
  - GBIF   — species occurrence lat/lon points  → heatmap or point_map
  - FAO FAOSTAT — forest area time series       → line_chart or bar_chart

World Bank + NASA POWER are also fetched (functions imported from chart-skill).

Usage:
    python main.py --topic "Jaguar distribution in Amazon basin" --output-dir /tmp/scout
    python main.py --topic "Deforestation in Congo basin" --output-dir /tmp/scout2
"""

import argparse
import asyncio
import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic
import httpx
import pycountry
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Load sibling skills via importlib (avoids "main" module name collision)
# ---------------------------------------------------------------------------

SKILLS_DIR = Path(__file__).parent.parent


def _load_from_skill(skill_name: str, *attr_names: str) -> tuple:
    """Import named attributes from a sibling skill's main.py."""
    skill_path  = SKILLS_DIR / skill_name / "main.py"
    module_name = skill_name.replace("-", "_")
    spec        = importlib.util.spec_from_file_location(module_name, skill_path)
    module      = importlib.util.module_from_spec(spec)
    skill_dir   = str(SKILLS_DIR / skill_name)
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    spec.loader.exec_module(module)
    return tuple(getattr(module, name) for name in attr_names)


(
    render_chart,
    slugify,
    PALETTE,
    SERIES_COLORS,
    INDICATOR_LABELS,
    fetch_worldbank_series,
    fetch_nasa_power,
    apply_pub_style,
) = _load_from_skill(
    "chart-skill",
    "render_chart", "slugify", "PALETTE", "SERIES_COLORS",
    "INDICATOR_LABELS", "fetch_worldbank_series", "fetch_nasa_power", "apply_pub_style",
)

(render_choropleth,) = _load_from_skill("map-skill", "render_choropleth")

CLAUDE_FAST  = "claude-haiku-4-5-20251001"
CLAUDE_SMART = "claude-sonnet-4-6"
CURRENT_YEAR = datetime.now().year

# ---------------------------------------------------------------------------
# FAOSTAT area code lookup (ISO3 → FAOSTAT numeric code)
# ---------------------------------------------------------------------------

# OWID columns to fetch per thematic scope (Our World in Data CO2 dataset)
OWID_COLUMNS_BY_SCOPE: dict[str, list[str]] = {
    "land_use":      ["land_use_change_co2", "co2_including_luc"],
    "deforestation": ["land_use_change_co2", "co2_including_luc"],
    "climate":       ["co2", "total_ghg", "temperature_change_from_co2"],
    "food_security": ["total_ghg", "methane"],
    "biodiversity":  ["land_use_change_co2"],
    "socioeconomic": ["co2_per_capita", "co2_per_gdp"],
    "urban":         ["co2_per_capita"],
    # health: OWID CO2 dataset has no health columns — skip OWID for health topics
    "water":         ["methane"],
    "_default":      ["co2", "co2_per_capita"],
}

OWID_COLUMN_LABELS: dict[str, tuple[str, str]] = {
    "land_use_change_co2":       ("CO₂ émis par changement d'usage des terres", "Mt CO₂"),
    "co2_including_luc":         ("CO₂ total incl. déforestation",              "Mt CO₂"),
    "co2":                       ("Émissions de CO₂ fossile",                   "Mt CO₂"),
    "co2_per_capita":            ("CO₂ par habitant",                           "t CO₂/hab"),
    "co2_per_gdp":               ("Intensité carbone du PIB",                   "kg CO₂/$"),
    "total_ghg":                 ("Émissions totales de GES",                   "Mt CO₂eq"),
    "total_ghg_excluding_lucf":  ("GES hors changement d'usage",                "Mt CO₂eq"),
    "temperature_change_from_co2": ("Contribution au réchauffement (CO₂)",      "°C"),
    "methane":                   ("Émissions de méthane",                       "Mt CO₂eq"),
}

# ---------------------------------------------------------------------------
# Thematic indicator filtering — only WB indicators that DIRECTLY measure
# the studied phenomenon are kept (prevents PIB/population charts for
# glacial melting or biodiversity topics).
# ---------------------------------------------------------------------------

THEMATIC_INDICATORS: dict[str, list[str]] = {
    "AG.LND.FRST.ZS":    ["deforestation", "forest", "land_use"],
    "AG.LND.FRST.K2":    ["deforestation", "forest"],
    "ER.LND.PTLD.ZS":    ["conservation", "biodiversity", "land_use"],
    "AG.LND.AGRI.ZS":    ["agriculture", "land_use", "food_security"],
    "AG.LND.ARBL.ZS":    ["agriculture", "food_security"],
    "SP.URB.TOTL.IN.ZS": ["urbanization", "urban", "population"],
    "SP.RUR.TOTL.ZS":    ["urbanization", "urban", "population"],
    "EN.URB.MCTY.TL.ZS": ["urbanization", "urban"],
    "EN.ATM.CO2E.PC":    ["climate", "emissions"],
    "EN.ATM.CO2E.KT":    ["climate", "emissions", "deforestation"],
    "EN.ATM.GHGT.KT.CE": ["climate", "emissions"],
    "EG.USE.PCAP.KG.OE": ["energy", "climate"],
    "NY.GDP.PCAP.CD":    ["economics", "development", "socioeconomic"],
    "NY.GDP.MKTP.CD":    ["economics", "socioeconomic"],
    "SP.POP.TOTL":       ["population", "demography", "socioeconomic"],
    "SP.POP.GROW":       ["population", "demography"],
    "SP.DYN.LE00.IN":    ["health"],
    "SH.XPD.CHEX.GD.ZS": ["health"],
    "SP.DYN.IMRT.IN":    ["health"],
    "SI.POV.GINI":       ["inequality", "social", "socioeconomic"],
}

# Maps thematic_scope → accepted indicator tags
SCOPE_TAGS: dict[str, list[str]] = {
    "biodiversity":  ["biodiversity", "forest", "land_use", "conservation"],
    "climate":       ["climate", "emissions", "energy"],
    "land_use":      ["land_use", "deforestation", "forest", "agriculture"],
    "food_security": ["food_security", "agriculture"],
    "socioeconomic": ["economics", "socioeconomic", "population", "inequality", "social"],
    "urban":         ["urban", "urbanization", "population"],
    "health":        ["health"],
    "water":         ["water"],
}

# Relevance multiplier per data source per thematic scope.
# Multiplied into richness score so the most thematically relevant datasets rank first.
RELEVANCE_BY_SCOPE: dict[str, dict[str, float]] = {
    "biodiversity":  {"gbif": 1.0, "owid": 0.6, "worldbank": 0.5, "nasa": 0.3},
    "climate":       {"nasa": 1.0, "worldbank": 0.8, "owid": 0.7, "gbif": 0.4},
    "land_use":      {"owid": 1.0, "worldbank": 0.9, "gbif": 0.5, "nasa": 0.3},
    "food_security": {"owid": 0.9, "worldbank": 0.8, "gbif": 0.3, "nasa": 0.3},
    "socioeconomic": {"worldbank": 1.0, "owid": 0.6, "gbif": 0.1, "nasa": 0.2},
    "urban":         {"worldbank": 1.0, "owid": 0.4, "gbif": 0.2, "nasa": 0.4},
    "health":        {"worldbank": 1.0, "owid": 0.5, "gbif": 0.2, "nasa": 0.3},
    "water":         {"worldbank": 0.8, "nasa": 0.9, "gbif": 0.5, "owid": 0.4},
}


def _enrich_countries_iso3(countries: list[dict]) -> list[dict]:
    """Ensure every country dict has iso3 and iso2 codes using pycountry."""
    for c in countries:
        if c.get("iso3") and c.get("iso2"):
            continue
        name = c.get("name", "")
        if not name:
            continue
        # Try exact lookup, then fuzzy search
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
    enriched = len([c for c in countries if c.get("iso3")])
    print(f"[scout:analyze] enriched ISO codes: {enriched}/{len(countries)} countries")
    return countries


def _filter_wb_indicators(indicators: list[str], thematic_scope: str) -> list[str]:
    """Keep only WB indicators whose tags overlap with the thematic_scope's accepted tags."""
    accepted = SCOPE_TAGS.get(thematic_scope, [])
    if not accepted:
        return indicators  # unknown scope → no filter
    filtered = []
    for ind in indicators:
        ind_tags = THEMATIC_INDICATORS.get(ind)
        if ind_tags is None:
            filtered.append(ind)  # unknown indicator → pass through
        elif any(tag in ind_tags for tag in accepted):
            filtered.append(ind)
    if len(filtered) < len(indicators):
        dropped = [i for i in indicators if i not in filtered]
        print(f"[scout:analyze] filtered out irrelevant WB indicators: {dropped} (scope={thematic_scope})")
    return filtered


# ---------------------------------------------------------------------------
# Step 1 — Analyze topic for scout
# ---------------------------------------------------------------------------

ANALYZE_SCOUT_PROMPT = """Analyse ce sujet de recherche géographique pour identifier les meilleures sources de données.

Sujet: {topic}

Retourne UNIQUEMENT du JSON valide:
{{
  "thematic_scope": "biodiversity",
  "geographic_scope": "regional",
  "countries": [{{"name": "Brazil", "iso2": "BR", "iso3": "BRA"}}],
  "bbox": {{"lat_min": -15.0, "lat_max": 5.0, "lon_min": -75.0, "lon_max": -45.0}},
  "lat": -3.0,
  "lon": -60.0,
  "taxon_hints": ["Panthera onca", "jaguar"],
  "worldbank_indicators": ["AG.LND.FRST.ZS", "ER.LND.PTLD.ZS"],
  "is_climate_topic": false,
  "year_start": 1990,
  "year_end": {current_year},
  "sources_to_query": ["gbif", "worldbank"]
}}

Règles:
- thematic_scope: [biodiversity, climate, land_use, food_security, socioeconomic, urban, water, health]
- geographic_scope: [global, regional, country, city]
- taxon_hints: noms d'espèces ou groupes taxonomiques si biodiversité, sinon []
- worldbank_indicators: 2-4 indicateurs parmi: {indicators_list}
- bbox: boîte englobante de la zone d'étude (degrés décimaux)
- is_climate_topic: true si températures, précipitations, événements météo
- sources_to_query: liste ordonnée par pertinence parmi [gbif, owid, worldbank, nasa]
  * gbif      → biodiversité, espèces, faune, flore; AUSSI pour sujets glaciaires/cryosphériques (espèces indicatrices arctiques/alpines comme proxy d'impact)
  * owid      → CO₂, GES, émissions liées au changement d'usage des terres (déforestation), méthane, réchauffement climatique (Our World in Data)
  * worldbank → toujours pertinent pour indicateurs socioéconomiques/environnementaux (superficie forestière, aires protégées, PIB, population)
  * nasa      → climat local, températures, précipitations (données météo)

RÈGLE SPÉCIALE — sujets glaciaires/cryosphériques (fonte des glaces, glaciers, banquise, pergélisol):
- Inclure gbif dans sources_to_query
- Remplir taxon_hints avec 2-3 espèces indicatrices de la cryosphère pour la région:
  * Scandinavie/Arctique → ["Rangifer tarandus", "Ursus maritimus", "Lemmus lemmus"]
  * Alpes/Europe → ["Marmota marmota", "Lagopus muta", "Pinus cembra"]
  * Himalaya/Asie → ["Panthera uncia", "Bos grunniens"]
  * Antarctique → ["Aptenodytes forsteri", "Pygoscelis papua"]
- Ces espèces froides permettent une heatmap de distribution pertinente comme proxy écologique de la cryosphère

IMPORTANT — bassins géographiques multi-pays: liste TOUS les pays du bassin dans "countries"
- Amazonie / bassin amazonien → Brésil, Pérou, Bolivie, Colombie, Équateur, Venezuela, Guyane, Suriname, Guyane française
- Congo / forêt congolaise → RDC, Congo, Cameroun, Gabon, RCA, Guinée équatoriale
- Sahel → Mali, Niger, Tchad, Burkina Faso, Sénégal, Mauritanie, Nigeria
- Mékong → Chine, Laos, Thaïlande, Cambodge, Vietnam, Myanmar
- Arctique → Canada, Russie, Norvège, Suède, Finlande, Danemark, États-Unis, Islande
Ne jamais retourner un seul pays pour un sujet régional/bassin — les comparaisons multi-pays sont essentielles.
"""


async def analyze_topic_for_scout(topic: str, client: anthropic.AsyncAnthropic) -> dict:
    indicators_list = ", ".join(list(INDICATOR_LABELS.keys())[:20])
    prompt = ANALYZE_SCOUT_PROMPT.format(
        topic=topic,
        current_year=CURRENT_YEAR,
        indicators_list=indicators_list,
    )
    try:
        msg = await client.messages.create(
            model=CLAUDE_FAST,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)
        # Filter WB indicators to only those directly relevant to the thematic scope
        scope = result.get("thematic_scope", "")
        result["worldbank_indicators"] = _filter_wb_indicators(
            result.get("worldbank_indicators", []), scope
        )
        # Enrich countries with ISO codes via pycountry
        if result.get("countries"):
            result["countries"] = _enrich_countries_iso3(result["countries"])
        return result
    except Exception as e:
        print(f"[scout:analyze] failed: {e}")
        return {
            "thematic_scope": "environment",
            "geographic_scope": "regional",
            "countries": [],
            "bbox": None,
            "lat": None, "lon": None,
            "taxon_hints": [],
            "worldbank_indicators": ["AG.LND.FRST.ZS"],
            "is_climate_topic": False,
            "year_start": 1990,
            "year_end": CURRENT_YEAR,
            "sources_to_query": ["worldbank"],
        }


# ---------------------------------------------------------------------------
# Basin expansion — inject all countries for known multi-country regions
# ---------------------------------------------------------------------------

_BASIN_COUNTRIES: dict[str, list[dict]] = {
    "amazon": [
        {"name": "Brazil",          "iso2": "BR", "iso3": "BRA"},
        {"name": "Peru",            "iso2": "PE", "iso3": "PER"},
        {"name": "Bolivia",         "iso2": "BO", "iso3": "BOL"},
        {"name": "Colombia",        "iso2": "CO", "iso3": "COL"},
        {"name": "Ecuador",         "iso2": "EC", "iso3": "ECU"},
        {"name": "Venezuela",       "iso2": "VE", "iso3": "VEN"},
        {"name": "Guyana",          "iso2": "GY", "iso3": "GUY"},
        {"name": "Suriname",        "iso2": "SR", "iso3": "SUR"},
        # Note: French Guiana (GUF) excluded — not a World Bank country
    ],
    "congo": [
        {"name": "Democratic Republic of Congo", "iso2": "CD", "iso3": "COD"},
        {"name": "Congo",           "iso2": "CG", "iso3": "COG"},
        {"name": "Cameroon",        "iso2": "CM", "iso3": "CMR"},
        {"name": "Gabon",           "iso2": "GA", "iso3": "GAB"},
        {"name": "Central African Republic", "iso2": "CF", "iso3": "CAF"},
    ],
    "sahel": [
        {"name": "Mali",            "iso2": "ML", "iso3": "MLI"},
        {"name": "Niger",           "iso2": "NE", "iso3": "NER"},
        {"name": "Chad",            "iso2": "TD", "iso3": "TCD"},
        {"name": "Burkina Faso",    "iso2": "BF", "iso3": "BFA"},
        {"name": "Senegal",         "iso2": "SN", "iso3": "SEN"},
        {"name": "Mauritania",      "iso2": "MR", "iso3": "MRT"},
    ],
    "mekong": [
        {"name": "China",           "iso2": "CN", "iso3": "CHN"},
        {"name": "Laos",            "iso2": "LA", "iso3": "LAO"},
        {"name": "Thailand",        "iso2": "TH", "iso3": "THA"},
        {"name": "Cambodia",        "iso2": "KH", "iso3": "KHM"},
        {"name": "Vietnam",         "iso2": "VN", "iso3": "VNM"},
        {"name": "Myanmar",         "iso2": "MM", "iso3": "MMR"},
    ],
}

_BASIN_KEYWORDS: dict[str, list[str]] = {
    "amazon":  ["amazon", "amazoni", "amazônia"],
    "congo":   ["congo", "bassin congolais", "forêt congolaise"],
    "sahel":   ["sahel"],
    "mekong":  ["mekong", "mékong"],
}


_BASIN_TO_PLOTLY_SCOPE: dict[str, str] = {
    "amazon":  "south america",
    "congo":   "africa",
    "sahel":   "africa",
    "mekong":  "asia",
}


def _expand_basin_countries(context: dict, topic: str) -> dict:
    """If topic mentions a known multi-country basin, REPLACE countries with the hardcoded basin list.
    Also stores basin name in context for downstream scope selection."""
    topic_lower = topic.lower()
    for basin_key, keywords in _BASIN_KEYWORDS.items():
        if any(kw in topic_lower for kw in keywords):
            context["countries"] = list(_BASIN_COUNTRIES[basin_key])
            context["basin"]     = basin_key
            print(f"[scout:analyze] basin '{basin_key}' detected → {len(context['countries'])} countries")
            break
    return context


# ---------------------------------------------------------------------------
# Step 2a — Fetch GBIF species occurrences
# ---------------------------------------------------------------------------

async def _resolve_taxon_key(taxon_name: str, client: httpx.AsyncClient) -> Optional[int]:
    try:
        resp = await client.get(
            "https://api.gbif.org/v1/species/suggest",
            params={"q": taxon_name, "limit": 5},
            timeout=15,
        )
        resp.raise_for_status()
        for item in resp.json():
            if item.get("key"):
                return item["key"]
    except Exception as e:
        print(f"[scout:gbif] taxon resolve failed for '{taxon_name}': {e}")
    return None


async def fetch_gbif_occurrences(context: dict, client: httpx.AsyncClient) -> Optional[dict]:
    """
    Fetch species occurrence points from GBIF.
    Uses taxon mode if context has taxon_hints, bbox mode as fallback.
    Returns {"source": "gbif", "type": "points", "points": [...]} or None.
    """
    params: dict = {
        "limit": 1000,
        "hasCoordinate": "true",
        "hasGeospatialIssue": "false",
    }
    taxon_name = ""
    taxon_hints = context.get("taxon_hints", [])

    if taxon_hints:
        taxon_key = await _resolve_taxon_key(taxon_hints[0], client)
        if taxon_key:
            params["taxonKey"] = taxon_key
            taxon_name = taxon_hints[0]

    if "taxonKey" not in params:
        bbox = context.get("bbox")
        if not bbox:
            print("[scout:gbif] no taxon key and no bbox, skipping")
            return None
        params["decimalLatitude"]  = f"{bbox['lat_min']},{bbox['lat_max']}"
        params["decimalLongitude"] = f"{bbox['lon_min']},{bbox['lon_max']}"

    try:
        all_results: list = []
        offset = 0
        max_records = 1500
        while len(all_results) < max_records:
            page_params = {**params, "offset": offset}
            resp = await client.get(
                "https://api.gbif.org/v1/occurrence/search",
                params=page_params,
                timeout=25,
            )
            resp.raise_for_status()
            data = resp.json()
            page_results = data.get("results", [])
            all_results.extend(page_results)
            if data.get("endOfRecords", True) or not page_results:
                break
            offset += len(page_results)
        results = all_results

        if len(results) < 5:
            print(f"[scout:gbif] only {len(results)} occurrences, skipping")
            return None

        points: list[dict] = []
        years_seen: set = set()
        countries_seen: set = set()

        for rec in results:
            lat = rec.get("decimalLatitude")
            lon = rec.get("decimalLongitude")
            if lat is None or lon is None:
                continue
            year    = rec.get("year")
            country = rec.get("countryCode", "")
            species = rec.get("species") or rec.get("genericName", "")
            points.append({"lat": lat, "lon": lon, "species": species,
                           "year": year, "country": country})
            if year:
                years_seen.add(year)
            if country:
                countries_seen.add(country)

        if not points:
            return None

        years_list = sorted(y for y in years_seen if y)
        print(f"[scout:gbif] {len(points)} occurrences, {len(countries_seen)} countries")
        return {
            "source":    "gbif",
            "type":      "points",
            "points":    points,
            "taxon":     taxon_name or context.get("thematic_scope", ""),
            "n_points":  len(points),
            "year_range": [min(years_list), max(years_list)] if years_list else [],
            "countries": list(countries_seen),
        }
    except Exception as e:
        print(f"[scout:gbif] fetch failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Step 2b — Fetch Our World in Data (CO2 / GHG / land-use emissions)
# ---------------------------------------------------------------------------

OWID_CSV_URL = "https://raw.githubusercontent.com/owid/co2-data/master/owid-co2-data.csv"

async def fetch_owid(context: dict, client: httpx.AsyncClient) -> Optional[dict]:
    """
    Fetch CO2 / GHG / land-use-change time series from Our World in Data.
    Columns chosen based on thematic_scope (OWID_COLUMNS_BY_SCOPE).
    Returns {"source": "owid", "type": "time_series", "series": {...}} or None.
    """
    countries      = context.get("countries", [])
    year_start     = context.get("year_start", 1990)
    year_end       = context.get("year_end", CURRENT_YEAR)
    thematic_scope = context.get("thematic_scope", "_default")

    if thematic_scope not in OWID_COLUMNS_BY_SCOPE:
        print(f"[scout:owid] skipped — no relevant OWID columns for scope={thematic_scope}")
        return None
    cols = OWID_COLUMNS_BY_SCOPE[thematic_scope]
    primary_col = cols[0]  # main column for series label

    # ISO3 codes from countries list; fallback to a global view if none
    iso_codes: dict[str, str] = {}  # iso3 → display name
    for c in countries[:8]:
        iso = c.get("iso3", "")
        if iso:
            iso_codes[iso.upper()] = c.get("name", iso)

    try:
        resp = await client.get(OWID_CSV_URL, timeout=20)
        resp.raise_for_status()
        lines = resp.text.splitlines()
    except Exception as e:
        print(f"[scout:owid] download failed: {e}")
        return None

    import csv as _csv
    reader = _csv.DictReader(lines)
    series_out: dict[str, dict] = {}

    for row in reader:
        iso = (row.get("iso_code") or "").upper()
        if not iso or iso.startswith("OWID"):  # skip aggregates
            continue
        # filter to requested countries (or accept all if no countries specified)
        if iso_codes and iso not in iso_codes:
            continue
        try:
            yr = int(row.get("year", 0))
        except (ValueError, TypeError):
            continue
        if yr < year_start or yr > year_end:
            continue

        # pick best available column
        val = None
        used_col = primary_col
        for col in cols:
            raw = row.get(col, "")
            if raw:
                try:
                    val = float(raw)
                    used_col = col
                    break
                except (ValueError, TypeError):
                    continue
        if val is None:
            continue

        label = iso_codes.get(iso, iso)
        series_out.setdefault(label, {"x": [], "y": []})
        series_out[label]["x"].append(yr)
        series_out[label]["y"].append(val)

    if not series_out:
        print("[scout:owid] no data retrieved")
        return None

    # sort each series by year
    for s in series_out.values():
        pairs = sorted(zip(s["x"], s["y"]))
        s["x"] = [p[0] for p in pairs]
        s["y"] = [p[1] for p in pairs]

    col_label, col_unit = OWID_COLUMN_LABELS.get(primary_col, (primary_col, ""))
    total_points = sum(len(v["x"]) for v in series_out.values())
    print(f"[scout:owid] {len(series_out)} countries, {total_points} points — col={primary_col}")
    return {
        "source":   "owid",
        "type":     "time_series",
        "series":   series_out,
        "label":    col_label,
        "unit":     col_unit,
        "column":   primary_col,
        "n_points": total_points,
        "countries": list(series_out.keys()),
    }


# ---------------------------------------------------------------------------
# Step 2c — World Bank (reuses chart-skill's fetch_worldbank_series)
# ---------------------------------------------------------------------------

async def fetch_worldbank_for_scout(context: dict, client: httpx.AsyncClient) -> list[dict]:
    countries = context.get("countries", [])
    if not countries:
        return []

    iso_codes = [c["iso3"] for c in countries if c.get("iso3")]
    if not iso_codes:
        iso_codes = [c["iso2"] for c in countries if c.get("iso2")]
    if not iso_codes:
        return []

    indicators = context.get("worldbank_indicators", [])
    year_start = context.get("year_start", 1990)
    year_end   = context.get("year_end", CURRENT_YEAR)

    datasets: list[dict] = []
    for indicator in indicators:
        ds = await fetch_worldbank_series(iso_codes, indicator, year_start, year_end, client)
        if ds:
            datasets.append(ds)
        await asyncio.sleep(0.3)

    print(f"[scout:worldbank] {len(datasets)}/{len(indicators)} indicators fetched")
    return datasets


# ---------------------------------------------------------------------------
# Step 2 — Dispatch all sources in parallel
# ---------------------------------------------------------------------------

async def fetch_all_sources(context: dict) -> dict[str, any]:
    sources_to_query = context.get("sources_to_query", ["worldbank"])
    results: dict[str, any] = {}

    async with httpx.AsyncClient(timeout=25) as http:
        tasks: dict[str, asyncio.Task] = {}

        if "gbif" in sources_to_query:
            tasks["gbif"] = asyncio.create_task(fetch_gbif_occurrences(context, http))
        if "owid" in sources_to_query:
            tasks["owid"] = asyncio.create_task(fetch_owid(context, http))
        if "worldbank" in sources_to_query:
            tasks["worldbank"] = asyncio.create_task(fetch_worldbank_for_scout(context, http))

        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for key, val in zip(tasks.keys(), gathered):
            results[key] = None if isinstance(val, Exception) else val

    # NASA uses its own session internally
    if "nasa" in sources_to_query and context.get("is_climate_topic"):
        lat, lon = context.get("lat"), context.get("lon")
        if lat is not None and lon is not None:
            results["nasa"] = await fetch_nasa_power(
                lat, lon,
                context.get("year_start", 1990),
                context.get("year_end", CURRENT_YEAR),
            )

    return results


# ---------------------------------------------------------------------------
# Step 2d — Check visual potential of each dataset
# ---------------------------------------------------------------------------

def check_visual_potential(name: str, dataset: any) -> dict:
    """
    Returns {"has_variation": bool, "note": str}.
    LOW = flat series / uniform choropleth / tightly clustered points — not worth visualizing.
    """
    if dataset is None:
        return {"has_variation": False, "note": "no data"}

    # World Bank: list of indicator dicts, each with a "series" dict {country: {year: value}}
    if name == "worldbank" and isinstance(dataset, list) and dataset:
        all_vals: list[float] = []
        for ds in dataset:
            for pts in ds.get("series", {}).values():
                all_vals.extend(float(v) for v in pts.values() if v is not None)
        if not all_vals:
            return {"has_variation": False, "note": "empty series"}
        mean_val = sum(all_vals) / len(all_vals)
        variation = (max(all_vals) - min(all_vals)) / abs(mean_val) if mean_val else 0
        if variation < 0.05:
            return {"has_variation": False, "note": f"flat series (var={variation:.1%})"}
        return {"has_variation": True, "note": f"range {min(all_vals):.1f}–{max(all_vals):.1f}"}

    # OWID: {"series": {country: {"x": [...], "y": [...]}}}
    elif name == "owid" and isinstance(dataset, dict):
        all_vals = []
        for s in dataset.get("series", {}).values():
            all_vals.extend(float(v) for v in s.get("y", []) if v is not None)
        if not all_vals:
            return {"has_variation": False, "note": "empty series"}
        mean_val = sum(all_vals) / len(all_vals)
        variation = (max(all_vals) - min(all_vals)) / abs(mean_val) if mean_val else 0
        if variation < 0.05:
            return {"has_variation": False, "note": f"flat series (var={variation:.1%})"}
        return {"has_variation": True, "note": f"range {min(all_vals):.1f}–{max(all_vals):.1f}"}

    # GBIF: {"points": [{lat, lon, ...}]}
    elif name == "gbif" and isinstance(dataset, dict):
        points = dataset.get("points", [])
        if len(points) < 5:
            return {"has_variation": False, "note": "too few points"}
        lats = [p["lat"] for p in points if p.get("lat") is not None]
        lons = [p["lon"] for p in points if p.get("lon") is not None]
        if not lats:
            return {"has_variation": False, "note": "no coordinates"}
        lat_span = max(lats) - min(lats)
        lon_span = max(lons) - min(lons)
        if lat_span < 2 and lon_span < 2:
            return {"has_variation": False, "note": f"clustered in {lat_span:.1f}°×{lon_span:.1f}°"}
        return {"has_variation": True, "note": f"{len(points)} pts, span={lat_span:.0f}°×{lon_span:.0f}°"}

    # NASA / Open-Meteo: {"T2M": {year: value}, "PRECTOTCORR": {year: value}}
    elif name == "nasa" and isinstance(dataset, dict):
        all_vals = [float(v) for pts in dataset.values() for v in pts.values() if v is not None]
        if not all_vals:
            return {"has_variation": False, "note": "empty"}
        mean_val = sum(all_vals) / len(all_vals)
        variation = (max(all_vals) - min(all_vals)) / abs(mean_val) if mean_val else 0
        if variation < 0.02:
            return {"has_variation": False, "note": f"flat (var={variation:.1%})"}
        return {"has_variation": True, "note": f"{len(all_vals)} annual values"}

    return {"has_variation": True, "note": "unknown format"}


def check_all_visual_potential(raw: dict[str, any]) -> dict[str, dict]:
    """Check visual potential for every available dataset."""
    return {
        name: check_visual_potential(name, dataset)
        for name, dataset in raw.items()
        if dataset is not None
    }


# ---------------------------------------------------------------------------
# Step 3 — Score datasets by richness (0–10)
# ---------------------------------------------------------------------------

def score_dataset(name: str, dataset: any, thematic_scope: str = "") -> Optional[dict]:
    if dataset is None:
        return None

    n_pts, temporal, geo_spread, data_type = 0, 0, 1, "unknown"

    if isinstance(dataset, dict) and dataset.get("source") == "gbif":
        points    = dataset.get("points", [])
        n_pts     = len(points)
        yr        = dataset.get("year_range", [])
        temporal  = (max(yr) - min(yr)) if len(yr) == 2 else 0
        geo_spread = len({p.get("country", "") for p in points if p.get("country")})
        data_type = "points"

    elif isinstance(dataset, dict) and dataset.get("source") == "owid":
        n_pts      = dataset.get("n_points", 0)
        all_years  = [yr for s in dataset.get("series", {}).values() for yr in s.get("x", [])]
        temporal   = (max(all_years) - min(all_years)) if all_years else 0
        geo_spread = len(dataset.get("series", {}))
        data_type  = "time_series"

    elif isinstance(dataset, list) and dataset:
        all_years: list[int] = []
        countries: set[str] = set()
        for ds in dataset:
            for country, pts in ds.get("series", {}).items():
                n_pts += len(pts)
                all_years.extend(pts.keys())
                countries.add(country)
        temporal   = (max(all_years) - min(all_years)) if all_years else 0
        geo_spread = len(countries)
        data_type  = "time_series"

    elif isinstance(dataset, dict) and any(k in dataset for k in ("T2M", "PRECTOTCORR")):
        all_years = [yr for pts in dataset.values() for yr in pts.keys()]
        n_pts     = len(all_years)
        temporal  = (max(all_years) - min(all_years)) if all_years else 0
        data_type = "time_series"

    else:
        return None

    richness = round(
        min(n_pts / 50, 1.0)       * 3.0 +
        min(temporal / 30, 1.0)    * 2.5 +
        min(geo_spread / 10, 1.0)  * 2.5 +
        1.0                        * 2.0,  # completeness always 1 (filtered at fetch)
        2,
    )

    # Apply thematic relevance multiplier (scope-specific, default=1.0 = no penalty)
    relevance = RELEVANCE_BY_SCOPE.get(thematic_scope, {}).get(name, 1.0)
    final_score = round(richness * relevance, 2)

    return {
        "source":            name,
        "n_points":          n_pts,
        "temporal_range":    temporal,
        "geographic_spread": geo_spread,
        "richness_score":    final_score,
        "data_type":         data_type,
    }


def score_all_datasets(raw: dict[str, any], thematic_scope: str = "") -> list[dict]:
    scored = [score_dataset(k, v, thematic_scope) for k, v in raw.items()]
    return sorted((s for s in scored if s), key=lambda x: x["richness_score"], reverse=True)


# ---------------------------------------------------------------------------
# Step 4 — Claude plans the best visualizations
# ---------------------------------------------------------------------------

SCOUT_PLAN_PROMPT = """Tu es un expert en visualisation de données pour la recherche géographique académique.

Sujet de recherche: {topic}
Portée géographique: {geographic_scope}

Jeux de données disponibles (triés par richesse décroissante):
{datasets_summary}

Choisis au maximum:
- 1 ou 2 types de graphiques (chart obligatoire, chart2 optionnel si une DEUXIÈME source de données distincte et complémentaire est disponible)
- 1 type de carte (map)

Types de graphiques:
- line_chart    : série(s) temporelle(s) ≥5 points
- bar_chart     : comparaison catégorielle ≤20 catégories
- grouped_bar   : multi-pays/multi-indicateurs côte à côte
- scatter       : corrélation entre 2 variables numériques
- histogram     : distribution d'une variable

Types de cartes:
- point_map  : points lat/lon sur une carte (≥5 points)
- heatmap    : densité de clusters lat/lon (≥20 points)
- choropleth : valeurs numériques par pays (≥3 pays)

Règles:
- Préférer les jeux de données avec richness_score élevé
- chart et chart2 doivent utiliser des sources DIFFÉRENTES (ex: owid + worldbank)
- chart2 n'est pertinent que si la source complémentaire apporte une perspective différente (ex: CO2 émis par déforestation [owid] vs superficie forestière [worldbank])
- Ne pas attribuer "point_map" ET "heatmap" à des sources différentes
- Si données insuffisantes pour ce type → mettre null
- Si un dataset a visual_potential=LOW → éviter de l'utiliser pour la visualisation principale; le mentionner seulement si aucune autre source n'est disponible
- PRIORITÉ CARTE: si worldbank a geographic_spread ≥ 3 pays ET les indicateurs WB sont thématiquement pertinents → PRÉFÉRER choropleth à heatmap/point_map
- Pour choropleth worldbank: source = "worldbank", viz_type = "choropleth"

RÈGLE CRITIQUE — PERTINENCE THÉMATIQUE DE LA CARTE (s'applique UNIQUEMENT au choropleth worldbank):
Les indicateurs WB disponibles sont listés dans le résumé ci-dessus (champ "indicateurs:").
Avant de choisir viz_type="choropleth" avec source="worldbank", vérifie: l'indicateur WB mesure-t-il DIRECTEMENT le phénomène du sujet?
- "Superficie forestière" pour déforestation → OUI → choropleth ✓
- "Émissions CO₂" pour émissions/climat → OUI → choropleth ✓
- "Population urbaine" pour urbanisation → OUI → choropleth ✓
- "Espérance de vie" pour santé → OUI → choropleth ✓
- "PIB par habitant" pour socioéconomique → OUI → choropleth ✓
- "Indice de Gini" pour inégalités → OUI → choropleth ✓
- "Terres agricoles (%)" pour sécurité alimentaire → OUI → choropleth ✓
- "Aires protégées (%)" pour biodiversité → OUI → choropleth ✓
- "PIB par habitant" pour déforestation → NON → map: null ✗
- "Population totale" pour climat → NON → map: null ✗
Si GBIF est disponible: les données GBIF (occurrences d'espèces réelles) sont TOUJOURS pertinentes pour point_map ou heatmap — utilise-les en priorité pour la carte si disponibles.

Retourne UNIQUEMENT du JSON valide:
{{
  "chart": {{
    "source": "owid",
    "viz_type": "line_chart",
    "title": "CO₂ émis par déforestation — pays amazoniens (1990–2022)",
    "x_label": "Année",
    "y_label": "Mt CO₂",
    "source_credit": "Our World in Data (2024)"
  }},
  "chart2": {{
    "source": "worldbank",
    "viz_type": "line_chart",
    "title": "Émissions de CO₂ liées à la déforestation — Brésil (1990–2022)",
    "x_label": "Année",
    "y_label": "kt CO₂",
    "source_credit": "World Bank (2024)"
  }},
  "map": {{
    "source": "gbif",
    "viz_type": "heatmap",
    "title": "Densité d'occurrences du jaguar — bassin amazonien",
    "source_credit": "GBIF (2024)"
  }}
}}

Si chart2 ou map non pertinents ou données insuffisantes, mets null pour cette clé.
"""


async def claude_plan_visualizations(
    topic: str,
    context: dict,
    scored: list[dict],
    raw: dict,
    client: anthropic.AsyncAnthropic,
    visual_potential: Optional[dict] = None,
) -> dict:
    if not scored:
        return {"chart": None, "map": None}

    if visual_potential is None:
        visual_potential = {}

    # Build indicator/content labels so Claude can judge thematic relevance
    wb_labels: list[str] = []
    if isinstance(raw.get("worldbank"), list):
        wb_labels = [ds.get("label", ds.get("indicator", "")) for ds in raw["worldbank"] if ds]
    gbif_taxon: str = raw.get("gbif", {}).get("taxon", "") if isinstance(raw.get("gbif"), dict) else ""

    def _extra(s: dict) -> str:
        parts = []
        if s["source"] == "worldbank" and wb_labels:
            parts.append(f"indicateurs: {', '.join(wb_labels)}")
        if s["source"] == "gbif" and gbif_taxon:
            parts.append(f"taxon: {gbif_taxon}")
        vp = visual_potential.get(s["source"])
        if vp:
            flag = "OK" if vp["has_variation"] else "LOW"
            parts.append(f"visual_potential={flag} ({vp['note']})")
        return (" | " + " | ".join(parts)) if parts else ""

    datasets_summary = "\n".join(
        f"[{s['source']}] score={s['richness_score']}/10 | type={s['data_type']} | "
        f"n_points={s['n_points']} | temporal_range={s['temporal_range']}y | "
        f"geo_spread={s['geographic_spread']}{_extra(s)}"
        for s in scored
    )

    prompt = SCOUT_PLAN_PROMPT.format(
        topic=topic,
        geographic_scope=context.get("geographic_scope", "regional"),
        datasets_summary=datasets_summary,
    )

    for attempt in range(3):
        try:
            msg = await client.messages.create(
                model=CLAUDE_SMART,
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = msg.content[0].text.strip()
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
            raw_text = re.sub(r"\s*```$", "", raw_text)
            plan = json.loads(raw_text)
            chart_type = plan.get("chart", {}).get("viz_type") if plan.get("chart") else None
            map_type   = plan.get("map", {}).get("viz_type")   if plan.get("map")   else None
            print(f"[scout:plan] chart={chart_type}, map={map_type}")
            return plan
        except json.JSONDecodeError as e:
            if attempt < 2:
                print(f"[scout:plan] JSON error attempt {attempt+1}, retrying: {e}")
            else:
                print(f"[scout:plan] JSON parse failed: {e}")

    return {"chart": None, "map": None}


# ---------------------------------------------------------------------------
# Step 5a — Convert datasets to chart-skill plan format
# ---------------------------------------------------------------------------

def _dataset_to_chart_plan(viz_entry: dict, dataset: any) -> Optional[dict]:
    """Convert a viz plan entry + raw dataset → chart-skill render_chart() plan dict."""
    viz_type    = viz_entry.get("viz_type", "line_chart")
    title       = viz_entry.get("title", "")
    x_label     = viz_entry.get("x_label", "")
    y_label     = viz_entry.get("y_label", "")
    source      = viz_entry.get("source_credit", "")
    source_name = viz_entry.get("source", "")

    series: list[dict] = []

    if source_name == "owid" and isinstance(dataset, dict):
        for country_name, pts in dataset.get("series", {}).items():
            xs = [str(x) for x in pts.get("x", [])]
            ys = pts.get("y", [])
            if xs and ys:
                series.append({"label": country_name, "x": xs, "y": ys})
        if not x_label:
            x_label = "Année"
        if not y_label:
            y_label = dataset.get("label", "Valeur")

    elif source_name == "worldbank" and isinstance(dataset, list):
        # Use only the FIRST indicator to keep a consistent y-axis unit.
        # Multiple indicators have incompatible units (%, USD, kt) — never mix them.
        if dataset:
            ds = dataset[0]
            all_series = ds.get("series", {})
            # Limit to 8 representative countries for readable charts
            if len(all_series) > 8:
                ranked = sorted(all_series.items(),
                                key=lambda kv: max(kv[1].values()) if kv[1] else 0)
                pick = {}
                pick[ranked[0][0]] = ranked[0][1]
                pick[ranked[1][0]] = ranked[1][1]
                pick[ranked[-1][0]] = ranked[-1][1]
                pick[ranked[-2][0]] = ranked[-2][1]
                remaining = 4
                step = max(1, (len(ranked) - 4) // (remaining + 1))
                for i in range(1, remaining + 1):
                    idx = 2 + i * step
                    if idx < len(ranked) - 2:
                        pick[ranked[idx][0]] = ranked[idx][1]
                all_series = pick
                print(f"[scout:chart] {len(ds['series'])} countries → {len(all_series)} representative for chart")
            for country, pts in all_series.items():
                sorted_pts = sorted(pts.items())
                xs = [str(y) for y, _ in sorted_pts]
                ys = [v for _, v in sorted_pts]
                series.append({"label": country, "x": xs, "y": ys})
            if not x_label:
                x_label = "Année"
            if not y_label:
                y_label = ds.get("label", "")

    elif source_name == "nasa" and isinstance(dataset, dict):
        nasa_labels = {"T2M": "Température (°C)", "PRECTOTCORR": "Précipitations (mm)"}
        for param, pts in dataset.items():
            sorted_pts = sorted(pts.items())
            xs = [str(y) for y, _ in sorted_pts]
            ys = [v for _, v in sorted_pts]
            series.append({"label": nasa_labels.get(param, param), "x": xs, "y": ys})
        if not x_label:
            x_label = "Année"

    # ── GBIF → histogram/bar_chart by year ─────────────────────────────────
    elif source_name == "gbif" and isinstance(dataset, dict):
        points = dataset.get("points", [])
        # Count occurrences by year
        year_counts: dict[int, int] = {}
        for pt in points:
            yr = pt.get("year")
            if yr:
                year_counts[yr] = year_counts.get(yr, 0) + 1
        if year_counts:
            sorted_years = sorted(year_counts.items())
            xs = [str(y) for y, _ in sorted_years]
            ys = [c for _, c in sorted_years]
            series.append({"label": dataset.get("taxon", "Occurrences"), "x": xs, "y": ys})
            if not x_label:
                x_label = "Année"
            if not y_label:
                y_label = "Nombre d'occurrences"
            # Override viz_type to bar_chart if histogram was requested (more readable)
            if viz_type == "histogram":
                viz_type = "bar_chart"

    if not series:
        return None

    return {
        "type":    viz_type,
        "title":   title,
        "x_label": x_label,
        "y_label": y_label,
        "source":  source,
        "series":  series,
    }


# ---------------------------------------------------------------------------
# Step 5b — Convert point dataset to map locations
# ---------------------------------------------------------------------------

def _dataset_to_map_locations(viz_entry: dict, dataset: any) -> list[dict]:
    """Convert a GBIF point dataset → [{lat, lon, name, feature_type}]."""
    source_name = viz_entry.get("source", "")
    locations: list[dict] = []

    if source_name == "gbif" and isinstance(dataset, dict):
        taxon = dataset.get("taxon", "")
        for pt in dataset.get("points", []):
            lat = pt.get("lat")
            lon = pt.get("lon")
            if lat is None or lon is None:
                continue
            name = pt.get("species") or taxon or "Occurrence"
            locations.append({
                "lat": lat, "lon": lon,
                "name": name,
                "feature_type": "hotspot",
            })

    return locations


# ---------------------------------------------------------------------------
# Step 5c — Render point map from pre-geocoded coordinates (no Nominatim)
# ---------------------------------------------------------------------------

async def render_points_from_coords(
    locations: list[dict],
    title: str,
    bbox: Optional[dict],
    output_dir: Path,
    slug: str,
    formats: list[str],
) -> dict:
    """Render Folium point map directly from lat/lon (bypasses Nominatim geocoding)."""
    if not locations:
        return {}

    output_paths: dict = {}
    lats = [loc["lat"] for loc in locations]
    lons = [loc["lon"] for loc in locations]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    if "html" in formats:
        try:
            import folium

            lat_span = (bbox["lat_max"] - bbox["lat_min"]) if bbox else 40
            zoom = 3 if lat_span > 40 else 5 if lat_span > 15 else 7
            m = folium.Map(location=[center_lat, center_lon], zoom_start=zoom,
                           tiles="CartoDB positron")
            if bbox:
                m.fit_bounds([[bbox["lat_min"], bbox["lon_min"]],
                              [bbox["lat_max"], bbox["lon_max"]]])

            for loc in locations[:500]:
                folium.CircleMarker(
                    location=[loc["lat"], loc["lon"]],
                    radius=4,
                    color=PALETTE["accent"],
                    fill=True,
                    fill_color=PALETTE["accent"],
                    fill_opacity=0.55,
                    weight=0.5,
                    popup=folium.Popup(loc.get("name", "")[:60], max_width=200),
                ).add_to(m)

            title_html = (
                f'<div style="position:fixed;top:12px;left:50%;transform:translateX(-50%);'
                f'background:white;padding:8px 18px;border-radius:4px;'
                f'box-shadow:0 2px 8px rgba(0,0,0,0.2);font-size:13px;font-weight:bold;'
                f'font-family:Arial;z-index:1000;color:{PALETTE["primary"]};">'
                f'{title[:70]}</div>'
            )
            m.get_root().html.add_child(folium.Element(title_html))

            html_path = output_dir / f"{slug}-map.html"
            m.save(str(html_path))
            output_paths["html"] = str(html_path)
            print(f"[scout:render] saved HTML (point_map) → {html_path}")
        except Exception as e:
            print(f"[scout:render] folium point map failed: {e}")

    if "png" in formats:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import geopandas as gpd
            from shapely.geometry import Point
            import contextily as ctx

            apply_pub_style()
            geometry = [Point(lon, lat) for lat, lon in zip(lats, lons)]
            gdf = gpd.GeoDataFrame(geometry=geometry, crs="EPSG:4326").to_crs("EPSG:3857")
            fig, ax = plt.subplots(figsize=(12, 9))
            gdf.plot(ax=ax, color=PALETTE["accent"], markersize=8, alpha=0.55, zorder=5)

            if bbox:
                try:
                    from pyproj import Transformer
                    t = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
                    xmin, ymin = t.transform(bbox["lon_min"], bbox["lat_min"])
                    xmax, ymax = t.transform(bbox["lon_max"], bbox["lat_max"])
                    mx = (xmax - xmin) * 0.05
                    my = (ymax - ymin) * 0.05
                    ax.set_xlim(xmin - mx, xmax + mx)
                    ax.set_ylim(ymin - my, ymax + my)
                except Exception:
                    pass

            ctx.add_basemap(ax, source=ctx.providers.CartoDB.Positron, zoom="auto")
            ax.set_axis_off()
            ax.set_title(title, fontsize=12, fontweight="bold",
                         color=PALETTE["primary"], pad=10)
            png_path = output_dir / f"{slug}-map.png"
            plt.savefig(png_path, dpi=300, facecolor="white", bbox_inches="tight")
            plt.close()
            output_paths["png"] = str(png_path)
            print(f"[scout:render] saved PNG (point_map/contextily) → {png_path}")

        except Exception as e:
            print(f"[scout:render] contextily PNG failed ({e}), using matplotlib fallback")
            try:
                import matplotlib
                matplotlib.use("Agg")
                import matplotlib.pyplot as plt
                apply_pub_style()
                fig, ax = plt.subplots(figsize=(12, 9))
                ax.scatter(lons, lats, color=PALETTE["accent"], s=15, alpha=0.55, zorder=5)
                ax.set_xlabel("Longitude")
                ax.set_ylabel("Latitude")
                ax.set_title(title, fontsize=12, fontweight="bold", color=PALETTE["primary"])
                plt.tight_layout()
                png_path = output_dir / f"{slug}-map.png"
                plt.savefig(png_path, dpi=300, facecolor="white")
                plt.close()
                output_paths["png"] = str(png_path)
            except Exception as e2:
                print(f"[scout:render] matplotlib fallback failed: {e2}")

    return output_paths


async def render_heatmap_from_coords(
    locations: list[dict],
    title: str,
    bbox: Optional[dict],
    output_dir: Path,
    slug: str,
    formats: list[str],
) -> dict:
    """Render Folium HeatMap + KDE density PNG from pre-geocoded coordinates."""
    if len(locations) < 3:
        return {}

    output_paths: dict = {}
    lats = [loc["lat"] for loc in locations]
    lons = [loc["lon"] for loc in locations]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)
    heat_data  = [[lat, lon] for lat, lon in zip(lats, lons)]

    if "html" in formats:
        try:
            import folium
            from folium.plugins import HeatMap

            lat_span = (bbox["lat_max"] - bbox["lat_min"]) if bbox else 40
            zoom = 3 if lat_span > 40 else 5
            m = folium.Map(location=[center_lat, center_lon], zoom_start=zoom,
                           tiles="CartoDB positron")
            if bbox:
                m.fit_bounds([[bbox["lat_min"], bbox["lon_min"]],
                              [bbox["lat_max"], bbox["lon_max"]]])

            HeatMap(heat_data, radius=15, blur=12, min_opacity=0.4).add_to(m)
            title_html = (
                f'<div style="position:fixed;top:12px;left:50%;transform:translateX(-50%);'
                f'background:white;padding:8px 18px;border-radius:4px;'
                f'box-shadow:0 2px 8px rgba(0,0,0,0.2);font-size:13px;font-weight:bold;'
                f'font-family:Arial;z-index:1000;color:{PALETTE["primary"]};">'
                f'{title[:70]}</div>'
            )
            m.get_root().html.add_child(folium.Element(title_html))
            html_path = output_dir / f"{slug}-map.html"
            m.save(str(html_path))
            output_paths["html"] = str(html_path)
            print(f"[scout:render] saved HTML (heatmap) → {html_path}")
        except Exception as e:
            print(f"[scout:render] heatmap HTML failed: {e}")

    if "png" in formats:
        pad = max(1.5, (max(lons) - min(lons)) * 0.08)
        if bbox:
            lon_min = bbox["lon_min"]; lon_max = bbox["lon_max"]
            lat_min = bbox["lat_min"]; lat_max = bbox["lat_max"]
        else:
            lon_min = min(lons) - pad; lon_max = max(lons) + pad
            lat_min = min(lats) - pad; lat_max = max(lats) + pad

        png_path = output_dir / f"{slug}-map.png"
        try:
            import cartopy.crs as ccrs
            import cartopy.feature as cfeature
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from scipy.stats import gaussian_kde
            import numpy as np

            apply_pub_style()
            fig, ax = plt.subplots(
                figsize=(14, 10),
                subplot_kw={"projection": ccrs.PlateCarree()},
            )
            ax.set_extent([lon_min, lon_max, lat_min, lat_max], crs=ccrs.PlateCarree())
            ax.add_feature(cfeature.OCEAN.with_scale("50m"),     facecolor="#D6EAF8", zorder=0)
            ax.add_feature(cfeature.LAND.with_scale("50m"),      facecolor="#F0EFE7", zorder=1)
            ax.add_feature(cfeature.COASTLINE.with_scale("50m"), linewidth=0.6, edgecolor="#555555", zorder=2)
            ax.add_feature(cfeature.BORDERS.with_scale("50m"),   linewidth=0.5, edgecolor="#888888", linestyle="--", zorder=2)
            ax.add_feature(cfeature.RIVERS.with_scale("50m"),    linewidth=0.4, edgecolor="#4BBCD6", alpha=0.7, zorder=2)
            ax.add_feature(cfeature.LAKES.with_scale("50m"),     facecolor="#D6EAF8", edgecolor="#4BBCD6", linewidth=0.3, zorder=2)
            gl = ax.gridlines(draw_labels=True, linewidth=0.25, color="#AAAAAA", alpha=0.5, linestyle="--")
            gl.top_labels = False; gl.right_labels = False

            # KDE density layer
            xy  = np.vstack([lons, lats])
            kde = gaussian_kde(xy, bw_method="scott")
            xx, yy = np.mgrid[lon_min:lon_max:300j, lat_min:lat_max:300j]
            z = kde(np.vstack([xx.ravel(), yy.ravel()])).reshape(xx.shape)

            im = ax.pcolormesh(
                xx, yy, z,
                transform=ccrs.PlateCarree(),
                cmap="YlOrRd", alpha=0.72, zorder=3,
            )
            # Mask KDE bleed-over into ocean by re-drawing ocean + lakes on top
            ax.add_feature(cfeature.OCEAN.with_scale("50m"), facecolor="#D6EAF8", zorder=4)
            ax.add_feature(cfeature.LAKES.with_scale("50m"), facecolor="#D6EAF8", edgecolor="#4BBCD6", linewidth=0.3, zorder=4)
            ax.add_feature(cfeature.COASTLINE.with_scale("50m"), linewidth=0.6, edgecolor="#555555", zorder=5)
            ax.scatter(
                lons, lats,
                transform=ccrs.PlateCarree(),
                s=4, c=PALETTE["primary"], alpha=0.25, zorder=6, edgecolors="none",
            )
            cbar = plt.colorbar(im, ax=ax, shrink=0.55, pad=0.03, aspect=20)
            cbar.set_label("Densité d'occurrences", fontsize=9, color=PALETTE["text"])
            cbar.ax.tick_params(labelsize=8)

            ax.set_title(title, fontsize=13, fontweight="bold",
                         color=PALETTE["primary"], pad=14)
            plt.tight_layout()
            plt.savefig(png_path, dpi=300, facecolor="white")
            plt.close()
            output_paths["png"] = str(png_path)
            print(f"[scout:render] saved PNG (heatmap/cartopy) → {png_path}")

        except Exception as e:
            print(f"[scout:render] cartopy heatmap failed ({type(e).__name__}: {e}), contextily fallback")
            try:
                import matplotlib
                matplotlib.use("Agg")
                import matplotlib.pyplot as plt
                from scipy.stats import gaussian_kde
                import numpy as np
                import contextily as ctx
                from pyproj import Transformer

                apply_pub_style()
                t   = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
                xy  = np.vstack([lons, lats])
                kde = gaussian_kde(xy)
                xx_ll, yy_ll = np.mgrid[lon_min:lon_max:200j, lat_min:lat_max:200j]
                z = kde(np.vstack([xx_ll.ravel(), yy_ll.ravel()])).reshape(xx_ll.shape)
                xx_m, yy_m   = t.transform(xx_ll, yy_ll)
                lons_m, lats_m = t.transform(lons, lats)

                fig, ax = plt.subplots(figsize=(12, 9))
                if bbox:
                    x0, y0 = t.transform(lon_min, lat_min)
                    x1, y1 = t.transform(lon_max, lat_max)
                    ax.set_xlim(x0, x1); ax.set_ylim(y0, y1)
                ctx.add_basemap(ax, source=ctx.providers.CartoDB.Positron, zoom="auto")
                ax.contourf(xx_m, yy_m, z, levels=15, cmap="YlOrRd", alpha=0.55, zorder=4)
                ax.scatter(lons_m, lats_m, s=6, c=PALETTE["primary"], alpha=0.25, zorder=5)
                ax.set_axis_off()
                ax.set_title(title, fontsize=12, fontweight="bold",
                             color=PALETTE["primary"], pad=10)
                plt.tight_layout()
                plt.savefig(png_path, dpi=300, facecolor="white")
                plt.close()
                output_paths["png"] = str(png_path)
                print(f"[scout:render] saved PNG (heatmap/contextily) → {png_path}")
            except Exception as e2:
                print(f"[scout:render] heatmap PNG failed: {e2}")

    return output_paths


# ---------------------------------------------------------------------------
# Choropleth helpers
# ---------------------------------------------------------------------------

def _infer_region_label(context: dict) -> str:
    """Derive a human-readable region label from context."""
    _BASIN_LABELS = {
        "amazon": "pays amazoniens",
        "congo":  "bassin du Congo",
        "sahel":  "Sahel",
        "mekong": "bassin du Mékong",
    }
    basin = context.get("basin", "")
    if basin and basin in _BASIN_LABELS:
        return _BASIN_LABELS[basin]

    countries = context.get("countries", [])
    if len(countries) == 1:
        return countries[0].get("name", "")

    bbox = context.get("bbox")
    if bbox:
        lon_mid = (bbox["lon_min"] + bbox["lon_max"]) / 2
        lat_mid = (bbox["lat_min"] + bbox["lat_max"]) / 2
        if -82 <= lon_mid <= -34 and -56 <= lat_mid <= 13:
            return "Amérique du Sud"
        if -18 <= lon_mid <= 51 and -35 <= lat_mid <= 38:
            return "Afrique"
        if -10 <= lon_mid <= 40 and 35 <= lat_mid <= 72:
            return "Europe"
        if 26 <= lon_mid <= 180 and lat_mid > 0:
            return "Asie"
        if -170 <= lon_mid <= -52 and lat_mid >= 7:
            return "Amérique du Nord"

    if 1 < len(countries) <= 3:
        return ", ".join(c.get("name", "") for c in countries)

    return ""


def _compute_choropleth_value(pts: dict) -> tuple[float, str, int, int]:
    """
    Returns (value, mode, first_year, last_year).
    mode = 'change_pct' if ≥10 data points exist (% change from first to last year)
    mode = 'latest'     otherwise (most recent value)
    """
    sorted_pts = sorted(pts.items())
    if len(sorted_pts) >= 10 and sorted_pts[0][1] not in (None, 0):
        first_yr, first_val = sorted_pts[0]
        last_yr,  last_val  = sorted_pts[-1]
        change = round((last_val - first_val) / abs(first_val) * 100, 1)
        return change, "change_pct", int(first_yr), int(last_yr)
    last_yr, last_val = sorted_pts[-1]
    return last_val, "latest", 0, int(last_yr)


# ---------------------------------------------------------------------------
# Step 5 — Orchestrate rendering
# ---------------------------------------------------------------------------

async def render_outputs(
    viz_plan: dict,
    raw: dict,
    topic: str,
    context: dict,
    output_dir: Path,
    formats: list[str],
    slug: str,
) -> dict:
    chart_result: dict = {}
    map_result:   dict = {}
    bbox = context.get("bbox")

    # ── Chart ──────────────────────────────────────────────────────────────
    chart_entry = viz_plan.get("chart")
    if chart_entry:
        source_name = chart_entry.get("source", "")
        dataset     = raw.get(source_name)
        plan        = _dataset_to_chart_plan(chart_entry, dataset)
        if plan:
            path = render_chart(plan, output_dir / f"{slug}-chart.png")
            if path:
                chart_result = {
                    "chart": str(path),
                    "title": chart_entry.get("title", ""),
                    "type":  chart_entry.get("viz_type", ""),
                }

    # ── Chart 2 (optional second chart from a different source) ────────────
    chart2_result: dict = {}
    chart2_entry = viz_plan.get("chart2")
    if chart2_entry:
        source_name2 = chart2_entry.get("source", "")
        dataset2     = raw.get(source_name2)
        plan2        = _dataset_to_chart_plan(chart2_entry, dataset2)
        if plan2:
            path2 = render_chart(plan2, output_dir / f"{slug}-chart2.png")
            if path2:
                chart2_result = {
                    "chart2": str(path2),
                    "title2": chart2_entry.get("title", ""),
                    "type2":  chart2_entry.get("viz_type", ""),
                }

    # ── Map ────────────────────────────────────────────────────────────────
    map_entry = viz_plan.get("map")
    if map_entry:
        source_name = map_entry.get("source", "")
        dataset     = raw.get(source_name)
        viz_type    = map_entry.get("viz_type", "point_map")
        title       = map_entry.get("title", "")

        if viz_type == "choropleth" and isinstance(dataset, list) and dataset:
            # Build choropleth plan from WB data using context for ISO3 mapping
            name_to_iso3 = {c["name"].lower(): c.get("iso3", "")
                            for c in context.get("countries", [])}
            ds = dataset[0]
            countries_data = []
            value_modes: list[str] = []
            first_years:  list[int] = []
            last_years:   list[int] = []
            for country_str, pts in ds.get("series", {}).items():
                iso3 = name_to_iso3.get(country_str.lower(), "")
                if not iso3 or not pts:
                    continue
                val, mode, first_yr, last_yr = _compute_choropleth_value(pts)
                countries_data.append({"iso3": iso3, "name": country_str, "value": val})
                value_modes.append(mode)
                if first_yr:
                    first_years.append(first_yr)
                last_years.append(last_yr)

            if len(countries_data) >= 3:
                # Determine overall mode (change_pct if any country has enough data)
                choropleth_mode = "change_pct" if "change_pct" in value_modes else "latest"
                yr_from = min(first_years) if first_years else (min(last_years) if last_years else 1990)
                yr_to   = max(last_years) if last_years else CURRENT_YEAR

                lbl = ds.get("label", "")

                # Choose colorscale and indicator label based on mode and indicator type
                lbl_lower = lbl.lower()
                if choropleth_mode == "change_pct":
                    # RdYlGn: red=loss/decrease (negative %), green=gain/increase (positive %)
                    # Reversed for emissions (more = worse)
                    if any(w in lbl_lower for w in ("co2", "émission", "pollution", "ghg")):
                        colorscale = "RdYlGn_r"  # red=high emissions, green=low
                    else:
                        colorscale = "RdYlGn"
                    indicator_label = f"Évolution (%) — {yr_from}–{yr_to}"
                    if not title or title == map_entry.get("title", ""):
                        title = f"{lbl} — Évolution {yr_from}–{yr_to}"
                else:
                    if any(w in lbl_lower for w in ("forêt", "forest", "végétat", "couvert")):
                        colorscale = "RdYlGn"
                    elif any(w in lbl_lower for w in ("co2", "émission", "pollution", "ghg")):
                        colorscale = "YlOrRd"
                    elif any(w in lbl_lower for w in ("population", "urbain", "densité")):
                        colorscale = "Blues"
                    else:
                        colorscale = "RdYlBu_r"
                    indicator_label = lbl

                # Plotly scope from basin or bbox
                scope = _BASIN_TO_PLOTLY_SCOPE.get(context.get("basin", ""))
                if not scope and context.get("bbox"):
                    bb = context["bbox"]
                    lon_mid = (bb["lon_min"] + bb["lon_max"]) / 2
                    lat_mid = (bb["lat_min"] + bb["lat_max"]) / 2
                    if -82 <= lon_mid <= -34 and -56 <= lat_mid <= 13:
                        scope = "south america"
                    elif -18 <= lon_mid <= 51 and -35 <= lat_mid <= 38:
                        scope = "africa"
                    elif -10 <= lon_mid <= 40 and 35 <= lat_mid <= 72:
                        scope = "europe"
                    elif 26 <= lon_mid <= 180 and lat_mid > 0:
                        scope = "asia"
                    elif -170 <= lon_mid <= -52 and lat_mid >= 7:
                        scope = "north america"
                    # Oceania: no Plotly scope — falls through to "world" with bbox zoom

                choropleth_plan = {
                    "title":            title,
                    "countries":        countries_data,
                    "indicator_label":  indicator_label,
                    "colorscale":       colorscale,
                    "source":           map_entry.get("source_credit", "Banque mondiale"),
                    "scope":            scope or "world",
                    "bbox":             context.get("bbox"),
                }
                print(f"[scout:choropleth] mode={choropleth_mode}, {len(countries_data)} countries, {yr_from}–{yr_to}")
                paths = render_choropleth(choropleth_plan, output_dir, slug, formats)
                if paths:
                    map_result = {"map": {**paths, "title": title, "type": "choropleth"}}

        elif viz_type in ("point_map", "heatmap") and dataset:
            locations = _dataset_to_map_locations(map_entry, dataset)
            if locations:
                if viz_type == "heatmap" and len(locations) >= 20:
                    paths = await render_heatmap_from_coords(
                        locations, title, bbox, output_dir, slug, formats
                    )
                else:
                    paths = await render_points_from_coords(
                        locations, title, bbox, output_dir, slug, formats
                    )
                if paths:
                    map_result = {"map": {**paths, "title": title, "type": viz_type}}

    return {**chart_result, **chart2_result, **map_result}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_data_scout(
    topic: str,
    output_dir: str = "./output",
    formats: Optional[list[str]] = None,
) -> dict:
    """
    Full pipeline: analyze → fetch → score → plan → render.
    Returns {"chart": str|None, "map": dict|None, "datasets": dict, "sources_queried": list}.
    """
    if formats is None:
        formats = ["png", "html"]

    slug    = slugify(topic) if topic else "research"
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[scout] topic='{topic}'")
    ai_client = anthropic.AsyncAnthropic()

    # Step 1
    print("[scout] Analyzing topic...")
    context = await analyze_topic_for_scout(topic, ai_client)
    context = _expand_basin_countries(context, topic)
    print(
        f"[scout] scope={context.get('thematic_scope')} | "
        f"countries={[c['name'] for c in context.get('countries', [])]} | "
        f"sources={context.get('sources_to_query')}"
    )

    # Step 2
    print("[scout] Fetching data sources...")
    raw = await fetch_all_sources(context)
    sources_queried = [k for k, v in raw.items() if v is not None]
    print(f"[scout] Sources with data: {sources_queried}")

    # Step 2d — visual potential check
    visual_potential = check_all_visual_potential(raw)
    for src, vp in visual_potential.items():
        flag = "OK" if vp["has_variation"] else "LOW"
        print(f"[scout:visual] {src}: {flag} — {vp['note']}")

    # Step 3
    thematic_scope = context.get("thematic_scope", "")
    scored = score_all_datasets(raw, thematic_scope)
    print("[scout] Dataset richness scores:")
    for s in scored:
        print(
            f"  {s['source']:12s}  score={s['richness_score']}/10  "
            f"n={s['n_points']}  range={s['temporal_range']}y  "
            f"geo={s['geographic_spread']}"
        )

    # Step 4
    print("[scout] Planning visualizations...")
    viz_plan = await claude_plan_visualizations(topic, context, scored, raw, ai_client, visual_potential)

    # Post-planning override: if worldbank has ≥3 countries AND filtered indicators exist,
    # force choropleth — Claude often picks heatmap by default even when multi-country WB data is better
    wb_scored = next((d for d in scored if d["source"] == "worldbank"), None)
    wb_indicators = context.get("worldbank_indicators", [])
    if (wb_scored and wb_scored.get("geographic_spread", 0) >= 3
            and len(wb_indicators) > 0
            and viz_plan.get("map") is not None
            and viz_plan["map"].get("viz_type") in ("heatmap", "point_map")):
        wb_data = raw.get("worldbank") or []
        wb_label = wb_data[0].get("label", "Indicateur") if wb_data else "Indicateur"
        region_label = _infer_region_label(context)
        year_start = context.get("year_start", 1990)
        year_end   = context.get("year_end", CURRENT_YEAR)
        if region_label:
            choropleth_title = f"{wb_label} — {region_label} ({year_start}–{year_end})"
        else:
            choropleth_title = f"{wb_label} ({year_start}–{year_end})"
        viz_plan["map"] = {
            "source": "worldbank",
            "viz_type": "choropleth",
            "title": choropleth_title,
            "source_credit": f"Banque mondiale ({year_end})",
        }
        print(f"[scout:plan] override → choropleth (worldbank geo={wb_scored['geographic_spread']} pays)")

    # Step 5
    print("[scout] Rendering...")
    outputs = await render_outputs(
        viz_plan, raw, topic, context, out_dir, formats, slug
    )

    result = {
        "chart":           outputs.get("chart"),
        "chart2":          outputs.get("chart2"),
        "map":             outputs.get("map"),
        "datasets":        {s["source"]: s for s in scored},
        "sources_queried": sources_queried,
        "viz_plan":        viz_plan,
    }

    print(f"\n[scout] Done")
    if result["chart"]:
        print(f"  Chart  → {result['chart']}")
    if result["chart2"]:
        print(f"  Chart2 → {result['chart2']}")
    if result["map"]:
        print(f"  Map    → {result['map']}")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="data-scout-skill — hunt open data, render best chart + map"
    )
    parser.add_argument("--topic",      required=True, help="Research topic")
    parser.add_argument("--output-dir", default="./output")
    parser.add_argument("--formats",    nargs="+", default=["png", "html"],
                        choices=["png", "html"])
    args = parser.parse_args()

    result = asyncio.run(run_data_scout(
        topic=args.topic,
        output_dir=args.output_dir,
        formats=args.formats,
    ))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
