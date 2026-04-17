"""
viz-data-fetch-skill
Fetches specific data from open APIs based on DataRequirement specs.
Each requirement specifies exactly what data is needed for a visualization idea.

Supported sources:
  - World Bank API  — socioeconomic/environmental indicators
  - GBIF            — biodiversity occurrence points
  - OWID            — Our World in Data (CO2, GHG, emissions)
  - NASA POWER      — climate data (with Open-Meteo fallback)

All output is converted to CSV format for transfer to the Claude sandbox.
"""

import asyncio
import csv as _csv
import io
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

CURRENT_YEAR = datetime.now().year


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DataRequirement:
    source: str             # "worldbank" | "gbif" | "owid" | "nasa" | "open-meteo"
    params: dict            # source-specific parameters
    role: str = "primary"   # "primary" | "secondary"
    description: str = ""


@dataclass
class FetchedData:
    requirement: DataRequirement
    success: bool
    data_csv: Optional[str] = None
    n_points: int = 0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# World Bank indicator labels (for CSV headers)
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

DATA_LABELS = {
    "T2M":         "Température moyenne annuelle (°C)",
    "PRECTOTCORR": "Précipitations annuelles (mm)",
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
# World Bank API
# ---------------------------------------------------------------------------

async def fetch_worldbank(params: dict, client: httpx.AsyncClient) -> FetchedData:
    """Fetch World Bank indicator time series, return as CSV."""
    indicator = params.get("indicator", "")
    countries_iso3 = params.get("countries_iso3", [])
    year_start = params.get("year_start", 1990)
    year_end = params.get("year_end", CURRENT_YEAR)

    if not indicator or not countries_iso3:
        return FetchedData(
            requirement=DataRequirement(source="worldbank", params=params),
            success=False, error="Missing indicator or countries"
        )

    iso_str = ";".join(countries_iso3)
    base_url = (
        f"https://api.worldbank.org/v2/country/{iso_str}/indicator/{indicator}"
        f"?format=json&per_page=1000&date={year_start}:{year_end}"
    )

    try:
        all_points: list[dict] = []
        page = 1
        while True:
            url = f"{base_url}&page={page}"
            resp = await client.get(url, timeout=45)
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
            return FetchedData(
                requirement=DataRequirement(source="worldbank", params=params),
                success=False, error="No data returned"
            )

        # Build CSV: country,iso3,year,value
        label = INDICATOR_LABELS.get(indicator, indicator)
        buf = io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(["country", "iso3", "year", "value", "indicator", "label"])
        n = 0
        for pt in all_points:
            if pt.get("value") is None:
                continue
            writer.writerow([
                pt["country"]["value"],
                pt["countryiso3code"],
                int(pt["date"]),
                float(pt["value"]),
                indicator,
                label,
            ])
            n += 1

        if n < 5:
            return FetchedData(
                requirement=DataRequirement(source="worldbank", params=params),
                success=False, error=f"Only {n} data points"
            )

        print(f"[viz-fetch:wb] {indicator} → {n} points for {len(countries_iso3)} countries")
        return FetchedData(
            requirement=DataRequirement(source="worldbank", params=params),
            success=True, data_csv=buf.getvalue(), n_points=n,
        )

    except Exception as e:
        err_msg = str(e) or type(e).__name__
        print(f"[viz-fetch:wb] {indicator} failed: {type(e).__name__}: {err_msg}")
        return FetchedData(
            requirement=DataRequirement(source="worldbank", params=params),
            success=False, error=err_msg,
        )


# ---------------------------------------------------------------------------
# GBIF species occurrences
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
        print(f"[viz-fetch:gbif] taxon resolve failed for '{taxon_name}': {e}")
    return None


async def fetch_gbif(params: dict, client: httpx.AsyncClient) -> FetchedData:
    """Fetch GBIF species occurrences, return as CSV with lat,lon,species,year,country."""
    taxon_hints = params.get("taxon_hints", [])
    bbox = params.get("bbox")
    max_records = params.get("max_records", 1500)

    query_params: dict = {
        "limit": 300,
        "hasCoordinate": "true",
        "hasGeospatialIssue": "false",
    }

    taxon_name = ""
    if taxon_hints:
        taxon_key = await _resolve_taxon_key(taxon_hints[0], client)
        if taxon_key:
            query_params["taxonKey"] = taxon_key
            taxon_name = taxon_hints[0]

    if "taxonKey" not in query_params:
        if not bbox:
            return FetchedData(
                requirement=DataRequirement(source="gbif", params=params),
                success=False, error="No taxon key and no bbox",
            )
        query_params["decimalLatitude"] = f"{bbox['lat_min']},{bbox['lat_max']}"
        query_params["decimalLongitude"] = f"{bbox['lon_min']},{bbox['lon_max']}"

    try:
        all_results: list = []
        offset = 0
        while len(all_results) < max_records:
            page_params = {**query_params, "offset": offset}
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

        if len(all_results) < 5:
            return FetchedData(
                requirement=DataRequirement(source="gbif", params=params),
                success=False, error=f"Only {len(all_results)} occurrences",
            )

        # Build CSV
        buf = io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(["lat", "lon", "species", "year", "country"])
        n = 0
        for rec in all_results:
            lat = rec.get("decimalLatitude")
            lon = rec.get("decimalLongitude")
            if lat is None or lon is None:
                continue
            writer.writerow([
                lat, lon,
                rec.get("species") or rec.get("genericName", ""),
                rec.get("year", ""),
                rec.get("countryCode", ""),
            ])
            n += 1

        print(f"[viz-fetch:gbif] {n} occurrences for '{taxon_name or 'bbox'}'")
        return FetchedData(
            requirement=DataRequirement(source="gbif", params=params),
            success=True, data_csv=buf.getvalue(), n_points=n,
        )

    except Exception as e:
        print(f"[viz-fetch:gbif] failed: {e}")
        return FetchedData(
            requirement=DataRequirement(source="gbif", params=params),
            success=False, error=str(e),
        )


# ---------------------------------------------------------------------------
# Our World in Data (CO2 / GHG / land-use)
# ---------------------------------------------------------------------------

OWID_CSV_URL = "https://raw.githubusercontent.com/owid/co2-data/master/owid-co2-data.csv"

async def fetch_owid(params: dict, client: httpx.AsyncClient) -> FetchedData:
    """Fetch OWID CO2/GHG data for specified countries and columns, return as CSV."""
    countries_iso3 = params.get("countries_iso3", [])
    columns = params.get("columns", ["co2", "co2_per_capita"])
    year_start = params.get("year_start", 1990)
    year_end = params.get("year_end", CURRENT_YEAR)

    iso_set = {c.upper() for c in countries_iso3}

    try:
        resp = await client.get(OWID_CSV_URL, timeout=20)
        resp.raise_for_status()
        lines = resp.text.splitlines()
    except Exception as e:
        return FetchedData(
            requirement=DataRequirement(source="owid", params=params),
            success=False, error=f"Download failed: {e}",
        )

    reader = _csv.DictReader(lines)

    buf = io.StringIO()
    writer = _csv.writer(buf)
    header = ["country", "iso3", "year"] + columns
    writer.writerow(header)
    n = 0

    for row in reader:
        iso = (row.get("iso_code") or "").upper()
        if not iso or iso.startswith("OWID"):
            continue
        if iso_set and iso not in iso_set:
            continue
        try:
            yr = int(row.get("year", 0))
        except (ValueError, TypeError):
            continue
        if yr < year_start or yr > year_end:
            continue

        values = []
        has_data = False
        for col in columns:
            raw = row.get(col, "")
            if raw:
                try:
                    values.append(float(raw))
                    has_data = True
                except (ValueError, TypeError):
                    values.append("")
            else:
                values.append("")

        if not has_data:
            continue

        writer.writerow([row.get("country", ""), iso, yr] + values)
        n += 1

    if n < 5:
        return FetchedData(
            requirement=DataRequirement(source="owid", params=params),
            success=False, error=f"Only {n} data points",
        )

    print(f"[viz-fetch:owid] {n} rows, columns={columns}")
    return FetchedData(
        requirement=DataRequirement(source="owid", params=params),
        success=True, data_csv=buf.getvalue(), n_points=n,
    )


# ---------------------------------------------------------------------------
# NASA POWER / Open-Meteo climate data
# ---------------------------------------------------------------------------

async def fetch_nasa(params: dict, client: httpx.AsyncClient) -> FetchedData:
    """Fetch climate data from NASA POWER, falling back to Open-Meteo."""
    lat = params.get("lat")
    lon = params.get("lon")
    year_start = max(params.get("year_start", 1990), 1981)
    year_end = min(params.get("year_end", CURRENT_YEAR), CURRENT_YEAR - 1)
    parameters = params.get("parameters", ["T2M", "PRECTOTCORR"])

    if lat is None or lon is None:
        return FetchedData(
            requirement=DataRequirement(source="nasa", params=params),
            success=False, error="Missing lat/lon",
        )

    if year_start > year_end:
        return FetchedData(
            requirement=DataRequirement(source="nasa", params=params),
            success=False, error="Invalid year range",
        )

    # Try NASA POWER first
    result = await _try_nasa_power(lat, lon, year_start, year_end, parameters, client)
    if result is not None:
        return result

    # Fallback to Open-Meteo
    return await _try_openmeteo(lat, lon, year_start, year_end, params, client)


async def _try_nasa_power(
    lat: float, lon: float, year_start: int, year_end: int,
    parameters: list[str], client: httpx.AsyncClient,
) -> Optional[FetchedData]:
    params_str = ",".join(parameters)
    url = (
        f"https://power.larc.nasa.gov/api/temporal/annual/point"
        f"?parameters={params_str}&community=AG"
        f"&longitude={lon}&latitude={lat}"
        f"&start={year_start}&end={year_end}&format=JSON"
    )
    try:
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        if "text/html" in resp.headers.get("content-type", ""):
            raise ValueError("NASA POWER returned HTML")
        data = resp.json()
        params_data = data.get("properties", {}).get("parameter", {})

        buf = io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(["year"] + parameters)
        n = 0
        years = set()
        for param_name, year_dict in params_data.items():
            for y in year_dict:
                years.add(int(y))
        for yr in sorted(years):
            row = [yr]
            has_val = False
            for p in parameters:
                v = params_data.get(p, {}).get(str(yr))
                if v is not None and v != -999.0:
                    row.append(v)
                    has_val = True
                else:
                    row.append("")
            if has_val:
                writer.writerow(row)
                n += 1

        if n < 5:
            return None

        print(f"[viz-fetch:nasa] NASA POWER → {n} years")
        return FetchedData(
            requirement=DataRequirement(source="nasa", params={"lat": lat, "lon": lon}),
            success=True, data_csv=buf.getvalue(), n_points=n,
        )
    except Exception as e:
        print(f"[viz-fetch:nasa] NASA POWER failed ({e}), trying Open-Meteo...")
        return None


async def _try_openmeteo(
    lat: float, lon: float, year_start: int, year_end: int,
    orig_params: dict, client: httpx.AsyncClient,
) -> FetchedData:
    start_date = f"{year_start}-01-01"
    end_date = f"{min(year_end, CURRENT_YEAR - 1)}-12-31"
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&daily=temperature_2m_mean,precipitation_sum"
        f"&timezone=UTC"
    )
    try:
        resp = await client.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_mean", [])
        precips = daily.get("precipitation_sum", [])

        if not dates:
            return FetchedData(
                requirement=DataRequirement(source="nasa", params=orig_params),
                success=False, error="Open-Meteo returned no data",
            )

        # Aggregate daily → annual
        year_temp: dict[int, list[float]] = {}
        year_precip: dict[int, list[float]] = {}
        for date_str, t, p in zip(dates, temps, precips):
            yr = int(date_str[:4])
            if t is not None:
                year_temp.setdefault(yr, []).append(t)
            if p is not None:
                year_precip.setdefault(yr, []).append(p)

        buf = io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(["year", "T2M", "PRECTOTCORR"])
        n = 0
        all_years = sorted(set(year_temp.keys()) | set(year_precip.keys()))
        for yr in all_years:
            t_vals = year_temp.get(yr, [])
            p_vals = year_precip.get(yr, [])
            if len(t_vals) < 300 and len(p_vals) < 300:
                continue
            t_avg = round(sum(t_vals) / len(t_vals), 2) if len(t_vals) >= 300 else ""
            p_sum = round(sum(p_vals), 1) if len(p_vals) >= 300 else ""
            writer.writerow([yr, t_avg, p_sum])
            n += 1

        if n < 5:
            return FetchedData(
                requirement=DataRequirement(source="nasa", params=orig_params),
                success=False, error=f"Open-Meteo: only {n} complete years",
            )

        print(f"[viz-fetch:nasa] Open-Meteo fallback → {n} years")
        return FetchedData(
            requirement=DataRequirement(source="nasa", params=orig_params),
            success=True, data_csv=buf.getvalue(), n_points=n,
        )
    except Exception as e:
        return FetchedData(
            requirement=DataRequirement(source="nasa", params=orig_params),
            success=False, error=f"Open-Meteo failed: {e}",
        )


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

async def fetch_viz_data(requirements: list[DataRequirement]) -> list[FetchedData]:
    """
    Fetch data for all requirements.
    World Bank requests are sequential (rate-limited), others run in parallel.
    Returns a list of FetchedData, one per requirement.
    """
    results: list[FetchedData] = [None] * len(requirements)

    # Separate WB (sequential) from others (parallel)
    wb_indices = []
    other_indices = []
    for i, req in enumerate(requirements):
        if req.source == "worldbank":
            wb_indices.append(i)
        else:
            other_indices.append(i)

    async with httpx.AsyncClient(timeout=60) as client:
        # World Bank: sequential with delay (API rate-limits parallel requests)
        for idx in wb_indices:
            req = requirements[idx]
            try:
                fd = await fetch_worldbank(req.params, client)
                fd.requirement = req
                results[idx] = fd
            except Exception as e:
                results[idx] = FetchedData(
                    requirement=req, success=False, error=str(e),
                )
            if idx != wb_indices[-1]:
                await asyncio.sleep(0.5)

        # Others: parallel
        if other_indices:
            tasks = []
            for idx in other_indices:
                req = requirements[idx]
                if req.source == "gbif":
                    tasks.append(fetch_gbif(req.params, client))
                elif req.source == "owid":
                    tasks.append(fetch_owid(req.params, client))
                elif req.source in ("nasa", "open-meteo"):
                    tasks.append(fetch_nasa(req.params, client))
                else:
                    tasks.append(_make_error(req, f"Unknown source: {req.source}"))

            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(gathered):
                idx = other_indices[i]
                if isinstance(result, Exception):
                    results[idx] = FetchedData(
                        requirement=requirements[idx],
                        success=False, error=str(result),
                    )
                else:
                    result.requirement = requirements[idx]
                    results[idx] = result

    return results


async def _make_error(req: DataRequirement, msg: str) -> FetchedData:
    return FetchedData(requirement=req, success=False, error=msg)


# ---------------------------------------------------------------------------
# CLI for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test viz-data-fetch-skill")
    parser.add_argument("--source", required=True, choices=["worldbank", "gbif", "owid", "nasa"])
    parser.add_argument("--indicator", default="SP.DYN.LE00.IN")
    parser.add_argument("--countries", default="NGA,KEN,ZAF")
    parser.add_argument("--taxon", default="Panthera onca")
    args = parser.parse_args()

    req = DataRequirement(source=args.source, params={})
    if args.source == "worldbank":
        req.params = {
            "indicator": args.indicator,
            "countries_iso3": args.countries.split(","),
            "year_start": 1990,
            "year_end": CURRENT_YEAR,
        }
    elif args.source == "gbif":
        req.params = {"taxon_hints": [args.taxon]}
    elif args.source == "owid":
        req.params = {
            "countries_iso3": args.countries.split(","),
            "columns": ["co2", "co2_per_capita"],
        }
    elif args.source == "nasa":
        req.params = {"lat": 45.5, "lon": -73.6}

    async def _test():
        results = await fetch_viz_data([req])
        for r in results:
            print(f"Success: {r.success}, Points: {r.n_points}")
            if r.error:
                print(f"Error: {r.error}")
            if r.data_csv:
                lines = r.data_csv.strip().split("\n")
                print(f"CSV rows: {len(lines)} (header + {len(lines)-1} data)")
                for line in lines[:5]:
                    print(f"  {line}")

    asyncio.run(_test())
