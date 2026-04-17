"""
system-1-research-synthesis-system
Full AI research pipeline chaining 6 specialized skills:

  1. notebooklm-research-skill  — find academic sources
  2. domain-analysis-skill      — classify + extract by domain (parallel agents)
  3. debate-generation-skill    — map intellectual landscape
  4. data-scout-skill           — hunt open data (GBIF, World Bank, FAOSTAT, NASA) + render figures
  5. text-writing-skill         — write scientific text in French
  6. pdf-rendering-skill        — assemble professional PDF


# ============================================================================
# EXPORTED VERSION: This copy was exported from the main project.
#
# For local development, use: systems/system-1-research-synthesis-system/main.py
# which can load from both .system/skills/ and ../../.claude/skills/.
#
# For GitHub users: All required skills are in .system/skills/
# No external dependencies needed.
# ============================================================================

Usage:
    python main.py --topic "urban heat islands Montreal"
    python main.py --topic "permafrost thaw Arctic" --max-sources 30 --report-format article
    python main.py --topic "..." --resume-from research-projects/my-run-20260322/
"""

import argparse
import asyncio
import importlib.util
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SYSTEM_DIR   = Path(__file__).parent.resolve()
PROJECT_ROOT = SYSTEM_DIR.parent.parent

# Try bundled skills first (.system/skills for GitHub), then project-level skills (.claude/skills for local dev)
BUNDLED_SKILLS_DIR = SYSTEM_DIR / ".system" / "skills"
PROJECT_SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"


# ---------------------------------------------------------------------------
# Skill loader
# ---------------------------------------------------------------------------

def load_skill(skill_dir_name: str, function_name: str):
    """Import a function from a skill's main.py by file path.

    Priority:
    1. .system/skills/ (bundled for export)
    2. .claude/skills/ (local development)
    """
    # Try bundled skills first
    skill_path = BUNDLED_SKILLS_DIR / skill_dir_name / "main.py"
    if not skill_path.exists():
        # Fall back to project-level skills
        skill_path = PROJECT_SKILLS_DIR / skill_dir_name / "main.py"

    if not skill_path.exists():
        raise FileNotFoundError(
            f"Skill '{skill_dir_name}' not found in:\n"
            f"  - {BUNDLED_SKILLS_DIR / skill_dir_name}\n"
            f"  - {PROJECT_SKILLS_DIR / skill_dir_name}"
        )

    module_name = skill_dir_name.replace("-", "_")
    spec        = importlib.util.spec_from_file_location(module_name, skill_path)
    module      = importlib.util.module_from_spec(spec)
    skill_dir   = str(skill_path.parent)
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    spec.loader.exec_module(module)
    return getattr(module, function_name)


# Lazy-load all skills at startup
run_research       = load_skill("notebooklm-research-skill", "run_research")
run_domain_analysis = load_skill("domain-analysis-skill",    "run_domain_analysis")
run_debate         = load_skill("debate-generation-skill",   "run_debate")
run_viz_pipeline   = load_skill("viz-orchestrator-skill",    "run_viz_pipeline")
run_text_writing   = load_skill("text-writing-skill",        "run_text_writing")
run_pdf_rendering  = load_skill("pdf-rendering-skill",       "run_pdf_rendering")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(overrides: dict) -> dict:
    config_path = SYSTEM_DIR / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    for key, value in overrides.items():
        if value is not None:
            config[key] = value
    return config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:50]


def create_results_dir(topic: str, resume_from: Optional[str] = None) -> Path:
    if resume_from:
        results_dir = Path(resume_from).resolve()
        if not results_dir.exists():
            raise FileNotFoundError(f"--resume-from path not found: {results_dir}")
        print(f"[pipeline] Resuming from: {results_dir}")
    else:
        slug        = slugify(topic)
        timestamp   = datetime.now().strftime("%Y%m%d-%H%M%S")
        results_dir = PROJECT_ROOT / "research-projects" / f"{slug}-{timestamp}"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "output").mkdir(exist_ok=True)
    (results_dir / "output" / "figures").mkdir(exist_ok=True)
    return results_dir


def _load_if_done(path: Path) -> Optional[any]:
    """Return parsed JSON from path if it exists and is non-empty, else None."""
    if path.exists() and path.stat().st_size > 10:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            print(f"[checkpoint] Loaded: {path.name} — skipping step")
            return data
        except Exception:
            pass
    return None


def step(n: int, total: int, label: str):
    filled = "█" * n + "░" * (total - n)
    print(f"\n[{filled}] Step {n}/{total} — {label}")
    print("-" * 60)


def _make_figures_summary(chart_result: dict, chart2_result: dict, map_result: dict) -> dict:
    """Build a figures_summary dict for text-writing-skill."""
    summary: dict = {}
    if chart_result.get("chart"):
        summary["chart"] = {
            "title": chart_result.get("title", "Visualisation des données"),
            "type":  chart_result.get("type", "chart"),
            "path":  chart_result["chart"],
        }
    if chart2_result.get("chart2"):
        summary["chart2"] = {
            "title": chart2_result.get("title2", "Visualisation comparative"),
            "type":  chart2_result.get("type2", "chart"),
            "path":  chart2_result["chart2"],
        }
    if map_result.get("map"):
        map_info = map_result["map"]
        summary["map"] = {
            "title": map_info.get("title", "Carte de couverture géographique"),
            "type":  map_info.get("type", "map"),
            "path":  map_info.get("png") or map_info.get("html") or "",
        }
    return summary


def _scout_to_chart_map(scout: dict) -> tuple[dict, dict, dict]:
    """Convert data-scout-skill output → (chart_result, chart2_result, map_result) format."""
    viz_plan    = scout.get("viz_plan", {})
    chart_plan  = viz_plan.get("chart") or {}
    chart2_plan = viz_plan.get("chart2") or {}
    chart_result: dict = {}
    if scout.get("chart"):
        chart_result = {
            "chart": scout["chart"],
            "title": chart_plan.get("title", "Visualisation des données"),
            "type":  chart_plan.get("viz_type", "chart"),
        }
    chart2_result: dict = {}
    if scout.get("chart2"):
        chart2_result = {
            "chart2": scout["chart2"],
            "title2": chart2_plan.get("title", "Visualisation comparative"),
            "type2":  chart2_plan.get("viz_type", "chart"),
        }
    map_result: dict = {}
    if scout.get("map"):
        map_result = {"map": scout["map"]}
    return chart_result, chart2_result, map_result


def _build_figures_for_pdf(chart_result: dict, chart2_result: dict, map_result: dict) -> dict:
    """Build figures dict for pdf-rendering-skill."""
    figures: dict = {}
    if chart_result.get("chart"):
        figures["chart"] = chart_result["chart"]
    if chart2_result.get("chart2"):
        figures["chart2"] = chart2_result["chart2"]
    if map_result.get("map"):
        map_info = map_result["map"]
        if isinstance(map_info, dict):
            figures["map"] = map_info
        else:
            figures["map"] = {"png": str(map_info)}
    return figures


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def run_pipeline(
    topic: str,
    config: dict,
    resume_from: Optional[str] = None,
) -> dict:
    start_time  = time.time()
    results_dir = create_results_dir(topic, resume_from)
    fig_dir     = results_dir / "output" / "figures"

    print(f"\n{'='*60}")
    print(f"  Research Synthesis System v2")
    print(f"  Topic : {topic}")
    print(f"  Output: {results_dir}")
    print(f"{'='*60}")

    TOTAL_STEPS = 6

    # ------------------------------------------------------------------
    # Step 1: Research
    # ------------------------------------------------------------------
    step(1, TOTAL_STEPS, "Finding sources (Semantic Scholar + arXiv + NotebookLM)")
    research = _load_if_done(results_dir / "research.json")
    if research is None:
        research = await run_research(
            topic=topic,
            max_sources=config["max_sources"],
            min_year=config["min_year"],
            peer_reviewed_only=config["peer_reviewed_only"],
            output_path=str(results_dir / "research.json"),
        )

    # ------------------------------------------------------------------
    # Step 2: Domain Analysis
    # ------------------------------------------------------------------
    step(2, TOTAL_STEPS, "Domain agents extracting specialized data (parallel)")
    extractions = _load_if_done(results_dir / "extracted.json")
    if extractions is None:
        top_sources = [
            s for s in research.get("sources", [])
            if s.get("url") or s.get("title")
        ][:config["max_domain_sources"]]

        fallback_texts = {
            s["url"]: (s.get("summary") or s.get("abstract") or "")
            for s in top_sources
            if s.get("url") and (s.get("summary") or s.get("abstract"))
        }

        if top_sources:
            extractions = await run_domain_analysis(
                sources=top_sources,
                topic=topic,
                output_path=str(results_dir / "extracted.json"),
                active_domains=config.get("domain_agents", ["environmental", "social", "economic", "spatial"]),
                fallback_texts=fallback_texts,
            )
        else:
            print("[pipeline] No sources for domain analysis, skipping")
            extractions = []
            (results_dir / "extracted.json").write_text("[]")

    # ------------------------------------------------------------------
    # Step 3: Debate
    # ------------------------------------------------------------------
    step(3, TOTAL_STEPS, "Generating intellectual landscape (debate)")
    debate = _load_if_done(results_dir / "debate.json")
    if debate is None:
        debate_input = extractions if extractions else research.get("sources", [])
        debate = await run_debate(
            sources=debate_input,
            topic=topic,
            n_perspectives=config["n_perspectives"],
            output_path=str(results_dir / "debate.json"),
        )

    # ------------------------------------------------------------------
    # Step 4: Data scout — hunt open data + render figures
    # ------------------------------------------------------------------
    step(4, TOTAL_STEPS, "Idea-first visualization pipeline (viz-orchestrator)")

    scout_checkpoint = _load_if_done(fig_dir / "scout.json")
    if scout_checkpoint:
        chart_result, chart2_result, map_result = _scout_to_chart_map(scout_checkpoint)
    else:
        scout_result = await run_viz_pipeline(
            topic=topic,
            output_dir=str(fig_dir),
            formats=config.get("map_formats", ["png", "html"]),
        )
        scout_result = scout_result or {}
        (fig_dir / "scout.json").write_text(
            json.dumps(scout_result, indent=2, ensure_ascii=False)
        )
        chart_result, chart2_result, map_result = _scout_to_chart_map(scout_result)

    chart_result  = chart_result  or {}
    chart2_result = chart2_result or {}
    map_result    = map_result    or {}

    generated_figs = []
    if chart_result.get("chart"):
        generated_figs.append("chart")
    if chart2_result.get("chart2"):
        generated_figs.append("chart2")
    if map_result.get("map"):
        generated_figs.append("map")

    # ------------------------------------------------------------------
    # Step 5: Text writing
    # ------------------------------------------------------------------
    step(5, TOTAL_STEPS, f"Writing {config['report_format']} report text")
    text_checkpoint = _load_if_done(results_dir / "output" / "report_text.json")
    if text_checkpoint:
        title      = text_checkpoint["title"]
        report_md  = text_checkpoint["markdown"]
        keywords   = text_checkpoint.get("keywords", [])
        word_count = text_checkpoint.get("word_count", 0)
    else:
        figures_summary = _make_figures_summary(chart_result, chart2_result, map_result)
        text_result = await run_text_writing(
            topic=topic,
            research=research,
            extractions=extractions if extractions else None,
            debate=debate,
            format=config["report_format"],
            figures_summary=figures_summary,
            output_path=str(results_dir / "output" / "report.md"),
        )
        title      = text_result["title"]
        report_md  = text_result["markdown"]
        keywords   = text_result.get("keywords", [])
        word_count = text_result.get("word_count", 0)
        (results_dir / "output" / "report_text.json").write_text(
            json.dumps(text_result, indent=2, ensure_ascii=False)
        )

    # ------------------------------------------------------------------
    # Step 6: PDF rendering
    # ------------------------------------------------------------------
    step(6, TOTAL_STEPS, "Rendering professional PDF")
    figures_for_pdf = _build_figures_for_pdf(chart_result, chart2_result, map_result)
    pdf_result = run_pdf_rendering(
        title=title,
        markdown=report_md,
        figures=figures_for_pdf,
        output_path=str(results_dir / "output" / "report.pdf"),
        keywords=keywords,
        format=config["report_format"],
        institution="Université de Montréal",
    )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    elapsed = round(time.time() - start_time, 1)
    summary = {
        "topic":         topic,
        "generated_at":  datetime.now().isoformat(timespec="seconds"),
        "elapsed_seconds": elapsed,
        "config":        config,
        "output_dir":    str(results_dir / "output"),
        "project_dir":   str(results_dir),
        "files": {
            "research":   str(results_dir / "research.json"),
            "extracted":  str(results_dir / "extracted.json"),
            "debate":     str(results_dir / "debate.json"),
            "report_pdf": pdf_result["pdf_path"],
            "report_md":  str(results_dir / "output" / "report.md"),
            "chart":      chart_result.get("chart"),
            "chart2":     chart2_result.get("chart2"),
            "map":        map_result.get("map"),
        },
        "stats": {
            "sources_found":     research.get("total_sources", 0),
            "records_extracted": len(extractions),
            "perspectives":      len(debate.get("perspectives", [])),
            "contradictions":    len(debate.get("contradictions", [])),
            "report_words":      word_count,
            "figures_generated": generated_figs,
        },
    }
    (results_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False)
    )

    print(f"\n{'='*60}")
    print(f"  DONE in {elapsed}s")
    print(f"  Sources found    : {summary['stats']['sources_found']}")
    print(f"  Records extracted: {summary['stats']['records_extracted']}")
    print(f"  Perspectives     : {summary['stats']['perspectives']}")
    print(f"  Report words     : {summary['stats']['report_words']}")
    print(f"  Figures          : {', '.join(generated_figs) or 'none'}")
    print(f"\n  Projet  → {results_dir}")
    print(f"  Output  → {results_dir / 'output'}")
    print(f"{'='*60}\n")

    return summary


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Mode presets
# ---------------------------------------------------------------------------

MODE_PRESETS: dict[str, dict] = {
    "standard": {},  # no overrides — use config.yaml defaults
    "deep": {
        "max_sources":        300,
        "max_domain_sources": 60,
        "n_perspectives":     5,
        "report_format":      "deep",
    },
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Research Synthesis System v2 — full pipeline"
    )
    parser.add_argument("--topic",                  required=True,
                        help="Research topic (e.g. 'urban heat islands Montreal')")
    parser.add_argument("--mode",                   default="standard",
                        choices=["standard", "deep"],
                        help="Research depth: standard (50 sources) or deep (300 sources)")
    parser.add_argument("--max-sources",            type=int, default=None)
    parser.add_argument("--min-year",               type=int, default=None)
    parser.add_argument("--peer-reviewed-only",     action="store_true", default=None)
    parser.add_argument("--max-domain-sources",     type=int, default=None)
    parser.add_argument("--domain-agents",          nargs="+", default=None,
                        choices=["environmental", "social", "economic", "spatial", "political", "health"])
    parser.add_argument("--n-perspectives",         type=int, default=None)
    parser.add_argument("--report-format",          default=None,
                        choices=["academic", "article", "brief", "deep"])
    parser.add_argument("--resume-from",            default=None,
                        help="Resume from an existing results directory")
    args = parser.parse_args()

    # Apply mode preset first, then explicit CLI flags override
    overrides = dict(MODE_PRESETS.get(args.mode, {}))
    cli_overrides = {
        "max_sources":          args.max_sources,
        "min_year":             args.min_year,
        "peer_reviewed_only":   args.peer_reviewed_only or None,
        "max_domain_sources":   args.max_domain_sources,
        "domain_agents":        args.domain_agents,
        "n_perspectives":       args.n_perspectives,
        "report_format":        args.report_format,
    }
    overrides.update({k: v for k, v in cli_overrides.items() if v is not None})

    if args.mode != "standard":
        print(f"[pipeline] Mode: {args.mode.upper()}")

    config = load_config(overrides)
    asyncio.run(run_pipeline(args.topic, config, resume_from=args.resume_from))


if __name__ == "__main__":
    main()
