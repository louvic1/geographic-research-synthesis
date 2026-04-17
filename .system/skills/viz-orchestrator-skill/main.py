"""
viz-orchestrator-skill
Coordinates the idea-first visualization pipeline:
  1. viz-ideation-skill   → brainstorm visualization ideas
  2. viz-data-fetch-skill → fetch exactly the data needed
  3. viz-render-skill     → render via Claude sandbox (PNG) + Folium (HTML)

Replaces data-scout-skill as step 4 of the system-1 pipeline.
Output format is compatible with scout.json (same dict structure).

Usage:
    result = await run_viz_pipeline(
        topic="Espérance de vie en Afrique subsaharienne",
        output_dir="./output/figures",
        formats=["png", "html"],
    )
"""

import asyncio
import importlib.util
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Load sibling skills via importlib
# ---------------------------------------------------------------------------

SKILLS_DIR = Path(__file__).parent.parent


def _load_from_skill(skill_name: str, *attr_names: str) -> tuple:
    """Import named attributes from a sibling skill's main.py."""
    skill_path = SKILLS_DIR / skill_name / "main.py"
    module_name = skill_name.replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, skill_path)
    module = importlib.util.module_from_spec(spec)
    skill_dir = str(SKILLS_DIR / skill_name)
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    spec.loader.exec_module(module)
    return tuple(getattr(module, name) for name in attr_names)


# Import from sibling skills
(ideate_visualizations, VizIdea, DataRequirement_ideation) = _load_from_skill(
    "viz-ideation-skill",
    "ideate_visualizations", "VizIdea", "DataRequirement",
)

(fetch_viz_data, DataRequirement_fetch, FetchedData) = _load_from_skill(
    "viz-data-fetch-skill",
    "fetch_viz_data", "DataRequirement", "FetchedData",
)

(render_visualization, RenderResult) = _load_from_skill(
    "viz-render-skill",
    "render_visualization", "RenderResult",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:50]


def _req_key(req) -> str:
    """Unique key for a DataRequirement to deduplicate."""
    return json.dumps({"source": req.source, "params": req.params}, sort_keys=True)


def _deduplicate_requirements(all_reqs: list) -> list:
    """Remove duplicate DataRequirements (same source + params)."""
    seen = set()
    unique = []
    for req in all_reqs:
        key = _req_key(req)
        if key not in seen:
            seen.add(key)
            unique.append(req)
    return unique


def _convert_requirement(req_ideation) -> object:
    """Convert ideation DataRequirement to fetch DataRequirement."""
    return DataRequirement_fetch(
        source=req_ideation.source,
        params=req_ideation.params,
        role=req_ideation.role,
        description=req_ideation.description,
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run_viz_pipeline(
    topic: str,
    output_dir: str = "./output",
    formats: Optional[list[str]] = None,
) -> dict:
    """
    Run the idea-first visualization pipeline.
    Returns a dict compatible with data-scout-skill's scout.json format.
    """
    if formats is None:
        formats = ["png", "html"]

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(topic)

    print(f"\n{'='*60}")
    print(f"[viz-pipeline] Starting idea-first visualization")
    print(f"[viz-pipeline] Topic: {topic}")
    print(f"{'='*60}\n")

    # ------------------------------------------------------------------
    # Step 1: Ideation — brainstorm visualization ideas
    # ------------------------------------------------------------------
    print("[viz-pipeline] Step 1/3: Ideation...")
    ideas = await ideate_visualizations(topic, max_ideas=3)

    if not ideas:
        print("[viz-pipeline] No ideas generated, returning empty")
        return {"chart": None, "chart2": None, "map": None,
                "datasets": {}, "sources_queried": [], "viz_plan": {}}

    print(f"[viz-pipeline] {len(ideas)} ideas generated:")
    for idea in ideas:
        print(f"  [{idea.id}] {idea.category}: {idea.title_fr}")

    # ------------------------------------------------------------------
    # Step 2: Data fetching — get exactly the data each idea needs
    # ------------------------------------------------------------------
    print("\n[viz-pipeline] Step 2/3: Fetching data...")

    # Collect and deduplicate all requirements
    all_reqs = []
    for idea in ideas:
        if idea.data_requirements:
            all_reqs.extend(idea.data_requirements)
    unique_reqs = _deduplicate_requirements(all_reqs)

    # Convert to fetch-skill DataRequirements
    fetch_reqs = [_convert_requirement(r) for r in unique_reqs]

    # Fetch in parallel
    fetched = await fetch_viz_data(fetch_reqs)

    # Build lookup: req_key → FetchedData
    fetched_map: dict[str, object] = {}
    for fd in fetched:
        key = _req_key(fd.requirement)
        fetched_map[key] = fd

    # Check viability of each idea
    viable_ideas = []
    for idea in ideas:
        if not idea.data_requirements:
            continue
        all_ok = True
        for req in idea.data_requirements:
            key = _req_key(req)
            fd = fetched_map.get(key)
            if not fd or not fd.success:
                all_ok = False
                break
        if all_ok:
            viable_ideas.append(idea)
        else:
            print(f"[viz-pipeline] Idea {idea.id} not viable (missing data)")

    if not viable_ideas:
        print("[viz-pipeline] No viable ideas — all data fetches failed")
        return {"chart": None, "chart2": None, "map": None,
                "datasets": {}, "sources_queried": [],
                "viz_plan": {"ideas": [asdict(i) for i in ideas]}}

    print(f"[viz-pipeline] {len(viable_ideas)} viable ideas")

    # ------------------------------------------------------------------
    # Step 3: Rendering — render each viable idea
    # ------------------------------------------------------------------
    print("\n[viz-pipeline] Step 3/3: Rendering...")

    chart_result: dict = {}
    chart2_result: dict = {}
    map_result: dict = {}

    for i, idea in enumerate(viable_ideas):
        # Wait between renders to avoid API rate limits
        if i > 0:
            print("[viz-pipeline] Waiting 15s between renders (rate limit)...")
            await asyncio.sleep(15)

        # Gather the fetched data for this idea
        idea_data = []
        for req in idea.data_requirements:
            key = _req_key(req)
            fd = fetched_map.get(key)
            if fd:
                idea_data.append(fd)

        if idea.category == "chart":
            if not chart_result:
                result = await render_visualization(
                    idea, idea_data, out_dir, slug, formats=["png"],
                )
                if result.success and result.png_path:
                    chart_result = {
                        "chart": result.png_path,
                        "title": result.title,
                        "type": result.viz_type,
                    }
                    print(f"[viz-pipeline] Chart rendered: {result.png_path}")
            elif not chart2_result:
                result = await render_visualization(
                    idea, idea_data, out_dir, f"{slug}-2", formats=["png"],
                )
                if result.success and result.png_path:
                    chart2_result = {
                        "chart2": result.png_path,
                        "title2": result.title,
                        "type2": result.viz_type,
                    }
                    print(f"[viz-pipeline] Chart2 rendered: {result.png_path}")

        elif idea.category in ("map", "choropleth"):
            if not map_result:
                result = await render_visualization(
                    idea, idea_data, out_dir, slug, formats=formats,
                )
                if result.success:
                    map_info: dict = {
                        "title": result.title,
                        "type": result.viz_type,
                    }
                    if result.png_path:
                        map_info["png"] = result.png_path
                    if result.html_path:
                        map_info["html"] = result.html_path
                    map_result = {"map": map_info}
                    print(f"[viz-pipeline] Map rendered: png={result.png_path}, html={result.html_path}")

    # ------------------------------------------------------------------
    # Assemble output (compatible with scout.json format)
    # ------------------------------------------------------------------
    sources_queried = list({
        fd.requirement.source for fd in fetched
        if fd.success
    })

    output = {
        "chart":  chart_result.get("chart") if chart_result else None,
        "chart2": chart2_result.get("chart2") if chart2_result else None,
        "map":    map_result.get("map") if map_result else None,
        "datasets": {},
        "sources_queried": sources_queried,
        "viz_plan": {
            "ideas": [asdict(i) for i in ideas],
            "chart": {
                "title": chart_result.get("title", ""),
                "viz_type": chart_result.get("type", ""),
            } if chart_result else None,
            "chart2": {
                "title": chart2_result.get("title2", ""),
                "viz_type": chart2_result.get("type2", ""),
            } if chart2_result else None,
            "map": {
                "title": map_result.get("map", {}).get("title", ""),
                "viz_type": map_result.get("map", {}).get("type", ""),
            } if map_result else None,
        },
    }

    print(f"\n{'='*60}")
    print(f"[viz-pipeline] Done!")
    print(f"  Chart:  {output['chart'] or 'none'}")
    print(f"  Chart2: {output['chart2'] or 'none'}")
    print(f"  Map:    {output['map'] or 'none'}")
    print(f"{'='*60}\n")

    return output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run viz-orchestrator pipeline")
    parser.add_argument("--topic", required=True)
    parser.add_argument("--output-dir", default="./output/figures")
    parser.add_argument("--formats", nargs="+", default=["png", "html"])
    args = parser.parse_args()

    async def _main():
        result = await run_viz_pipeline(
            topic=args.topic,
            output_dir=args.output_dir,
            formats=args.formats,
        )
        print("\nResult:")
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    asyncio.run(_main())
