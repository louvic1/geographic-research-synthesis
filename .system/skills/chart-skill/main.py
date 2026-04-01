"""
chart-skill
Generates professional publication-quality data charts for geographic research.

Approach:
  1. analyze_topic()     — Claude Haiku extracts geo context + relevant indicators
  2. fetch_data()        — World Bank API + NASA POWER (optional)
  3. claude_plan_chart() — Claude Sonnet selects best chart type and builds plan
  4. render_chart()      — Plotly (primary), seaborn (statistical), matplotlib (fallback)

Supported chart types:
  line_chart     — time series (one or more series)
  bar_chart      — category comparison (single series)
  grouped_bar    — multi-country/multi-series comparison
  scatter        — correlation between two variables
  histogram      — distribution of a single variable
  box_plot        — spread / outliers by group (seaborn)
  heatmap_matrix — correlation matrix across indicators (seaborn)

Usage:
    python main.py --topic "Urbanisation en Afrique subsaharienne" \\
                   --output-dir figures/
"""

import argparse
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic
import httpx
from dotenv import load_dotenv

load_dotenv()

CLAUDE_FAST  = "claude-haiku-4-5-20251001"
CLAUDE_SMART = "claude-sonnet-4-6"

CURRENT_YEAR = datetime.now().year

# Publication-quality palette (matches PDF report and map-skill)
PALETTE = {
    "primary":   "#1B3A6B",
    "secondary": "#2A9D8F",
    "accent":    "#E63946",
    "neutral":   "#457B9D",
    "orange":    "#F4A261",
    "light":     "#F4F7FB",
    "text":      "#1A1A1A",
    "grid":      "#E8EEF5",
}
SERIES_COLORS = ["#1B3A6B", "#2A9D8F", "#E63946", "#F4A261", "#457B9D", "#6A4C93", "#E9C46A"]


# ---------------------------------------------------------------------------
# World Bank indicator library
# ---------------------------------------------------------------------------

INDICATORS_BY_DOMAIN: dict[str, list[str]] = {
    "urban":          ["SP.URB.TOTL.IN.ZS", "SP.RUR.TOTL.ZS", "EN.URB.MCTY.TL.ZS"],
    "agriculture":    ["AG.LND.AGRI.ZS", "AG.LND.ARBL.ZS", "AG.LND.FRST.ZS"],
    "forest":         ["AG.LND.FRST.ZS", "AG.LND.AGRI.ZS", "ER.LND.PTLD.ZS"],
    "deforestation":  ["AG.LND.FRST.ZS", "AG.LND.FRST.K2", "ER.LND.PTLD.ZS", "EN.ATM.CO2E.KT"],
    "climate":        ["EN.ATM.CO2E.PC", "EG.USE.PCAP.KG.OE", "EN.ATM.GHGT.KT.CE"],
    "population":     ["SP.POP.TOTL", "SP.URB.TOTL.IN.ZS", "SP.POP.GROW"],
    "economics":      ["NY.GDP.MKTP.CD", "NY.GDP.PCAP.CD", "SI.POV.GINI"],
    "environment":    ["AG.LND.FRST.ZS", "ER.LND.PTLD.ZS", "EN.ATM.CO2E.PC"],
    "health":         ["SP.DYN.LE00.IN", "SH.XPD.CHEX.GD.ZS", "SP.DYN.IMRT.IN"],
}

INDICATOR_LABELS: dict[str, str] = {
    "SP.URB.TOTL.IN.ZS": "Population urbaine (% du total)",
    "SP.RUR.TOTL.ZS":    "Population rurale (% du total)",
    "EN.URB.MCTY.TL.ZS": "Pop. agglomérations > 1M (%)",
    "AG.LND.AGRI.ZS":    "Terres agricoles (% superficie terrestre)",
    "AG.LND.ARBL.ZS":    "Terres arables (% superficie terrestre)",
    "AG.LND.FRST.ZS":    "Superficie forestière (% superficie terrestre)",
    "ER.LND.PTLD.ZS":    "Aires protégées terrestres (%)",
    "EN.ATM.CO2E.PC":    "Émissions CO₂ (t/hab.)",
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
    "AG.LND.FRST.K2":    "Superficie forestière (km²)",
    "EN.ATM.CO2E.KT":    "Émissions CO₂ totales (kt)",
}

DATA_LABELS = {
    "T2M":         "Température moyenne annuelle (°C)",
    "PRECTOTCORR": "Précipitations annuelles (mm)",
}


# ---------------------------------------------------------------------------
# matplotlib publication style
# ---------------------------------------------------------------------------

def apply_pub_style() -> None:
    try:
        import matplotlib as mpl
        mpl.rcParams.update({
            "font.family":       "sans-serif",
            "font.sans-serif":   ["Arial", "Helvetica", "DejaVu Sans"],
            "font.size":         10,
            "axes.titlesize":    13,
            "axes.labelsize":    11,
            "axes.spines.top":   False,
            "axes.spines.right": False,
            "axes.grid":         True,
            "grid.color":        PALETTE["grid"],
            "grid.linewidth":    0.8,
            "figure.facecolor":  "white",
            "axes.facecolor":    "white",
            "axes.linewidth":    0.8,
            "axes.edgecolor":    "#CCCCCC",
            "xtick.color":       "#666666",
            "ytick.color":       "#666666",
            "text.color":        PALETTE["text"],
            "savefig.dpi":       300,
            "savefig.bbox":      "tight",
            "savefig.facecolor": "white",
        })
    except ImportError:
        pass


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:50]


def normalize_input(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "sources" in data:
        return data["sources"]
    if isinstance(data, dict):
        return [data]
    return []


# ---------------------------------------------------------------------------
# Step 1 — Analyze topic
# ---------------------------------------------------------------------------

ANALYZE_PROMPT = """Analyse ce sujet de recherche géographique et extrais le contexte géographique et les besoins en données.

Sujet: {topic}

Retourne UNIQUEMENT du JSON valide avec cette structure exacte:
{{
  "countries": [
    {{"name": "Canada", "iso2": "CA", "iso3": "CAN"}}
  ],
  "compare_countries": false,
  "region_name": "Montréal, Québec",
  "lat": 45.5017,
  "lon": -73.5673,
  "domains": ["urban", "agriculture", "forest"],
  "worldbank_indicators": ["SP.URB.TOTL.IN.ZS", "AG.LND.FRST.ZS", "AG.LND.AGRI.ZS"],
  "is_climate_topic": false,
  "year_start": 1990,
  "year_end": {current_year}
}}

Règles:
- "compare_countries": true seulement si le sujet compare explicitement plusieurs pays
- "domains": liste parmi [urban, agriculture, forest, deforestation, climate, population, economics, environment, health]
- "worldbank_indicators": choisir 2-4 indicateurs DIRECTEMENT pertinents (voir liste ci-dessous)
- "is_climate_topic": true si températures, précipitations, événements météo
- lat/lon: coordonnées du lieu principal
- Si plusieurs pays sans comparaison explicite: prendre le pays le plus représentatif

Indicateurs World Bank disponibles:
{indicators_list}
"""


async def analyze_topic(topic: str, client: anthropic.AsyncAnthropic) -> dict:
    indicators_list = "\n".join(f"  {k}: {v}" for k, v in INDICATOR_LABELS.items())
    prompt = ANALYZE_PROMPT.format(
        topic=topic,
        current_year=CURRENT_YEAR,
        indicators_list=indicators_list,
    )
    try:
        msg = await client.messages.create(
            model=CLAUDE_FAST,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        print(f"[chart:analyze] failed: {e}")
        return {
            "countries": [],
            "compare_countries": False,
            "region_name": topic,
            "lat": None, "lon": None,
            "domains": ["urban"],
            "worldbank_indicators": ["SP.URB.TOTL.IN.ZS"],
            "is_climate_topic": False,
            "year_start": 1990,
            "year_end": CURRENT_YEAR,
        }


# ---------------------------------------------------------------------------
# Step 2a — Fetch World Bank data
# ---------------------------------------------------------------------------

async def fetch_worldbank_series(
    iso_codes: list[str],
    indicator: str,
    year_start: int,
    year_end: int,
    client: httpx.AsyncClient,
) -> Optional[dict]:
    iso_str = ";".join(iso_codes)
    base_url = (
        f"https://api.worldbank.org/v2/country/{iso_str}/indicator/{indicator}"
        f"?format=json&per_page=1000&date={year_start}:{year_end}"
    )
    try:
        all_points: list[dict] = []
        page = 1
        while True:
            url = f"{base_url}&page={page}"
            resp = await client.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list) or len(data) < 2 or not data[1]:
                break
            all_points.extend(data[1])
            total_pages = data[0].get("pages", 1)
            if page >= total_pages:
                break
            page += 1

        if not all_points:
            return None

        series: dict[str, dict[int, float]] = {}
        for point in all_points:
            if point.get("value") is None:
                continue
            country = point["country"]["value"]
            year = int(point["date"])
            if country not in series:
                series[country] = {}
            series[country][year] = float(point["value"])

        series = {c: pts for c, pts in series.items() if len(pts) >= 5}
        if not series:
            return None

        return {
            "indicator": indicator,
            "label":     INDICATOR_LABELS.get(indicator, indicator),
            "series":    series,
        }
    except Exception as e:
        msg = str(e) or type(e).__name__
        print(f"[chart:worldbank] {indicator} failed: {msg}")
        return None


async def fetch_worldbank_data(context: dict) -> list[dict]:
    countries = context.get("countries", [])
    if not countries:
        print("[chart:worldbank] no countries identified, skipping")
        return []

    iso_codes = [c["iso3"] for c in countries if c.get("iso3")]
    if not iso_codes:
        iso_codes = [c["iso2"] for c in countries if c.get("iso2")]
    if not iso_codes:
        return []

    indicators  = context.get("worldbank_indicators", [])
    year_start  = context.get("year_start", 1990)
    year_end    = context.get("year_end", CURRENT_YEAR)

    datasets: list[dict] = []
    async with httpx.AsyncClient() as client:
        for indicator in indicators:
            ds = await fetch_worldbank_series(iso_codes, indicator, year_start, year_end, client)
            if ds:
                datasets.append(ds)
            await asyncio.sleep(0.3)

    print(f"[chart:worldbank] fetched {len(datasets)}/{len(indicators)} indicators")
    return datasets


# ---------------------------------------------------------------------------
# Step 2b — NASA POWER climate data (optional)
# ---------------------------------------------------------------------------

async def fetch_nasa_power(lat: float, lon: float, year_start: int, year_end: int) -> Optional[dict]:
    if lat is None or lon is None:
        return None
    # NASA POWER annual data starts in 1981; current year data is incomplete until year-end
    nasa_start = max(year_start, 1981)
    nasa_end   = min(year_end, CURRENT_YEAR - 1)
    if nasa_start > nasa_end:
        return None
    url = (
        f"https://power.larc.nasa.gov/api/temporal/annual/point"
        f"?parameters=T2M,PRECTOTCORR&community=AG"
        f"&longitude={lon}&latitude={lat}"
        f"&start={nasa_start}&end={nasa_end}&format=JSON"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            # Guard against HTML/Next.js responses (API endpoint changes)
            if "text/html" in resp.headers.get("content-type", ""):
                raise ValueError("NASA POWER returned HTML — endpoint may have changed")
            data = resp.json()
        params = data.get("properties", {}).get("parameter", {})
        result: dict[str, dict[int, float]] = {}
        for param_name, year_dict in params.items():
            clean = {int(y): v for y, v in year_dict.items() if v not in (None, -999.0)}
            if len(clean) >= 5:
                result[param_name] = clean
        if result:
            return result
        raise ValueError("NASA POWER returned empty data")
    except Exception as e:
        print(f"[chart:nasa] NASA POWER failed ({type(e).__name__}: {e}), trying Open-Meteo fallback...")
        return await _fetch_openmeteo(lat, lon, nasa_start, nasa_end)


async def _fetch_openmeteo(lat: float, lon: float, year_start: int, year_end: int) -> Optional[dict]:
    """Fallback climate data from Open-Meteo Historical Weather API (free, no key, global coverage)."""
    # Open-Meteo archive covers 1940–present at daily resolution
    start_date = f"{year_start}-01-01"
    end_date   = f"{min(year_end, CURRENT_YEAR - 1)}-12-31"
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&daily=temperature_2m_mean,precipitation_sum"
        f"&timezone=UTC"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        daily = data.get("daily", {})
        dates      = daily.get("time", [])
        temps      = daily.get("temperature_2m_mean", [])
        precips    = daily.get("precipitation_sum", [])

        if not dates:
            return None

        # Aggregate daily → annual (mean temp, sum precip)
        year_temp:   dict[int, list[float]] = {}
        year_precip: dict[int, list[float]] = {}
        for date_str, t, p in zip(dates, temps, precips):
            yr = int(date_str[:4])
            if t is not None:
                year_temp.setdefault(yr, []).append(t)
            if p is not None:
                year_precip.setdefault(yr, []).append(p)

        result: dict[str, dict[int, float]] = {}
        t2m = {yr: round(sum(v) / len(v), 2) for yr, v in year_temp.items() if len(v) >= 300}
        if len(t2m) >= 5:
            result["T2M"] = t2m
        prec = {yr: round(sum(v), 1) for yr, v in year_precip.items() if len(v) >= 300}
        if len(prec) >= 5:
            result["PRECTOTCORR"] = prec

        if result:
            print(f"[chart:nasa] Open-Meteo fallback: {list(result.keys())}, {len(next(iter(result.values())))} years")
            return result
        return None
    except Exception as e:
        print(f"[chart:nasa] Open-Meteo fallback also failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Step 3 — Claude plans the best chart
# ---------------------------------------------------------------------------

PLAN_PROMPT = """Tu es un expert en visualisation de données pour la recherche géographique académique.

Sujet de recherche: {topic}
Contexte géographique: {location}

Données disponibles:
{data_summary}

Décide quel type de graphique représente le mieux ces données de façon scientifique et claire.

TYPES DE GRAPHIQUES DISPONIBLES:
- line_chart          : série(s) temporelle(s) sur UN SEUL axe Y — uniquement si toutes les séries partagent la même unité
- dual_axis_line      : série(s) temporelle(s) sur DEUX axes Y — OBLIGATOIRE si les séries ont des unités différentes (ex: °C et mm, valeurs absolues et %, etc.)
- bar_chart           : comparaison de catégories — ideal pour une seule série, catégories sur x
- grouped_bar         : comparaison multi-pays/multi-indicateurs — barres groupées côte à côte
- scatter             : corrélation entre deux variables — chaque point = une entité
- histogram           : distribution d'une variable — fréquences ou densité
- box_plot            : dispersion et outliers par groupe (boîtes à moustaches)
- heatmap_matrix      : matrice de corrélation entre indicateurs

RÈGLES STRICTES:
- UN SEUL graphique, le plus pertinent scientifiquement
- INTERDIT de mélanger des unités incompatibles sur le même axe Y — utiliser dual_axis_line dans ce cas
- Exemples d'unités incompatibles: °C et mm, kg et %, km² et nb habitants, USD et tonnes
- Minimum 5 points pour line_chart/dual_axis_line/histogram; minimum 3 entités pour bar/grouped_bar/scatter
- Titre court et précis en français (≤ 14 mots)
- Labels d'axes en français
- Si les données sont insuffisantes ou incohérentes → skip

Retourne UNIQUEMENT du JSON valide:

Si pertinent (axes identiques):
{{
  "skip": false,
  "type": "line_chart",
  "title": "Évolution de la population urbaine en Afrique subsaharienne (1990–2024)",
  "x_label": "Année",
  "y_label": "Population urbaine (% du total)",
  "source": "Banque mondiale (2024)",
  "series": [
    {{"label": "Nigeria", "x": [1990, 1991, ...], "y": [35.1, 35.8, ...]}}
  ]
}}

Si unités incompatibles (ex: température + précipitations):
{{
  "skip": false,
  "type": "dual_axis_line",
  "title": "Température et précipitations annuelles en Scandinavie (1981–2024)",
  "x_label": "Année",
  "y_label": "Température moyenne (°C)",
  "y2_label": "Précipitations (mm)",
  "source": "Open-Meteo (2024)",
  "series": [
    {{"label": "Température (°C)", "yaxis": "y1", "x": [1981, 1982, ...], "y": [-2.1, -1.8, ...]}},
    {{"label": "Précipitations (mm)", "yaxis": "y2", "x": [1981, 1982, ...], "y": [450, 470, ...]}}
  ]
}}

Si non pertinent:
{{
  "skip": true,
  "skip_reason": "Données trop éparses pour une visualisation cohérente"
}}

Note: pour scatter, chaque point a "x_label" et "y_label" distincts, et chaque élément de series est {{"label": "pays", "x": valeur_indicateur_1, "y": valeur_indicateur_2}}.
Note: pour histogram, une seule série avec tous les points dans "x".
Note: pour heatmap_matrix, series est une matrice: {{"labels": ["ind1","ind2",...], "matrix": [[1.0, 0.8, ...], ...]}}.
"""


def _interpolate_gaps(pts: dict) -> dict:
    """Fill year gaps with linear interpolation for smooth chart rendering."""
    sorted_pts = sorted((int(y), v) for y, v in pts.items() if v is not None)
    if len(sorted_pts) < 2:
        return pts
    filled: dict[int, float] = {}
    for i, (y, v) in enumerate(sorted_pts):
        filled[y] = v
        if i < len(sorted_pts) - 1:
            next_y, next_v = sorted_pts[i + 1]
            gap = next_y - y
            if 1 < gap <= 5:
                for g in range(1, gap):
                    interp = v + (next_v - v) * g / gap
                    filled[y + g] = round(interp, 2)
    return filled


MAX_CHART_COUNTRIES = 8


def _select_representative_countries(series: dict[str, dict], n: int = MAX_CHART_COUNTRIES) -> dict[str, dict]:
    """Pick a representative sample: highest, lowest, and evenly spaced middle values."""
    if len(series) <= n:
        return series
    # Rank by last available value
    ranked = sorted(series.items(), key=lambda kv: max(kv[1].values()) if kv[1] else 0)
    # Always include top 2, bottom 2, and spread the rest evenly
    selected: dict[str, dict] = {}
    selected[ranked[0][0]] = ranked[0][1]
    selected[ranked[1][0]] = ranked[1][1]
    selected[ranked[-1][0]] = ranked[-1][1]
    selected[ranked[-2][0]] = ranked[-2][1]
    remaining = n - 4
    step = max(1, (len(ranked) - 4) // (remaining + 1))
    for i in range(1, remaining + 1):
        idx = 2 + i * step
        if idx < len(ranked) - 2:
            selected[ranked[idx][0]] = ranked[idx][1]
    return selected


def _summarize_datasets(wb_datasets: list[dict], nasa_data: Optional[dict]) -> str:
    lines: list[str] = []

    for ds in wb_datasets:
        label = ds["label"]
        series = ds["series"]
        if len(series) > MAX_CHART_COUNTRIES:
            series = _select_representative_countries(series)
            print(f"[chart:summary] {len(ds['series'])} countries → {len(series)} representative for chart")
        for country, pts in series.items():
            filled = _interpolate_gaps(pts)
            sorted_pts = sorted(filled.items())
            years  = [str(y) for y, _ in sorted_pts]
            values = [f"{v:.2f}" for _, v in sorted_pts]
            lines.append(
                f"[Banque mondiale] {label} — {country}\n"
                f"  Années : {', '.join(years)}\n"
                f"  Valeurs: {', '.join(values)}"
            )

    if nasa_data:
        for param, pts in nasa_data.items():
            sorted_pts = sorted(pts.items())
            years  = [str(y) for y, _ in sorted_pts]
            values = [f"{v:.2f}" for _, v in sorted_pts]
            param_label = DATA_LABELS.get(param, param)
            lines.append(
                f"[NASA POWER] {param_label}\n"
                f"  Années : {', '.join(years)}\n"
                f"  Valeurs: {', '.join(values)}"
            )

    return "\n\n".join(lines) if lines else "Aucune donnée disponible."


async def claude_plan_chart(
    topic: str,
    context: dict,
    wb_datasets: list[dict],
    nasa_data: Optional[dict],
    client: anthropic.AsyncAnthropic,
) -> Optional[dict]:
    if not wb_datasets and not nasa_data:
        print("[chart:plan] no data available, skipping")
        return None

    data_summary = _summarize_datasets(wb_datasets, nasa_data)
    location = context.get("region_name") or (
        ", ".join(c["name"] for c in context.get("countries", []))
    )

    prompt = PLAN_PROMPT.format(
        topic=topic,
        location=location,
        data_summary=data_summary,
    )

    current_prompt = prompt
    for attempt in range(3):
        try:
            msg = await client.messages.create(
                model=CLAUDE_SMART,
                max_tokens=2000,
                messages=[{"role": "user", "content": current_prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            plan = json.loads(raw)
            break
        except json.JSONDecodeError as e:
            if attempt < 2:
                print(f"[chart:plan] JSON error attempt {attempt+1}, retrying: {e}")
                current_prompt = (
                    prompt
                    + "\n\nIMPORTANT: JSON invalide. Retourne UNIQUEMENT un objet JSON valide et complet. "
                    "Si les séries sont longues, réduis à 10 points max par série mais ne coupe jamais le JSON."
                )
            else:
                print(f"[chart:plan] JSON parse failed after 3 attempts: {e}")
                return None
        except Exception as e:
            print(f"[chart:plan] Claude error: {e}")
            return None
    else:
        return None

    if plan.get("skip"):
        print(f"[chart:plan] skipped: {plan.get('skip_reason', '—')}")
        return None

    if not plan.get("series") and plan.get("type") not in ("histogram", "heatmap_matrix"):
        print("[chart:plan] no series in plan, skipping")
        return None

    print(f"[chart:plan] type={plan['type']} — {plan.get('title', '')}")
    return plan


# ---------------------------------------------------------------------------
# Step 4 — Render chart from plan
# ---------------------------------------------------------------------------

def _xaxis_kwargs(x_label: str, series: list[dict], chart_type: str) -> dict:
    """Build Plotly xaxis dict with adaptive tick spacing for dense time series."""
    base = dict(
        title=x_label,
        gridcolor=PALETTE["grid"],
        zeroline=False,
        tickfont=dict(size=9),
    )
    if chart_type not in ("line_chart", "bar_chart", "grouped_bar"):
        return base

    # Collect all unique x values across series
    all_xs: list[str] = []
    seen: set[str] = set()
    for s in series:
        for x in s.get("x", []):
            sx = str(x)
            if sx not in seen:
                seen.add(sx)
                all_xs.append(sx)

    n = len(all_xs)
    if n <= 12:
        return base  # fine as-is

    # Choose step so we show at most ~10 ticks
    step = max(2, round(n / 10))
    tick_vals = all_xs[::step]
    # Always include the last value
    if all_xs[-1] not in tick_vals:
        tick_vals = tick_vals + [all_xs[-1]]

    base["tickmode"]  = "array"
    base["tickvals"]  = tick_vals
    base["ticktext"]  = tick_vals
    return base


def render_chart(plan: dict, output_path: Path) -> Optional[Path]:
    """
    Render chart from plan dict.
    Plotly: line_chart, bar_chart, grouped_bar, scatter.
    Seaborn: box_plot, heatmap_matrix, histogram (statistical precision).
    Matplotlib: fallback for all.
    """
    chart_type = plan.get("type", "line_chart")
    title      = plan.get("title", "")
    x_label    = plan.get("x_label", "")
    y_label    = plan.get("y_label", "")
    source     = plan.get("source", "")
    series     = plan.get("series", [])

    # ── Seaborn path for statistical charts ───────────────────────────────
    if chart_type in ("box_plot", "heatmap_matrix", "histogram"):
        return _render_seaborn(plan, output_path)

    # ── Dual axis line chart ───────────────────────────────────────────────
    if chart_type == "dual_axis_line":
        return _render_dual_axis(plan, output_path)

    # ── Plotly path ────────────────────────────────────────────────────────
    try:
        import plotly.graph_objects as go
        import plotly.io as pio

        fig = go.Figure()
        w, h = 960, 460

        if chart_type == "scatter":
            for i, s in enumerate(series):
                color = SERIES_COLORS[i % len(SERIES_COLORS)]
                fig.add_trace(go.Scatter(
                    x=[s.get("x")], y=[s.get("y")],
                    mode="markers",
                    name=s.get("label", f"Pt {i+1}"),
                    marker=dict(color=color, size=9, opacity=0.85,
                                line=dict(width=0.5, color="white")),
                    hovertemplate=f"{s.get('label','')}<br>x: %{{x:.2f}}<br>y: %{{y:.2f}}<extra></extra>",
                ))
        else:
            for i, s in enumerate(series):
                color = SERIES_COLORS[i % len(SERIES_COLORS)]
                xs = [str(x) for x in s.get("x", [])]
                ys = s.get("y", [])
                label = s.get("label", f"Série {i+1}")

                if chart_type == "line_chart":
                    fig.add_trace(go.Scatter(
                        x=xs, y=ys, mode="lines+markers", name=label,
                        line=dict(color=color, width=2.2),
                        marker=dict(size=5, color=color),
                        hovertemplate=f"{label}<br>%{{x}}: %{{y:.2f}}<extra></extra>",
                    ))
                elif chart_type in ("bar_chart", "grouped_bar"):
                    fig.add_trace(go.Bar(
                        x=xs, y=ys, name=label,
                        marker_color=color,
                        hovertemplate=f"{label}<br>%{{x}}: %{{y:.2f}}<extra></extra>",
                    ))

        annotations = []
        if source:
            annotations.append(dict(
                text=f"Source : {source}",
                xref="paper", yref="paper",
                x=1, y=-0.13, xanchor="right", yanchor="top",
                font=dict(size=8, color="#888"),
                showarrow=False,
            ))

        show_legend = len(series) > 1
        barmode = "group" if chart_type == "grouped_bar" else None

        layout_kwargs = dict(
            title=dict(
                text=title,
                font=dict(size=14, family="Arial", color=PALETTE["primary"]),
                x=0, xanchor="left",
            ),
            template="plotly_white",
            font=dict(family="Arial", size=10, color=PALETTE["text"]),
            margin=dict(l=65, r=40, t=65, b=75),
            height=h, width=w,
            plot_bgcolor="white",
            paper_bgcolor="white",
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02,
                xanchor="left", x=0, font=dict(size=9),
            ) if show_legend else dict(visible=False),
            xaxis=_xaxis_kwargs(x_label, series, chart_type),
            yaxis=dict(
                title=y_label,
                gridcolor=PALETTE["grid"],
                zeroline=False,
                tickfont=dict(size=9),
            ),
            annotations=annotations,
        )
        if barmode:
            layout_kwargs["barmode"] = barmode

        fig.update_layout(**layout_kwargs)
        pio.write_image(fig, str(output_path), format="png", scale=2, width=w, height=h)
        print(f"[chart:render] saved (plotly/{chart_type}) → {output_path}")
        return output_path

    except Exception as e:
        print(f"[chart:render] plotly failed ({type(e).__name__}: {e}), using matplotlib fallback")

    # ── Matplotlib fallback ────────────────────────────────────────────────
    return _render_matplotlib(plan, output_path)


def _render_dual_axis(plan: dict, output_path: Path) -> Optional[Path]:
    """Render a dual-Y-axis line chart for series with incompatible units (e.g. °C and mm)."""
    title   = plan.get("title", "")
    x_label = plan.get("x_label", "")
    y_label = plan.get("y_label", "")    # left axis
    y2_label = plan.get("y2_label", "") # right axis
    source  = plan.get("source", "")
    series  = plan.get("series", [])

    try:
        import plotly.graph_objects as go
        import plotly.io as pio

        w, h = 960, 460
        fig = go.Figure()

        # Split series by axis assignment
        y1_series = [s for s in series if s.get("yaxis", "y1") == "y1"]
        y2_series = [s for s in series if s.get("yaxis") == "y2"]

        # Y1 series (left axis) — primary colors
        for i, s in enumerate(y1_series):
            color = SERIES_COLORS[i % len(SERIES_COLORS)]
            xs = [str(x) for x in s.get("x", [])]
            ys = s.get("y", [])
            label = s.get("label", f"Série {i+1}")
            fig.add_trace(go.Scatter(
                x=xs, y=ys, mode="lines+markers", name=label,
                yaxis="y1",
                line=dict(color=color, width=2.2),
                marker=dict(size=5, color=color),
                hovertemplate=f"{label}<br>%{{x}}: %{{y:.2f}}<extra></extra>",
            ))

        # Y2 series (right axis) — secondary colors starting from index len(y1_series)
        for i, s in enumerate(y2_series):
            color = SERIES_COLORS[(len(y1_series) + i) % len(SERIES_COLORS)]
            xs = [str(x) for x in s.get("x", [])]
            ys = s.get("y", [])
            label = s.get("label", f"Série {len(y1_series)+i+1}")
            fig.add_trace(go.Scatter(
                x=xs, y=ys, mode="lines+markers", name=label,
                yaxis="y2",
                line=dict(color=color, width=2.2, dash="dot"),
                marker=dict(size=5, color=color, symbol="diamond"),
                hovertemplate=f"{label}<br>%{{x}}: %{{y:.2f}}<extra></extra>",
            ))

        annotations = []
        if source:
            annotations.append(dict(
                text=f"Source : {source}",
                xref="paper", yref="paper",
                x=1, y=-0.13, xanchor="right", yanchor="top",
                font=dict(size=8, color="#888"),
                showarrow=False,
            ))

        all_series = y1_series + y2_series
        fig.update_layout(
            title=dict(
                text=title,
                font=dict(size=14, family="Arial", color=PALETTE["primary"]),
                x=0, xanchor="left",
            ),
            template="plotly_white",
            font=dict(family="Arial", size=10, color=PALETTE["text"]),
            margin=dict(l=65, r=75, t=65, b=75),
            height=h, width=w,
            plot_bgcolor="white",
            paper_bgcolor="white",
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02,
                xanchor="left", x=0, font=dict(size=9),
            ),
            xaxis=_xaxis_kwargs(x_label, all_series, "line_chart"),
            yaxis=dict(
                title=y_label,
                gridcolor=PALETTE["grid"],
                zeroline=False,
                tickfont=dict(size=9),
            ),
            yaxis2=dict(
                title=y2_label,
                overlaying="y",
                side="right",
                gridcolor="rgba(0,0,0,0)",  # no grid for secondary (would overlap)
                zeroline=False,
                tickfont=dict(size=9),
                showgrid=False,
            ),
            annotations=annotations,
        )

        pio.write_image(fig, str(output_path), format="png", scale=2, width=w, height=h)
        print(f"[chart:render] saved (plotly/dual_axis_line) → {output_path}")
        return output_path

    except Exception as e:
        print(f"[chart:render] dual_axis_line failed ({type(e).__name__}: {e}), falling back to matplotlib")
        return _render_matplotlib(plan, output_path)


def _render_seaborn(plan: dict, output_path: Path) -> Optional[Path]:
    """Render statistical charts with seaborn."""
    chart_type = plan.get("type")
    title      = plan.get("title", "")
    x_label    = plan.get("x_label", "")
    y_label    = plan.get("y_label", "")
    source     = plan.get("source", "")
    series     = plan.get("series", [])

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import seaborn as sns
        import pandas as pd

        apply_pub_style()
        sns.set_theme(style="whitegrid", palette=SERIES_COLORS[:len(series) or 1])

        if chart_type == "heatmap_matrix":
            labels = plan.get("labels", [s.get("label", f"V{i}") for i, s in enumerate(series)])
            matrix = plan.get("matrix")
            if not matrix:
                # Build correlation matrix from series data
                data_dict = {}
                for s in series:
                    if s.get("x") and s.get("y"):
                        data_dict[s["label"]] = s["y"]
                if not data_dict:
                    return None
                df = pd.DataFrame(data_dict)
                matrix_df = df.corr()
            else:
                matrix_df = pd.DataFrame(matrix, index=labels, columns=labels)

            fig, ax = plt.subplots(figsize=(max(6, len(labels)), max(5, len(labels) - 1)))
            sns.heatmap(
                matrix_df, annot=True, fmt=".2f", cmap="coolwarm",
                center=0, vmin=-1, vmax=1, linewidths=0.5,
                cbar_kws={"shrink": 0.8}, ax=ax,
            )
            ax.set_title(title, fontsize=12, fontweight="bold", color=PALETTE["primary"], pad=12)

        elif chart_type == "box_plot":
            rows = []
            for s in series:
                for val in s.get("y", []):
                    rows.append({"group": s.get("label", ""), "value": val})
            if not rows:
                return None
            df = pd.DataFrame(rows)
            fig, ax = plt.subplots(figsize=(max(8, len(series) * 1.5), 6))
            sns.boxplot(data=df, x="group", y="value", palette=SERIES_COLORS[:len(series)], ax=ax)
            ax.set_xlabel(x_label, fontsize=10)
            ax.set_ylabel(y_label, fontsize=10)
            ax.set_title(title, fontsize=12, fontweight="bold", color=PALETTE["primary"], pad=12)

        elif chart_type == "histogram":
            all_vals = []
            for s in series:
                all_vals.extend(s.get("x", []) or s.get("y", []))
            if not all_vals:
                return None
            fig, ax = plt.subplots(figsize=(10, 6))
            sns.histplot(all_vals, kde=True, color=PALETTE["primary"], ax=ax)
            ax.set_xlabel(x_label or y_label, fontsize=10)
            ax.set_ylabel("Fréquence", fontsize=10)
            ax.set_title(title, fontsize=12, fontweight="bold", color=PALETTE["primary"], pad=12)

        else:
            return None

        if source:
            fig.text(0.99, 0.01, f"Source : {source}", ha="right", fontsize=7.5, color="#888")

        plt.tight_layout()
        plt.savefig(output_path, dpi=300, facecolor="white")
        plt.close()
        print(f"[chart:render] saved (seaborn/{chart_type}) → {output_path}")
        return output_path

    except Exception as e:
        print(f"[chart:render] seaborn failed ({type(e).__name__}: {e})")
        return None


def _render_matplotlib(plan: dict, output_path: Path) -> Optional[Path]:
    """Matplotlib fallback for line/bar charts."""
    chart_type = plan.get("type", "line_chart")
    title      = plan.get("title", "")
    x_label    = plan.get("x_label", "")
    y_label    = plan.get("y_label", "")
    source     = plan.get("source", "")
    series     = plan.get("series", [])

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        apply_pub_style()
        fig, ax = plt.subplots(figsize=(11, 5))

        for i, s in enumerate(series):
            color = SERIES_COLORS[i % len(SERIES_COLORS)]
            xs = s.get("x", [])
            ys = s.get("y", [])
            label = s.get("label", f"Série {i+1}")
            if chart_type in ("line_chart",):
                ax.plot(xs, ys, color=color, linewidth=2, marker="o", markersize=4, label=label)
            elif chart_type in ("bar_chart", "grouped_bar"):
                ax.bar(xs, ys, label=label, color=color, alpha=0.85)
            elif chart_type == "scatter":
                ax.scatter([s.get("x")], [s.get("y")], color=color, s=80, label=label, alpha=0.85,
                           edgecolors="white", linewidths=0.5)

        ax.set_xlabel(x_label, fontsize=10)
        ax.set_ylabel(y_label, fontsize=10)
        ax.set_title(title, fontsize=12, fontweight="bold", color=PALETTE["primary"], pad=12)
        if len(series) > 1:
            ax.legend(fontsize=9, framealpha=0.9)
        if source:
            fig.text(0.99, 0.01, f"Source : {source}", ha="right", fontsize=7.5, color="#888")
        ax.grid(True, color=PALETTE["grid"], linewidth=0.8)
        ax.set_axisbelow(True)
        plt.tight_layout()
        plt.savefig(output_path, dpi=300, facecolor="white")
        plt.close()
        print(f"[chart:render] saved (matplotlib) → {output_path}")
        return output_path

    except Exception as e:
        print(f"[chart:render] all renderers failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_chart(
    data,
    topic: str = "",
    output_dir: str = "./output",
    formats: Optional[list[str]] = None,
) -> dict:
    """
    Generate the best possible chart for the given topic using open data.
    Returns {"chart": str} (path to PNG) or {}.
    """
    if formats is None:
        formats = ["png"]

    slug    = slugify(topic) if topic else "research"
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[chart] topic='{topic}'")

    ai_client = anthropic.AsyncAnthropic()

    print("[chart] Analyzing topic...")
    context = await analyze_topic(topic, ai_client)
    print(f"[chart] Countries: {[c['name'] for c in context.get('countries', [])]}")
    print(f"[chart] Indicators: {context.get('worldbank_indicators', [])}")

    # Fetch World Bank + NASA in parallel
    wb_task = asyncio.create_task(fetch_worldbank_data(context))
    nasa_task = (
        asyncio.create_task(fetch_nasa_power(
            context.get("lat"), context.get("lon"),
            context.get("year_start", 1990),
            context.get("year_end", CURRENT_YEAR),
        ))
        if context.get("is_climate_topic")
        else None
    )

    wb_datasets = await wb_task
    nasa_data   = await nasa_task if nasa_task else None

    plan = await claude_plan_chart(topic, context, wb_datasets, nasa_data, ai_client)
    if not plan:
        print("[chart] No chart generated.")
        return {}

    chart_path = out_dir / f"{slug}-chart.png"
    path = render_chart(plan, chart_path)
    if path:
        result = {"chart": str(path), "title": plan.get("title", ""), "type": plan.get("type", "")}
        print(f"[chart] Done → {path}")
        return result

    return {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="chart-skill — professional data charts")
    parser.add_argument("--topic",      required=True)
    parser.add_argument("--data",       default=None, help="extracted.json or research.json")
    parser.add_argument("--output-dir", default="./output")
    parser.add_argument("--formats",    nargs="+", default=["png"], choices=["png"])
    args = parser.parse_args()

    raw = []
    if args.data:
        raw = json.loads(Path(args.data).read_text())

    result = asyncio.run(run_chart(
        data=raw, topic=args.topic,
        output_dir=args.output_dir,
        formats=args.formats,
    ))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
