"""
viz-render-skill
Renders visualizations using Claude API code execution (sandbox) for PNG
and local Folium for interactive HTML maps.

Two rendering paths:
  - PNG: Claude writes & executes Python code in Anthropic's sandbox
         (matplotlib, seaborn, pandas, numpy, scipy pre-installed)
  - HTML: Folium rendered locally (sandbox has no internet for CDN refs)

For choropleths, a bundled Natural Earth GeoJSON is sent to the sandbox
as base64 so Claude can use geopandas to render country polygons.
"""

import asyncio  # noqa: F401 — used in retry sleep
import base64
import csv as _csv
import io
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

load_dotenv()

CLAUDE_MODEL = "claude-sonnet-4-6"

# Publication-quality palette (matches all other skills)
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

GEOJSON_PATH = Path(__file__).parent / "data" / "ne_110m_admin_0_countries.geojson"


# ---------------------------------------------------------------------------
# Data structures (mirrors viz-ideation-skill)
# ---------------------------------------------------------------------------

@dataclass
class DataRequirement:
    source: str
    params: dict
    role: str = "primary"
    description: str = ""


@dataclass
class VizIdea:
    id: str
    category: str            # "chart" | "map" | "choropleth"
    description: str
    title_fr: str
    why_compelling: str = ""
    data_requirements: list = None
    fallback_idea: Optional[str] = None


@dataclass
class FetchedData:
    requirement: DataRequirement
    success: bool
    data_csv: Optional[str] = None
    n_points: int = 0
    error: Optional[str] = None


@dataclass
class RenderResult:
    success: bool
    png_path: Optional[str] = None
    html_path: Optional[str] = None
    title: str = ""
    viz_type: str = ""
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Sandbox rendering (PNG via Claude API code execution)
# ---------------------------------------------------------------------------

RENDER_PROMPT_CHART = """Tu dois créer une visualisation de données de qualité publication pour un rapport de recherche géographique académique.

## Visualisation demandée
{description}

## Titre de la figure
{title_fr}

## Données (CSV)
```csv
{data_csv}
```

## Instructions de style OBLIGATOIRES
- Qualité publication académique (300 DPI)
- Police: DejaVu Sans (ou Arial), taille 10
- Fond blanc, grille subtile couleur {grid}
- Palette de couleurs pour les séries (dans cet ordre): {series_colors}
- Couleur primaire pour titres: {primary}
- Labels et titres en FRANÇAIS
- Annotation source en bas à droite, police 8, gris
- Marges serrées (tight layout)
- Taille: figsize=(10, 5) pour charts, (12, 8) pour maps
- Sauvegarder avec: import os; plt.savefig(os.path.join(os.environ.get('OUTPUT_DIR', '/tmp'), 'output.png'), dpi=300, bbox_inches='tight')

## Instructions techniques
- Utiliser matplotlib et/ou seaborn (pré-installés)
- Lire les données avec pandas: pd.read_csv(io.StringIO(csv_string))
- Le graphique doit être auto-suffisant — toutes les données sont fournies ci-dessus
- Pas de plt.show(), seulement plt.savefig()
- Axes propres: pas de spines top/right, tick labels lisibles
- Si beaucoup de pays (>8 séries), utiliser un sous-ensemble représentatif ou des couleurs distinctes
- Pour les séries temporelles: lignes avec marqueurs petits (markersize=4)
- Légende positionnée intelligemment (pas sur les données)

Écris et exécute le code Python complet.
"""

RENDER_PROMPT_CHOROPLETH = """Tu dois créer une carte choroplèthe de qualité publication pour un rapport de recherche géographique académique.

## Visualisation demandée
{description}

## Titre de la figure
{title_fr}

## Données (CSV avec valeurs par pays)
```csv
{data_csv}
```

## Données géographiques (GeoJSON des frontières)
Le fichier GeoJSON est disponible à /tmp/countries.geojson (déjà créé).
Le champ ISO3 dans le GeoJSON est "ISO_A3" ou "ISO_A3_EH".

## Instructions de style OBLIGATOIRES
- Qualité publication académique (300 DPI)
- Police: DejaVu Sans, taille 10
- Fond blanc
- Colorscale appropriée au sujet (ex: RdYlGn pour espérance de vie, YlOrRd pour émissions, Blues pour précipitations)
- Titres et labels en FRANÇAIS
- Barre de couleur avec label de l'indicateur
- Frontières des pays visibles (linewidth=0.5, gris)
- Sauvegarder avec: import os; plt.savefig(os.path.join(os.environ.get('OUTPUT_DIR', '/tmp'), 'output.png'), dpi=300, bbox_inches='tight')

## Instructions techniques
- Exécuter: pip install geopandas
- Lire GeoJSON: gpd.read_file('/tmp/countries.geojson')
- Merger avec les données CSV sur le code ISO3
- Utiliser matplotlib pour le rendu (ax.set_axis_off() pour cacher les axes)
- Centrer la vue sur la région d'intérêt (set_xlim/set_ylim)
- Ajouter les pays sans données en gris clair (#EEEEEE)
- Pas de plt.show(), seulement plt.savefig()

Écris et exécute le code Python complet.
"""

RENDER_PROMPT_HEATMAP = """Tu dois créer une carte de densité (heatmap) de qualité publication pour un rapport de recherche géographique.

## Visualisation demandée
{description}

## Titre de la figure
{title_fr}

## Données (CSV avec lat/lon)
```csv
{data_csv}
```

## Instructions de style OBLIGATOIRES
- Qualité publication académique (300 DPI)
- Police: DejaVu Sans, taille 10
- Fond blanc
- Colormap: YlOrRd (jaune-orange-rouge)
- Barre de couleur: "Densité d'occurrences"
- Titres en FRANÇAIS
- Sauvegarder avec: import os; plt.savefig(os.path.join(os.environ.get('OUTPUT_DIR', '/tmp'), 'output.png'), dpi=300, bbox_inches='tight')

## Instructions techniques
- Exécuter: pip install geopandas cartopy
- Utiliser cartopy pour le fond de carte (coastlines, borders, rivers, lakes)
- KDE (scipy.stats.gaussian_kde) pour la densité
- Grille 300x300 pour le maillage KDE
- Points originaux en overlay (petits, alpha=0.25)
- Masquer le KDE dans l'océan en re-dessinant ocean/lakes par-dessus (zorder supérieur)
- Projection: PlateCarree
- Pas de plt.show(), seulement plt.savefig()

Écris et exécute le code Python complet.
"""


async def render_via_sandbox(
    idea: VizIdea,
    fetched_data: list[FetchedData],
    output_dir: Path,
    slug: str,
) -> RenderResult:
    """Render a visualization using Claude API code execution sandbox."""
    client = anthropic.AsyncAnthropic()

    # Combine all CSV data
    data_csvs = {}
    for fd in fetched_data:
        if fd.success and fd.data_csv:
            data_csvs[fd.requirement.role] = fd.data_csv

    if not data_csvs:
        return RenderResult(success=False, error="No data available", title=idea.title_fr)

    # Choose prompt template based on category
    combined_csv = "\n".join(data_csvs.values())

    if idea.category == "choropleth":
        prompt = RENDER_PROMPT_CHOROPLETH.format(
            description=idea.description,
            title_fr=idea.title_fr,
            data_csv=combined_csv,
            **PALETTE,
            series_colors=", ".join(SERIES_COLORS),
        )
        # Load and filter GeoJSON for the sandbox
        geojson_content = await _prepare_geojson(fetched_data)
        if geojson_content:
            prompt = f"D'abord, crée le fichier GeoJSON:\n```python\nimport json\ngeojson = {json.dumps(json.dumps(geojson_content))}\nwith open('/tmp/countries.geojson', 'w') as f:\n    f.write(geojson)\n```\n\n{prompt}"
    elif idea.category == "map":
        prompt = RENDER_PROMPT_HEATMAP.format(
            description=idea.description,
            title_fr=idea.title_fr,
            data_csv=combined_csv,
            **PALETTE,
            series_colors=", ".join(SERIES_COLORS),
        )
    else:
        prompt = RENDER_PROMPT_CHART.format(
            description=idea.description,
            title_fr=idea.title_fr,
            data_csv=combined_csv,
            **PALETTE,
            series_colors=", ".join(SERIES_COLORS),
        )

    # Retry loop for rate limits (429)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = await client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=16000,
                tools=[{
                    "type": "code_execution_20260120",
                    "name": "code_execution",
                }],
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract PNG file from response
            # The code_execution_20260120 tool produces:
            #   bash_code_execution_tool_result (type)
            #     .content = BashCodeExecutionResultBlock (single object, not list)
            #       .content = [BashCodeExecutionOutputBlock(file_id=...), ...]
            #       .stdout, .stderr, .return_code
            png_path = output_dir / f"{slug}-{idea.id}.png"
            file_downloaded = False

            for block in response.content:
                btype = getattr(block, "type", "")
                if btype == "bash_code_execution_tool_result":
                    result_block = getattr(block, "content", None)
                    if result_block is None:
                        continue
                    rtype = getattr(result_block, "type", "")
                    if rtype == "bash_code_execution_result":
                        for output_item in getattr(result_block, "content", []):
                            if getattr(output_item, "type", "") == "bash_code_execution_output":
                                fid = getattr(output_item, "file_id", None)
                                if fid:
                                    file_content = await client.beta.files.download(fid)
                                    with open(png_path, "wb") as f:
                                        f.write(file_content.read())
                                    file_downloaded = True
                                    print(f"[viz-render] sandbox PNG → {png_path}")
                                    break
                if file_downloaded:
                    break

            if not file_downloaded:
                error_msg = _extract_error(response)
                return RenderResult(
                    success=False,
                    error=error_msg or "No output file produced by sandbox",
                    title=idea.title_fr,
                    viz_type=idea.category,
                )

            return RenderResult(
                success=True,
                png_path=str(png_path),
                title=idea.title_fr,
                viz_type=idea.category,
            )

        except anthropic.RateLimitError as e:
            wait = 30 * (attempt + 1)
            print(f"[viz-render] rate limit hit, waiting {wait}s (attempt {attempt+1}/{max_retries})...")
            await asyncio.sleep(wait)
            if attempt == max_retries - 1:
                print(f"[viz-render] sandbox rendering failed after {max_retries} retries: {e}")
                return RenderResult(
            success=False,
            error=str(e),
            title=idea.title_fr,
            viz_type=idea.category,
        )


def _extract_error(response) -> str:
    """Extract error message from code execution response."""
    for block in response.content:
        btype = getattr(block, "type", "")
        if btype == "bash_code_execution_tool_result":
            result_block = getattr(block, "content", None)
            if result_block is None:
                continue
            rtype = getattr(result_block, "type", "")
            if rtype == "bash_code_execution_result":
                stderr = getattr(result_block, "stderr", "")
                stdout = getattr(result_block, "stdout", "")
                if stderr:
                    return stderr[:500]
                if stdout and ("error" in stdout.lower() or "traceback" in stdout.lower()):
                    return stdout[:500]
            elif rtype == "bash_code_execution_tool_result_error":
                return getattr(result_block, "error_code", "unknown error")
        if hasattr(block, "text"):
            text = block.text
            if "error" in text.lower() or "traceback" in text.lower():
                return text[:500]
    return ""


async def _prepare_geojson(fetched_data: list[FetchedData]) -> Optional[str]:
    """Load and filter GeoJSON to only the countries in the data."""
    if not GEOJSON_PATH.exists():
        print(f"[viz-render] GeoJSON not found: {GEOJSON_PATH}")
        return None

    try:
        with open(GEOJSON_PATH, "r") as f:
            geojson = json.load(f)

        # Extract ISO3 codes from CSV data
        iso3_needed = set()
        for fd in fetched_data:
            if fd.success and fd.data_csv:
                reader = _csv.DictReader(io.StringIO(fd.data_csv))
                for row in reader:
                    iso = row.get("iso3", "")
                    if iso:
                        iso3_needed.add(iso.upper())

        if not iso3_needed:
            return json.dumps(geojson)

        # Filter features to relevant countries + neighbors for context
        filtered_features = []
        for feature in geojson.get("features", []):
            props = feature.get("properties", {})
            iso = props.get("ISO_A3") or props.get("ISO_A3_EH", "")
            if iso in iso3_needed:
                filtered_features.append(feature)

        # If we filtered too aggressively, include all features
        if len(filtered_features) < 3:
            return json.dumps(geojson)

        filtered_geojson = {
            "type": "FeatureCollection",
            "features": filtered_features,
        }
        result = json.dumps(filtered_geojson)
        print(f"[viz-render] GeoJSON filtered: {len(filtered_features)} countries ({len(result)//1024}KB)")
        return result

    except Exception as e:
        print(f"[viz-render] GeoJSON preparation failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Local Folium rendering (HTML maps)
# ---------------------------------------------------------------------------

async def render_folium_html(
    idea: VizIdea,
    fetched_data: list[FetchedData],
    output_dir: Path,
    slug: str,
) -> Optional[str]:
    """Render interactive HTML map locally using Folium."""
    try:
        import folium
    except ImportError:
        print("[viz-render] folium not installed, skipping HTML map")
        return None

    # Get point data from CSV
    points = []
    for fd in fetched_data:
        if not fd.success or not fd.data_csv:
            continue
        reader = _csv.DictReader(io.StringIO(fd.data_csv))
        for row in reader:
            try:
                lat = float(row.get("lat", ""))
                lon = float(row.get("lon", ""))
                points.append({
                    "lat": lat, "lon": lon,
                    "species": row.get("species", ""),
                    "year": row.get("year", ""),
                    "country": row.get("country", ""),
                })
            except (ValueError, TypeError):
                continue

    if not points:
        # Try choropleth HTML via Plotly
        return await _render_choropleth_html(idea, fetched_data, output_dir, slug)

    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)
    lat_span = max(lats) - min(lats)
    zoom = 3 if lat_span > 40 else 5 if lat_span > 15 else 7

    html_path = output_dir / f"{slug}-{idea.id}-map.html"

    if idea.category == "map" and len(points) >= 20:
        # Heatmap
        from folium.plugins import HeatMap
        m = folium.Map(location=[center_lat, center_lon], zoom_start=zoom,
                       tiles="CartoDB positron")
        heat_data = [[p["lat"], p["lon"]] for p in points[:1500]]
        HeatMap(heat_data, radius=15, blur=12, min_opacity=0.4).add_to(m)
    else:
        # Point map
        m = folium.Map(location=[center_lat, center_lon], zoom_start=zoom,
                       tiles="CartoDB positron")
        for p in points[:500]:
            popup_text = p.get("species", "") or p.get("country", "")
            folium.CircleMarker(
                location=[p["lat"], p["lon"]],
                radius=4,
                color=PALETTE["accent"],
                fill=True, fill_color=PALETTE["accent"],
                fill_opacity=0.55, weight=0.5,
                popup=folium.Popup(popup_text[:60], max_width=200) if popup_text else None,
            ).add_to(m)

    # Fit bounds
    if len(points) > 1:
        m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])

    # Title overlay
    title_html = (
        f'<div style="position:fixed;top:12px;left:50%;transform:translateX(-50%);'
        f'background:white;padding:8px 18px;border-radius:4px;'
        f'box-shadow:0 2px 8px rgba(0,0,0,0.2);font-size:13px;font-weight:bold;'
        f'font-family:Arial;z-index:1000;color:{PALETTE["primary"]};">'
        f'{idea.title_fr[:70]}</div>'
    )
    m.get_root().html.add_child(folium.Element(title_html))

    m.save(str(html_path))
    print(f"[viz-render] Folium HTML → {html_path}")
    return str(html_path)


async def _render_choropleth_html(
    idea: VizIdea,
    fetched_data: list[FetchedData],
    output_dir: Path,
    slug: str,
) -> Optional[str]:
    """Render choropleth HTML using Plotly (local)."""
    try:
        import plotly.express as px
    except ImportError:
        print("[viz-render] plotly not installed, skipping choropleth HTML")
        return None

    # Parse country data from CSV
    countries = []
    for fd in fetched_data:
        if not fd.success or not fd.data_csv:
            continue
        reader = _csv.DictReader(io.StringIO(fd.data_csv))
        for row in reader:
            iso3 = row.get("iso3", "")
            value = row.get("value", "")
            country_name = row.get("country", "")
            if iso3 and value:
                try:
                    countries.append({
                        "iso3": iso3,
                        "country": country_name,
                        "value": float(value),
                    })
                except (ValueError, TypeError):
                    continue

    if not countries:
        return None

    # Use latest value per country
    latest: dict[str, dict] = {}
    for c in countries:
        existing = latest.get(c["iso3"])
        if not existing:
            latest[c["iso3"]] = c

    import pandas as pd
    df = pd.DataFrame(list(latest.values()))

    label = idea.title_fr[:60]
    fig = px.choropleth(
        df, locations="iso3", color="value",
        hover_name="country",
        color_continuous_scale="RdYlGn",
        labels={"value": label},
        title=idea.title_fr,
    )
    fig.update_geos(
        showcoastlines=True, coastlinecolor="#AAAAAA", coastlinewidth=0.5,
        showland=True, landcolor="#EEF0E5",
        showocean=True, oceancolor="#C8E0EF",
        showcountries=True, countrycolor="#999999", countrywidth=0.8,
        showrivers=True, rivercolor="#7EC8D9", riverwidth=0.8,
        showlakes=True, lakecolor="#C8E0EF",
        fitbounds="locations",
        projection_type="natural earth",
    )
    fig.update_layout(
        width=1000, height=600,
        margin=dict(l=10, r=10, t=50, b=10),
        font=dict(family="Arial", size=11),
        title_font_color=PALETTE["primary"],
    )

    html_path = output_dir / f"{slug}-{idea.id}-map.html"
    fig.write_html(str(html_path), include_plotlyjs="cdn")
    print(f"[viz-render] Plotly choropleth HTML → {html_path}")
    return str(html_path)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def render_visualization(
    idea: VizIdea,
    fetched_data: list[FetchedData],
    output_dir: Path,
    slug: str,
    formats: list[str] = None,
) -> RenderResult:
    """
    Render a visualization: PNG via sandbox, HTML via local Folium/Plotly.
    Returns RenderResult with paths to generated files.
    """
    if formats is None:
        formats = ["png", "html"]

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = RenderResult(
        success=False,
        title=idea.title_fr,
        viz_type=idea.category,
    )

    # PNG via sandbox
    if "png" in formats:
        sandbox_result = await render_via_sandbox(idea, fetched_data, output_dir, slug)
        if sandbox_result.success:
            result.success = True
            result.png_path = sandbox_result.png_path
        else:
            print(f"[viz-render] sandbox failed for {idea.id}: {sandbox_result.error}")
            # Fallback: try local rendering for charts
            local_png = await _local_fallback_png(idea, fetched_data, output_dir, slug)
            if local_png:
                result.success = True
                result.png_path = local_png

    # HTML via local Folium/Plotly
    if "html" in formats and idea.category in ("map", "choropleth"):
        html_path = await render_folium_html(idea, fetched_data, output_dir, slug)
        if html_path:
            result.html_path = html_path
            if not result.success:
                result.success = True

    return result


async def _local_fallback_png(
    idea: VizIdea,
    fetched_data: list[FetchedData],
    output_dir: Path,
    slug: str,
) -> Optional[str]:
    """Fallback local rendering if sandbox fails."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import pandas as pd

        # Apply pub style
        plt.rcParams.update({
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": PALETTE["grid"],
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
        })

        png_path = output_dir / f"{slug}-{idea.id}.png"

        # Route based on idea category
        if idea.category == "choropleth":
            return await _local_fallback_choropleth(
                idea, fetched_data, output_dir, slug, plt, pd, png_path
            )

        # Parse all data frames by role
        data_by_role: dict[str, pd.DataFrame] = {}
        for fd in fetched_data:
            if fd.success and fd.data_csv:
                data_by_role[fd.requirement.role] = pd.read_csv(io.StringIO(fd.data_csv))

        if not data_by_role:
            return None

        primary_df = data_by_role.get("primary")
        secondary_df = data_by_role.get("secondary")

        # Scatter plot: if we have both primary and secondary data with value columns
        desc_lower = idea.description.lower()
        if primary_df is not None and secondary_df is not None and ("scatter" in desc_lower or "nuage" in desc_lower or "vs" in idea.title_fr.lower()):
            return _local_fallback_scatter(
                idea, data_by_role, plt, pd, png_path
            )

        # Default: time series line chart from primary data
        df = primary_df if primary_df is not None else list(data_by_role.values())[0]

        if "year" in df.columns and "value" in df.columns and "country" in df.columns:
            fig, ax = plt.subplots(figsize=(10, 5))
            countries = df["country"].unique()[:8]
            for i, country in enumerate(countries):
                mask = df["country"] == country
                color = SERIES_COLORS[i % len(SERIES_COLORS)]
                ax.plot(df[mask]["year"], df[mask]["value"],
                        marker="o", markersize=3, linewidth=1.8,
                        color=color, label=country)
            ax.set_xlabel("Année")
            ax.set_title(idea.title_fr, fontsize=12, fontweight="bold",
                         color=PALETTE["primary"])
            if len(countries) > 1:
                ax.legend(loc="best", fontsize=8)
            plt.tight_layout()
            plt.savefig(png_path, dpi=300, facecolor="white")
            plt.close()
            print(f"[viz-render] local fallback PNG → {png_path}")
            return str(png_path)

        elif "lat" in df.columns and "lon" in df.columns:
            fig, ax = plt.subplots(figsize=(12, 8))
            ax.scatter(df["lon"], df["lat"], s=8, alpha=0.5,
                       color=PALETTE["accent"])
            ax.set_xlabel("Longitude")
            ax.set_ylabel("Latitude")
            ax.set_title(idea.title_fr, fontsize=12, fontweight="bold",
                         color=PALETTE["primary"])
            plt.tight_layout()
            plt.savefig(png_path, dpi=300, facecolor="white")
            plt.close()
            print(f"[viz-render] local fallback scatter → {png_path}")
            return str(png_path)

    except Exception as e:
        print(f"[viz-render] local fallback failed: {e}")

    return None


def _local_fallback_scatter(
    idea, data_by_role: dict, plt, pd, png_path: Path,
) -> Optional[str]:
    """Render a scatter/bubble plot locally from primary + secondary data."""
    try:
        primary = data_by_role.get("primary")
        secondary = data_by_role.get("secondary")
        tertiary = data_by_role.get("tertiary")  # population for bubble size

        if primary is None or secondary is None:
            return None

        # Get latest year per country for each dataset
        def latest_per_country(df):
            if "year" in df.columns:
                idx = df.groupby("country")["year"].idxmax()
                return df.loc[idx]
            return df

        p = latest_per_country(primary)
        s = latest_per_country(secondary)

        merged = p[["country", "iso3", "value"]].merge(
            s[["iso3", "value"]], on="iso3", suffixes=("_y", "_x"),
        )

        if merged.empty:
            return None

        fig, ax = plt.subplots(figsize=(10, 7))

        sizes = 80
        if tertiary is not None:
            t = latest_per_country(tertiary)
            merged = merged.merge(t[["iso3", "value"]], on="iso3")
            pop = merged["value"]
            sizes = (pop / pop.max() * 500).clip(lower=30)

        ax.scatter(
            merged["value_x"], merged["value_y"],
            s=sizes, alpha=0.7,
            color=PALETTE["secondary"], edgecolor=PALETTE["primary"], linewidth=0.5,
        )

        # Label each point with country name
        for _, row in merged.iterrows():
            ax.annotate(
                row["country"][:15], (row["value_x"], row["value_y"]),
                fontsize=7, ha="center", va="bottom",
                xytext=(0, 5), textcoords="offset points",
            )

        ax.set_xlabel("Dépenses de santé (% PIB)", fontsize=10)
        ax.set_ylabel("Espérance de vie (années)", fontsize=10)
        ax.set_title(idea.title_fr, fontsize=12, fontweight="bold",
                     color=PALETTE["primary"])
        plt.tight_layout()
        plt.savefig(png_path, dpi=300, facecolor="white")
        plt.close()
        print(f"[viz-render] local fallback scatter → {png_path}")
        return str(png_path)

    except Exception as e:
        print(f"[viz-render] local scatter fallback failed: {e}")
        return None


async def _local_fallback_choropleth(
    idea, fetched_data: list, output_dir: Path, slug: str,
    plt, pd, png_path: Path,
) -> Optional[str]:
    """Render a choropleth locally using geopandas + matplotlib."""
    try:
        import geopandas as gpd

        # Parse country data
        countries = []
        for fd in fetched_data:
            if not fd.success or not fd.data_csv:
                continue
            reader = _csv.DictReader(io.StringIO(fd.data_csv))
            for row in reader:
                iso3 = row.get("iso3", "")
                value = row.get("value", "")
                if iso3 and value:
                    try:
                        countries.append({
                            "iso3": iso3,
                            "country": row.get("country", ""),
                            "value": float(value),
                            "year": int(row.get("year", 0)),
                        })
                    except (ValueError, TypeError):
                        continue

        if not countries:
            return None

        df = pd.DataFrame(countries)
        # Keep latest year per country
        idx = df.groupby("iso3")["year"].idxmax()
        df = df.loc[idx]

        if not GEOJSON_PATH.exists():
            print(f"[viz-render] GeoJSON not found for choropleth fallback")
            return None

        world = gpd.read_file(GEOJSON_PATH)

        # Try ISO_A3, fall back to ISO_A3_EH
        iso_col = "ISO_A3" if "ISO_A3" in world.columns else "ISO_A3_EH"
        merged = world.merge(df, left_on=iso_col, right_on="iso3", how="left")

        # Filter to region of interest (countries with data + neighbors)
        data_iso3 = set(df["iso3"])
        region_mask = merged[iso_col].isin(data_iso3)
        if region_mask.sum() < 3:
            return None

        # Get bounding box from data countries
        region = merged[region_mask]
        bounds = region.total_bounds  # [minx, miny, maxx, maxy]
        pad = 5  # degrees padding
        xlim = (bounds[0] - pad, bounds[2] + pad)
        ylim = (bounds[1] - pad, bounds[3] + pad)

        # Filter all countries within the view
        view_mask = (
            (merged.geometry.centroid.x >= xlim[0]) &
            (merged.geometry.centroid.x <= xlim[1]) &
            (merged.geometry.centroid.y >= ylim[0]) &
            (merged.geometry.centroid.y <= ylim[1])
        )
        view = merged[view_mask]

        fig, ax = plt.subplots(1, 1, figsize=(12, 8))

        # Background countries (no data) in light gray
        view[view["value"].isna()].plot(
            ax=ax, color="#EEEEEE", edgecolor="#999999", linewidth=0.5,
        )

        # Data countries with color scale
        view[view["value"].notna()].plot(
            column="value", ax=ax, legend=True,
            cmap="RdYlGn", edgecolor="#666666", linewidth=0.5,
            legend_kwds={"shrink": 0.6, "label": "Espérance de vie (années)"},
        )

        ax.set_xlim(xlim)
        ax.set_ylim(ylim)
        ax.set_axis_off()
        ax.set_title(idea.title_fr, fontsize=13, fontweight="bold",
                     color=PALETTE["primary"], pad=15)

        plt.tight_layout()
        plt.savefig(png_path, dpi=300, facecolor="white")
        plt.close()
        print(f"[viz-render] local fallback choropleth → {png_path}")
        return str(png_path)

    except ImportError:
        print("[viz-render] geopandas not installed, choropleth fallback unavailable")
        return None
    except Exception as e:
        print(f"[viz-render] local choropleth fallback failed: {e}")
        return None


# ---------------------------------------------------------------------------
# CLI for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test viz-render-skill")
    parser.add_argument("--csv", required=True, help="Path to CSV data file")
    parser.add_argument("--title", default="Test Visualization")
    parser.add_argument("--category", default="chart", choices=["chart", "map", "choropleth"])
    parser.add_argument("--output-dir", default="./output")
    args = parser.parse_args()

    csv_content = Path(args.csv).read_text()
    idea = VizIdea(
        id="test",
        category=args.category,
        description=f"Test visualization: {args.title}",
        title_fr=args.title,
    )
    fd = FetchedData(
        requirement=DataRequirement(source="test", params={}),
        success=True, data_csv=csv_content, n_points=100,
    )

    async def _test():
        result = await render_visualization(idea, [fd], Path(args.output_dir), "test")
        print(f"Success: {result.success}")
        print(f"PNG: {result.png_path}")
        print(f"HTML: {result.html_path}")
        if result.error:
            print(f"Error: {result.error}")

    asyncio.run(_test())
