"""
map-skill
Generates professional publication-quality maps for geographic research.

Approach:
  1. collect_geographic_data() — extracts locations from extracted records + World Bank data
  2. claude_plan_map()         — Claude Sonnet decides map type based on available data
  3. render_*()                — renders chosen map type

Supported map types:
  point_map   — geocoded location markers (folium HTML + contextily PNG)
  choropleth  — country-level shading from World Bank data (plotly)
  heatmap     — density heatmap from point clusters (folium HeatMap)
  skip        — no usable geographic data

Usage:
    python main.py --topic "Urbanisation en Afrique subsaharienne" \\
                   --data extracted.json --output-dir figures/
"""

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Optional

import anthropic
import httpx
from dotenv import load_dotenv

load_dotenv()

CLAUDE_FAST  = "claude-haiku-4-5-20251001"
CLAUDE_SMART = "claude-sonnet-4-6"

MAX_LOCATIONS = 50

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

TYPE_COLORS = {
    "city":    PALETTE["primary"],
    "region":  PALETTE["secondary"],
    "country": PALETTE["accent"],
    "":        PALETTE["neutral"],
}


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


def _cartopy_basemap(ax, lon_min: float, lon_max: float, lat_min: float, lat_max: float, scale: str = "50m"):
    """Set up a Cartopy axes with Natural Earth vector features (borders, rivers, ocean, land)."""
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    ax.set_extent([lon_min, lon_max, lat_min, lat_max], crs=ccrs.PlateCarree())
    ax.add_feature(cfeature.OCEAN.with_scale(scale),     facecolor="#D6EAF8", zorder=0)
    ax.add_feature(cfeature.LAND.with_scale(scale),      facecolor="#F0EFE7", zorder=1)
    ax.add_feature(cfeature.COASTLINE.with_scale(scale), linewidth=0.6, edgecolor="#555555", zorder=2)
    ax.add_feature(cfeature.BORDERS.with_scale(scale),   linewidth=0.5, edgecolor="#888888", linestyle="--", zorder=2)
    ax.add_feature(cfeature.RIVERS.with_scale(scale),    linewidth=0.4, edgecolor="#4BBCD6", alpha=0.7, zorder=2)
    ax.add_feature(cfeature.LAKES.with_scale(scale),     facecolor="#D6EAF8", edgecolor="#4BBCD6", linewidth=0.3, zorder=2)
    gl = ax.gridlines(draw_labels=True, linewidth=0.25, color="#AAAAAA", alpha=0.5, linestyle="--")
    gl.top_labels   = False
    gl.right_labels = False
    return gl


def apply_pub_style() -> None:
    try:
        import matplotlib as mpl
        mpl.rcParams.update({
            "font.family":       "sans-serif",
            "font.sans-serif":   ["Arial", "Helvetica", "DejaVu Sans"],
            "font.size":         10,
            "figure.facecolor":  "white",
            "axes.facecolor":    "white",
            "savefig.dpi":       300,
            "savefig.bbox":      "tight",
            "savefig.facecolor": "white",
        })
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Step 1 — Collect geographic data
# ---------------------------------------------------------------------------

IDENTIFY_FEATURES_PROMPT = """Tu es un expert en géographie et cartographie scientifique.

Sujet de recherche: {topic}

Identifie les entités géographiques pertinentes à représenter sur une carte de la zone d'étude.
La carte doit montrer des données RÉELLES sur le terrain, pas les lieux où les articles ont été publiés.

Pour les sujets de DÉFORESTATION TROPICALE (amazonie, forêt tropicale, déboisement):
- Inclure les fronts de déforestation actifs (ex: "Arc de déforestation du Mato Grosso")
- Inclure les bassins versants clés (ex: "Bassin du Rio Tapajós")
- Inclure les territoires indigènes menacés
- Inclure les unités de conservation (parcs nationaux, RESEX)
- Inclure les villes portes d'entrée des fronts pionniers
- Si le sujet couvre plusieurs pays (ex: Amazonie = Brésil, Pérou, Colombie...) → recommander choropleth plutôt que point_map

Retourne UNIQUEMENT du JSON valide:
{{
  "region_name": "Amazonie brésilienne",
  "bbox": {{"lat_min": -15.0, "lat_max": 5.0, "lon_min": -75.0, "lon_max": -45.0}},
  "zoom": 5,
  "features": [
    {{"name": "Arc de déforestation du Mato Grosso", "type": "deforestation_front", "description": "Principale zone de déforestation active au Brésil"}},
    {{"name": "Terra Indígena Yanomami", "type": "indigenous_territory", "description": "Plus grande terre indigène du Brésil, menacée"}},
    {{"name": "Parc National da Amazônia", "type": "protected_area", "description": "Unité de conservation fédérale"}},
    {{"name": "Manaus", "type": "city", "description": "Capitale de l'État d'Amazonas"}},
    {{"name": "Rio Tapajós", "type": "watershed", "description": "Bassin versant clé de l'Amazonie centrale"}},
    {{"name": "Belém", "type": "city", "description": "Capitale du Pará, porte de l'Amazonie"}}
  ]
}}

Types autorisés: city, indigenous_territory, protected_area, deforestation_front, watershed, hotspot, region, river, border, site
Vise 8-15 entités géographiques réelles et pertinentes pour le sujet.
Choisis des entités directement liées au sujet (pour déforestation → fronts actifs, territoires indigènes, aires protégées, bassins versants).
"""


async def identify_geographic_features(
    topic: str,
    client: anthropic.AsyncAnthropic,
) -> dict:
    """
    Ask Claude Haiku to identify geographic entities relevant to the research topic.
    Returns {"region_name": str, "bbox": dict, "zoom": int, "features": list[dict]}.
    """
    prompt = IDENTIFY_FEATURES_PROMPT.format(topic=topic)
    current_prompt = prompt
    for attempt in range(3):
        try:
            msg = await client.messages.create(
                model=CLAUDE_FAST,
                max_tokens=1000,
                messages=[{"role": "user", "content": current_prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            result = json.loads(raw)
            print(f"[map:features] {len(result.get('features', []))} features identified in {result.get('region_name', topic)}")
            return result
        except json.JSONDecodeError as e:
            if attempt < 2:
                current_prompt = prompt + "\n\nIMPORTANT: JSON invalide. Retourne UNIQUEMENT du JSON valide."
            else:
                print(f"[map:features] JSON failed: {e}")
                return {"region_name": topic, "bbox": None, "zoom": 5, "features": []}
        except Exception as e:
            print(f"[map:features] Claude error: {e}")
            return {"region_name": topic, "bbox": None, "zoom": 5, "features": []}
    return {"region_name": topic, "bbox": None, "zoom": 5, "features": []}


def _features_to_locations(features: list[dict]) -> list[dict]:
    """Convert identified features to location dicts for geocoding."""
    type_map = {
        "city":                "city",
        "indigenous_territory": "region",
        "protected_area":      "region",
        "deforestation_front": "region",
        "watershed":           "region",
        "hotspot":             "region",
        "region":              "region",
        "river":               "region",
        "border":              "region",
        "site":                "city",
    }
    locations = []
    for f in features:
        locations.append({
            "name":        f.get("name", ""),
            "type":        type_map.get(f.get("type", ""), ""),
            "feature_type": f.get("type", ""),
            "description": f.get("description", ""),
            "source":      f.get("type", ""),
        })
    return locations


async def fetch_worldbank_for_choropleth(
    iso3_codes: list[str],
    indicator: str,
    year: int,
) -> Optional[dict]:
    """Fetch a single-year snapshot of a World Bank indicator for choropleth."""
    iso_str = ";".join(iso3_codes)
    url = (
        f"https://api.worldbank.org/v2/country/{iso_str}/indicator/{indicator}"
        f"?format=json&per_page=200&date={year}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, list) or len(data) < 2 or not data[1]:
            return None

        values = {}
        for point in data[1]:
            if point.get("value") is not None:
                iso3 = point.get("countryiso3code", "")
                if iso3:
                    values[iso3] = float(point["value"])

        return values if len(values) >= 3 else None
    except Exception as e:
        print(f"[map:worldbank] {indicator} failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Step 2 — Claude decides map type
# ---------------------------------------------------------------------------

PLAN_PROMPT = """Tu es un expert en cartographie pour la recherche géographique académique.

Sujet: {topic}
Données géographiques disponibles:
{data_summary}

Choisis le type de carte le plus pertinent scientifiquement.

TYPES DISPONIBLES:
- point_map   : lieux nommés sur une carte avec marqueurs — idéal si on a des lieux précis (villes, sites, etc.)
- choropleth  : remplissage par pays/région avec une valeur numérique — idéal si données quantitatives par pays
- heatmap     : densité de points — idéal si beaucoup de points concentrés géographiquement
- skip        : aucune donnée cartographiable

RÈGLES:
- point_map si: ≥3 lieux geocodables distincts
- choropleth si: ≥3 pays avec valeur numérique du même indicateur
- heatmap si: ≥10 points dans une zone géographique concentrée
- skip si: données insuffisantes ou non cartographiables

Retourne UNIQUEMENT du JSON valide:

Pour point_map:
{{
  "skip": false,
  "type": "point_map",
  "title": "Localisation des études de cas sur l'urbanisation (Afrique subsaharienne)",
  "use_locations": true
}}

Pour choropleth:
{{
  "skip": false,
  "type": "choropleth",
  "title": "Population urbaine par pays (%, 2022)",
  "indicator_label": "Population urbaine (% du total)",
  "countries": [{{"iso3": "NGA", "name": "Nigeria", "value": 52.8}}, ...],
  "colorscale": "Blues",
  "source": "Banque mondiale (2024)"
}}

Pour heatmap:
{{
  "skip": false,
  "type": "heatmap",
  "title": "Densité des lieux étudiés — Afrique subsaharienne",
  "use_locations": true
}}

Pour skip:
{{
  "skip": true,
  "skip_reason": "Moins de 3 lieux identifiés"
}}
"""


async def claude_plan_map(
    topic: str,
    locations: list[dict],
    wb_data: Optional[dict],
    client: anthropic.AsyncAnthropic,
) -> Optional[dict]:
    """Claude decides which map type to render."""
    if not locations and not wb_data:
        print("[map:plan] no geographic data available, skipping")
        return None

    # Build data summary for Claude
    lines = []
    if locations:
        lines.append(f"Lieux nommés ({len(locations)} au total):")
        for loc in locations[:15]:
            lines.append(f"  - {loc['name']} ({loc.get('type', 'inconnu')})")
        if len(locations) > 15:
            lines.append(f"  ... et {len(locations) - 15} autres")
    if wb_data:
        lines.append(f"\nDonnées Banque mondiale par pays ({len(wb_data)} pays avec valeurs):")
        for iso3, val in list(wb_data.items())[:10]:
            lines.append(f"  - {iso3}: {val:.2f}")

    data_summary = "\n".join(lines) if lines else "Aucune donnée géographique."

    prompt = PLAN_PROMPT.format(topic=topic, data_summary=data_summary)

    current_prompt = prompt
    for attempt in range(3):
        try:
            msg = await client.messages.create(
                model=CLAUDE_SMART,
                max_tokens=1200,
                messages=[{"role": "user", "content": current_prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            plan = json.loads(raw)
            break
        except json.JSONDecodeError as e:
            if attempt < 2:
                print(f"[map:plan] JSON error attempt {attempt+1}, retrying: {e}")
                current_prompt = (
                    prompt + "\n\nIMPORTANT: JSON invalide. Retourne UNIQUEMENT du JSON valide et complet."
                )
            else:
                print(f"[map:plan] JSON failed after 3 attempts: {e}")
                return None
        except Exception as e:
            print(f"[map:plan] Claude error: {e}")
            return None
    else:
        return None

    if plan.get("skip"):
        print(f"[map:plan] skipped: {plan.get('skip_reason', '—')}")
        return None

    print(f"[map:plan] type={plan.get('type')} — {plan.get('title', '')}")
    return plan


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

async def geocode_location(name: str, client: httpx.AsyncClient) -> Optional[tuple[float, float]]:
    try:
        resp = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": name, "format": "json", "limit": 1},
            headers={"User-Agent": "geographic-research-bot/1.0"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    return None


async def geocode_all(locations: list[dict]) -> list[tuple[dict, float, float]]:
    """Geocode all locations with rate-limiting."""
    geocoded: list[tuple[dict, float, float]] = []
    async with httpx.AsyncClient() as client:
        for loc in locations:
            coords = await geocode_location(loc["name"], client)
            if coords:
                geocoded.append((loc, coords[0], coords[1]))
            await asyncio.sleep(1.1)  # Nominatim rate limit
    return geocoded


# ---------------------------------------------------------------------------
# Step 3 — Renderers
# ---------------------------------------------------------------------------

async def render_point_map(
    locations: list[dict],
    topic: str,
    title: str,
    output_dir: Path,
    slug: str,
    formats: list[str],
    bbox: Optional[dict] = None,
    zoom: int = 5,
) -> dict:
    """Folium HTML + contextily PNG map of geocoded points, focused on study area."""
    try:
        import folium
    except ImportError:
        print("[map:render] folium not installed, skipping")
        return {}

    print(f"[map:render] geocoding {len(locations)} locations...")
    geocoded = await geocode_all(locations)

    if len(geocoded) < 2:
        print("[map:render] not enough geocoded locations (need ≥2)")
        return {}

    # Center: use bbox center if provided, else centroid of geocoded points
    if bbox:
        avg_lat = (bbox["lat_min"] + bbox["lat_max"]) / 2
        avg_lon = (bbox["lon_min"] + bbox["lon_max"]) / 2
    else:
        avg_lat = sum(c[1] for c in geocoded) / len(geocoded)
        avg_lon = sum(c[2] for c in geocoded) / len(geocoded)
    output_paths: dict = {}

    # Feature-type color coding (more semantic than generic type_colors)
    feature_type_colors = {
        "city":                PALETTE["primary"],
        "indigenous_territory": "#E07B39",
        "protected_area":      PALETTE["secondary"],
        "deforestation_front": "#E63946",
        "watershed":           "#4BBCD6",
        "hotspot":             PALETTE["accent"],
        "region":              PALETTE["neutral"],
        "river":               "#4BBCD6",
        "site":                PALETTE["orange"],
    }

    def _loc_color(loc: dict) -> str:
        ft = loc.get("feature_type") or loc.get("type", "")
        return feature_type_colors.get(ft, PALETTE["neutral"])

    def _loc_radius(loc: dict) -> int:
        ft = loc.get("feature_type") or loc.get("type", "")
        return {"city": 8, "indigenous_territory": 11, "protected_area": 9, "hotspot": 10}.get(ft, 7)

    # ── Folium HTML ────────────────────────────────────────────────────────
    if "html" in formats:
        m = folium.Map(location=[avg_lat, avg_lon], zoom_start=zoom, tiles="CartoDB positron")

        # If bbox provided, fit map to study area bounds
        if bbox:
            m.fit_bounds([
                [bbox["lat_min"], bbox["lon_min"]],
                [bbox["lat_max"], bbox["lon_max"]],
            ])

        for loc, lat, lon in geocoded:
            color  = _loc_color(loc)
            radius = _loc_radius(loc)
            ft     = loc.get("feature_type") or loc.get("type", "")
            desc   = loc.get("description", "")
            popup_html = (
                f"<div style='font-family:Arial;font-size:12px;min-width:160px'>"
                f"<b>{loc['name']}</b><br>"
                f"<span style='color:{color};font-size:10px;text-transform:uppercase;letter-spacing:0.5px'>{ft}</span>"
                + (f"<br><span style='color:#555;font-size:10px'>{desc}</span>" if desc else "")
                + "</div>"
            )
            folium.CircleMarker(
                location=[lat, lon], radius=radius,
                color=color, fill=True, fill_color=color, fill_opacity=0.75,
                weight=1.5,
                popup=folium.Popup(popup_html, max_width=280),
                tooltip=folium.Tooltip(loc["name"], style="font-family:Arial;font-size:11px"),
            ).add_to(m)

        # Legend
        legend_items = []
        seen_types: set[str] = set()
        for loc, _, _ in geocoded:
            ft = loc.get("feature_type") or loc.get("type", "")
            if ft and ft not in seen_types:
                seen_types.add(ft)
                color = feature_type_colors.get(ft, PALETTE["neutral"])
                label = ft.replace("_", " ").title()
                legend_items.append(
                    f'<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">'
                    f'<div style="width:12px;height:12px;border-radius:50%;background:{color}"></div>'
                    f'<span style="font-size:11px">{label}</span></div>'
                )
        if legend_items:
            legend_html = (
                f'<div style="position:fixed;bottom:30px;left:12px;background:white;'
                f'padding:10px 14px;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,0.2);'
                f'font-family:Arial;z-index:1000">'
                + "".join(legend_items)
                + "</div>"
            )
            m.get_root().html.add_child(folium.Element(legend_html))

        title_html = (
            f'<div style="position:fixed;top:12px;left:50%;transform:translateX(-50%);'
            f'background:white;padding:8px 18px;border-radius:4px;'
            f'box-shadow:0 2px 8px rgba(0,0,0,0.2);font-size:13px;'
            f'font-weight:bold;font-family:Arial;z-index:1000;color:{PALETTE["primary"]};">'
            f'{title[:70]}</div>'
        )
        m.get_root().html.add_child(folium.Element(title_html))
        html_path = output_dir / f"{slug}-map.html"
        m.save(str(html_path))
        output_paths["html"] = str(html_path)
        print(f"[map:render] saved HTML (point_map) → {html_path}")

    # ── Static PNG ─────────────────────────────────────────────────────────
    if "png" in formats:
        png_path = output_dir / f"{slug}-map.png"

        # Compute extent from bbox or from geocoded points
        lons_all = [lon for _, _, lon in geocoded]
        lats_all = [lat for _, lat, _ in geocoded]
        if bbox:
            lon_min = bbox["lon_min"]; lon_max = bbox["lon_max"]
            lat_min = bbox["lat_min"]; lat_max = bbox["lat_max"]
        else:
            pad = max(1.5, (max(lons_all) - min(lons_all)) * 0.10)
            lon_min = min(lons_all) - pad; lon_max = max(lons_all) + pad
            lat_min = min(lats_all) - pad; lat_max = max(lats_all) + pad

        ft_style = {
            "city":                {"color": PALETTE["primary"],   "size": 55,  "marker": "o", "zorder": 6},
            "indigenous_territory": {"color": "#E07B39",           "size": 80,  "marker": "^", "zorder": 5},
            "protected_area":      {"color": PALETTE["secondary"], "size": 70,  "marker": "s", "zorder": 5},
            "deforestation_front": {"color": "#E63946",            "size": 85,  "marker": "D", "zorder": 7},
            "watershed":           {"color": "#4BBCD6",            "size": 65,  "marker": "v", "zorder": 4},
            "hotspot":             {"color": PALETTE["accent"],    "size": 80,  "marker": "*", "zorder": 7},
            "site":                {"color": PALETTE["orange"],    "size": 60,  "marker": "D", "zorder": 5},
        }
        default_style = {"color": PALETTE["neutral"], "size": 50, "marker": "o", "zorder": 4}

        try:
            import cartopy.crs as ccrs
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            apply_pub_style()
            fig, ax = plt.subplots(
                figsize=(14, 10),
                subplot_kw={"projection": ccrs.PlateCarree()},
            )
            _cartopy_basemap(ax, lon_min, lon_max, lat_min, lat_max)

            seen_types: set[str] = set()
            for loc, lat, lon in geocoded:
                ft    = loc.get("feature_type") or loc.get("type", "")
                style = ft_style.get(ft, default_style)
                label = ft.replace("_", " ").title() if ft not in seen_types else "_nolegend_"
                seen_types.add(ft)
                ax.scatter(
                    lon, lat,
                    transform=ccrs.PlateCarree(),
                    c=style["color"], s=style["size"],
                    marker=style["marker"], alpha=0.88,
                    zorder=style["zorder"], label=label,
                    edgecolors="white", linewidths=0.7,
                )

            lat_span = lat_max - lat_min
            for loc, lat, lon in geocoded:
                if (loc.get("feature_type") or loc.get("type")) == "city":
                    ax.text(
                        lon, lat + lat_span * 0.013, loc["name"],
                        transform=ccrs.PlateCarree(),
                        fontsize=7, ha="center", va="bottom",
                        color=PALETTE["text"], fontweight="bold", zorder=8,
                        bbox=dict(boxstyle="round,pad=0.1", facecolor="white",
                                  alpha=0.65, edgecolor="none"),
                    )

            ax.legend(loc="lower right", fontsize=8, framealpha=0.92, edgecolor="#DDDDDD")
            ax.set_title(title, fontsize=13, fontweight="bold",
                         color=PALETTE["primary"], pad=14)
            plt.tight_layout()
            plt.savefig(png_path, dpi=300, facecolor="white")
            plt.close()
            output_paths["png"] = str(png_path)
            print(f"[map:render] saved PNG (point_map/cartopy) → {png_path}")

        except Exception as e:
            print(f"[map:render] cartopy failed ({type(e).__name__}: {e}), trying contextily")
            try:
                import contextily as ctx
                import geopandas as gpd
                import matplotlib
                matplotlib.use("Agg")
                import matplotlib.pyplot as plt
                from shapely.geometry import Point

                apply_pub_style()
                rows = [{"name": loc["name"],
                         "feature_type": loc.get("feature_type") or loc.get("type", "")}
                        for loc, _, _ in geocoded]
                gdf = gpd.GeoDataFrame(
                    rows,
                    geometry=[Point(lon, lat) for _, lat, lon in geocoded],
                    crs="EPSG:4326",
                ).to_crs(epsg=3857)

                fig, ax = plt.subplots(figsize=(12, 9))
                seen2: set[str] = set()
                for ft, style in {**ft_style, "_default": default_style}.items():
                    mask = (~gdf["feature_type"].isin(ft_style.keys())
                            if ft == "_default" else gdf["feature_type"] == ft)
                    subset = gdf[mask]
                    if subset.empty:
                        continue
                    label = ft.replace("_", " ").title() if ft != "_default" else "Autre"
                    ax.scatter(subset.geometry.x, subset.geometry.y,
                               c=style["color"], s=style["size"] * 1.4,
                               marker=style["marker"], alpha=0.85,
                               zorder=style["zorder"], label=label,
                               edgecolors="white", linewidths=0.6)
                    seen2.add(ft)
                ctx.add_basemap(ax, source=ctx.providers.CartoDB.Positron, zoom="auto")
                if bbox:
                    from pyproj import Transformer
                    t = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
                    x0, y0 = t.transform(lon_min, lat_min)
                    x1, y1 = t.transform(lon_max, lat_max)
                    ax.set_xlim(x0 - (x1-x0)*0.03, x1 + (x1-x0)*0.03)
                    ax.set_ylim(y0 - (y1-y0)*0.03, y1 + (y1-y0)*0.03)
                for _, row in gdf[gdf["feature_type"] == "city"].iterrows():
                    ax.annotate(row["name"], (row.geometry.x, row.geometry.y),
                                fontsize=7.5, ha="center", va="bottom",
                                xytext=(0, 8), textcoords="offset points",
                                color=PALETTE["text"], fontweight="bold")
                ax.set_axis_off()
                ax.set_title(title, fontsize=12, fontweight="bold",
                             color=PALETTE["primary"], pad=12)
                ax.legend(loc="lower right", fontsize=8, framealpha=0.9,
                          edgecolor="#ddd", markerscale=1.2)
                plt.tight_layout()
                plt.savefig(png_path, dpi=300, facecolor="white")
                plt.close()
                output_paths["png"] = str(png_path)
                print(f"[map:render] saved PNG (point_map/contextily) → {png_path}")
            except Exception as e2:
                print(f"[map:render] PNG rendering failed: {e2}")

    return output_paths


async def render_heatmap(
    locations: list[dict],
    topic: str,
    title: str,
    output_dir: Path,
    slug: str,
    formats: list[str],
    bbox: Optional[dict] = None,
) -> dict:
    """Folium HeatMap density visualization."""
    try:
        import folium
        from folium.plugins import HeatMap
    except ImportError:
        print("[map:render] folium not installed, falling back to point_map")
        return await render_point_map(locations, topic, title, output_dir, slug, formats)

    print(f"[map:render] geocoding {len(locations)} locations for heatmap...")
    geocoded = await geocode_all(locations)

    if len(geocoded) < 3:
        print("[map:render] not enough geocoded locations for heatmap")
        return {}

    avg_lat = sum(c[1] for c in geocoded) / len(geocoded)
    avg_lon = sum(c[2] for c in geocoded) / len(geocoded)
    output_paths: dict = {}

    heat_data = [[lat, lon] for _, lat, lon in geocoded]

    if "html" in formats:
        m = folium.Map(location=[avg_lat, avg_lon], zoom_start=5, tiles="CartoDB positron")
        n_pts = len(heat_data)
        radius = max(10, min(25, 500 // n_pts)) if n_pts > 0 else 15
        HeatMap(heat_data, radius=radius, blur=15, min_opacity=0.4).add_to(m)
        title_html = (
            f'<div style="position:fixed;top:12px;left:50%;transform:translateX(-50%);'
            f'background:white;padding:8px 18px;border-radius:4px;'
            f'box-shadow:0 2px 8px rgba(0,0,0,0.2);font-size:13px;'
            f'font-weight:bold;font-family:Arial;z-index:1000;color:{PALETTE["primary"]};">'
            f'{title[:70]}</div>'
        )
        m.get_root().html.add_child(folium.Element(title_html))
        html_path = output_dir / f"{slug}-map.html"
        m.save(str(html_path))
        output_paths["html"] = str(html_path)
        print(f"[map:render] saved HTML (heatmap) → {html_path}")

    if "png" in formats:
        lons = [lon for _, _, lon in geocoded]
        lats = [lat for _, lat, _ in geocoded]

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
            _cartopy_basemap(ax, lon_min, lon_max, lat_min, lat_max)

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
            ax.scatter(
                lons, lats,
                transform=ccrs.PlateCarree(),
                s=4, c=PALETTE["primary"], alpha=0.25, zorder=4, edgecolors="none",
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
            print(f"[map:render] saved PNG (heatmap/cartopy) → {png_path}")

        except Exception as e:
            print(f"[map:render] cartopy heatmap failed ({type(e).__name__}: {e}), fallback")
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
                print(f"[map:render] saved PNG (heatmap/contextily) → {png_path}")
            except Exception as e2:
                print(f"[map:render] heatmap PNG failed: {e2}")

    return output_paths


def render_choropleth(
    plan: dict,
    output_dir: Path,
    slug: str,
    formats: list[str],
) -> dict:
    """Plotly choropleth map for country-level data."""
    countries_data  = plan.get("countries", [])
    title           = plan.get("title", "")
    indicator_label = plan.get("indicator_label", "Valeur")
    colorscale      = plan.get("colorscale", "Blues")
    source          = plan.get("source", "")
    scope           = plan.get("scope", "world")   # e.g. "south america", "africa"
    bbox            = plan.get("bbox")              # {"lon_min":…, "lon_max":…, "lat_min":…, "lat_max":…}

    if len(countries_data) < 3:
        print("[map:render] not enough countries for choropleth")
        return {}

    output_paths: dict = {}

    try:
        import plotly.express as px
        import plotly.io as pio
        import pandas as pd

        df = pd.DataFrame(countries_data)
        if "iso3" not in df.columns or "value" not in df.columns:
            print("[map:render] choropleth missing iso3/value columns")
            return {}

        fig = px.choropleth(
            df,
            locations="iso3",
            color="value",
            hover_name="name" if "name" in df.columns else "iso3",
            color_continuous_scale=colorscale,
            labels={"value": indicator_label},
        )

        # Build geo dict — zoom to scope or bbox
        geo_dict = dict(
            showframe=False,
            showcoastlines=True,
            coastlinecolor="#AAAAAA",
            showland=True,
            landcolor="#EEF0E5",
            showocean=True,
            oceancolor="#C8E0EF",
            showcountries=True,
            countrycolor="#999999",
            countrywidth=0.8,
            showrivers=True,
            rivercolor="#7EC8D9",
            riverwidth=0.8,
            showlakes=True,
            lakecolor="#C8E0EF",
            bgcolor="white",
        )
        if scope != "world":
            geo_dict["scope"] = scope
        elif bbox:
            geo_dict["projection_type"] = "natural earth"
            geo_dict["lonaxis"] = dict(range=[bbox["lon_min"], bbox["lon_max"]])
            geo_dict["lataxis"] = dict(range=[bbox["lat_min"], bbox["lat_max"]])
        else:
            geo_dict["projection_type"] = "natural earth"
            geo_dict["fitbounds"] = "locations"

        annotations = [
            dict(
                text=title,
                xref="paper", yref="paper",
                x=0.5, y=1.04,
                xanchor="center", yanchor="bottom",
                font=dict(size=15, family="Arial Black", color=PALETTE["primary"]),
                showarrow=False,
            ),
        ]
        if source:
            annotations.append(dict(
                text=f"Source : {source}",
                xref="paper", yref="paper",
                x=1, y=-0.04, xanchor="right",
                font=dict(size=8, color="#888"),
                showarrow=False,
            ))

        fig.update_layout(
            title=None,
            font=dict(family="Arial", size=10),
            paper_bgcolor="white",
            plot_bgcolor="white",
            geo=geo_dict,
            coloraxis_colorbar=dict(
                title=dict(text=indicator_label, font=dict(size=9)),
                tickfont=dict(size=8),
                thickness=14,
                len=0.75,
                y=0.5,
            ),
            margin=dict(l=10, r=10, t=70, b=40),
            width=1000, height=600,
            annotations=annotations,
        )

        if "html" in formats:
            html_path = output_dir / f"{slug}-map.html"
            fig.write_html(str(html_path))
            output_paths["html"] = str(html_path)
            print(f"[map:render] saved HTML (choropleth) → {html_path}")

        if "png" in formats:
            png_path = output_dir / f"{slug}-map.png"
            pio.write_image(fig, str(png_path), format="png", scale=2, width=1000, height=560)
            output_paths["png"] = str(png_path)
            print(f"[map:render] saved PNG (choropleth) → {png_path}")

    except Exception as e:
        print(f"[map:render] choropleth failed ({type(e).__name__}: {e})")

    return output_paths


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_map(
    data,
    topic: str = "",
    context: Optional[dict] = None,
    output_dir: str = "./output",
    formats: Optional[list[str]] = None,
) -> dict:
    """
    Generate the best map for the given topic and data.
    Returns {"map": {"png": str, "html": str, "title": str, "type": str}} or {}.
    """
    if formats is None:
        formats = ["png", "html"]

    slug    = slugify(topic) if topic else "research"
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    records = normalize_input(data)
    print(f"\n[map] topic='{topic}', {len(records)} records")

    ai_client = anthropic.AsyncAnthropic()

    # Step 1: Claude identifies geographic features relevant to the research topic
    print("[map] Identifying geographic features for study area...")
    geo_info  = await identify_geographic_features(topic, ai_client)
    features  = geo_info.get("features", [])
    bbox      = geo_info.get("bbox")
    zoom      = geo_info.get("zoom", 5)
    locations = _features_to_locations(features)

    # Optionally fetch World Bank snapshot for choropleth (multi-country topics)
    wb_snapshot: Optional[dict] = None
    if context:
        countries  = context.get("countries", [])
        indicators = context.get("worldbank_indicators", [])
        if countries and indicators:
            iso3_list = [c["iso3"] for c in countries if c.get("iso3")]
            if iso3_list and len(iso3_list) >= 3:
                wb_snapshot = await fetch_worldbank_for_choropleth(
                    iso3_list, indicators[0], context.get("year_end", 2022) - 2
                )
                if wb_snapshot:
                    print(f"[map] World Bank snapshot: {len(wb_snapshot)} countries")

    # Step 2: Claude decides map type
    plan = await claude_plan_map(topic, locations, wb_snapshot, ai_client)
    if not plan:
        # If planning fails but we have features, still render a point map
        if len(locations) >= 2:
            plan = {"type": "point_map", "title": geo_info.get("region_name", topic)[:70]}
        else:
            print("[map] No map generated.")
            return {}

    map_type = plan.get("type", "point_map")
    title    = plan.get("title", geo_info.get("region_name", topic)[:70])

    if map_type == "choropleth" and wb_snapshot:
        # Enrich plan with actual values if Claude plan only has structure
        if not plan.get("countries") and wb_snapshot:
            plan["countries"] = [
                {"iso3": iso3, "name": iso3, "value": val}
                for iso3, val in wb_snapshot.items()
            ]
        paths = render_choropleth(plan, out_dir, slug, formats)
    elif map_type == "heatmap":
        paths = await render_heatmap(locations, topic, title, out_dir, slug, formats, bbox=bbox)
    else:
        # Default: point_map, pass bbox and zoom for focused rendering
        paths = await render_point_map(locations, topic, title, out_dir, slug, formats,
                                       bbox=bbox, zoom=zoom)

    if paths:
        result = {"map": {**paths, "title": title, "type": map_type}}
        print(f"[map] Done → {list(paths.keys())}")
        return result

    return {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="map-skill — professional geographic maps")
    parser.add_argument("--topic",      required=True)
    parser.add_argument("--data",       default=None, help="extracted.json")
    parser.add_argument("--output-dir", default="./output")
    parser.add_argument("--formats",    nargs="+", default=["png", "html"],
                        choices=["png", "html"])
    args = parser.parse_args()

    raw = []
    if args.data:
        raw = json.loads(Path(args.data).read_text())

    result = asyncio.run(run_map(
        data=raw, topic=args.topic,
        output_dir=args.output_dir,
        formats=args.formats,
    ))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
