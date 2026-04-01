"""
pdf-rendering-skill
Pure layout engine — no Claude calls.
Takes markdown text + figure paths → professional academic PDF.

Role: layout and rendering only. Text must come pre-written from text-writing-skill.
Uses an external style.css for clean separation of styling from logic.

Usage:
    python main.py --title "Titre du rapport" \\
                   --markdown report.md \\
                   --figures figures.json \\
                   --output report.pdf \\
                   --keywords "urbanisation, Afrique, métropolisation" \\
                   --format academic
"""

import argparse
import base64
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import markdown as md_lib
from dotenv import load_dotenv

load_dotenv()

SKILL_DIR = Path(__file__).parent.resolve()
CSS_PATH  = SKILL_DIR / "style.css"

FRENCH_MONTHS = [
    "", "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]

# Map figure key → target section heading keywords (French, lowercase)
FIGURE_TARGETS: dict[str, list[str]] = {
    "chart":    ["résultats", "données", "tendances", "résultat"],
    "chart2":   ["discussion", "analyse", "comparaison", "implications"],
    "map":      ["revue de littérature", "introduction", "contexte", "géographique"],
    "timeline": ["introduction", "contexte", "revue"],
}

FIGURE_CAPTIONS: dict[str, str] = {
    "chart":  "Figure {n} — Visualisation des données quantitatives (Banque mondiale / NASA POWER).",
    "chart2": "Figure {n} — Analyse comparative des données (sources multiples).",
    "map":    "Figure {n} — Cartographie de la couverture géographique des sources analysées.",
}


# ---------------------------------------------------------------------------
# Helper: slugify headings for TOC anchors
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:60]


def _add_heading_ids(html: str) -> str:
    """Add unique id attributes to all h2 and h3 tags for TOC anchor links."""
    counter: dict[str, int] = {}

    def _replace(match):
        tag   = match.group(1)
        attrs = match.group(2)
        text  = re.sub(r"<[^>]+>", "", match.group(3))
        sid   = _slug(text)
        n     = counter.get(sid, 0)
        counter[sid] = n + 1
        unique_id = sid if n == 0 else f"{sid}-{n}"
        if "id=" not in attrs:
            attrs = f' id="{unique_id}"' + attrs
        return f"<{tag}{attrs}>{match.group(3)}</{tag}>"

    return re.sub(r"<(h[23])([^>]*)>(.*?)</\1>", _replace, html, flags=re.DOTALL)


def _build_toc(body_html: str) -> str:
    """Build a nested TOC from h2/h3 headings in the HTML body."""
    headings = re.findall(r'<(h[23])[^>]*id="([^"]+)"[^>]*>(.*?)</\1>', body_html, re.DOTALL)
    if not headings:
        return ""

    items: list[str] = []
    for tag, hid, text in headings:
        clean = re.sub(r"<[^>]+>", "", text).strip()
        if tag == "h2":
            items.append(f'<li><a href="#{hid}">{clean}</a></li>')
        else:
            items.append(f'<ul><li><a href="#{hid}">{clean}</a></li></ul>')

    toc_html = (
        '<div class="toc-page">'
        '<h2>Table des matières</h2>'
        '<ul>' + "\n".join(items) + "</ul>"
        "</div>"
    )
    return toc_html


# ---------------------------------------------------------------------------
# Figure injection
# ---------------------------------------------------------------------------

def _png_to_base64(path_str: str) -> Optional[str]:
    """Convert a PNG file to base64 data URI."""
    path = Path(path_str) if not isinstance(path_str, Path) else path_str
    if not path.exists() or path.suffix.lower() != ".png":
        return None
    try:
        data = path.read_bytes()
        return "data:image/png;base64," + base64.b64encode(data).decode()
    except Exception:
        return None


def _make_figure_html(b64: str, caption: str) -> str:
    return (
        f'<div class="figure-block">'
        f'<img src="{b64}" alt="{caption}" />'
        f'<p class="figure-caption">{caption}</p>'
        f"</div>"
    )


def _inject_figures_inline(body_html: str, figures: dict) -> str:
    """
    Inject figure images after the first </p> of their target section.
    Figures not injected inline are collected and appended at the end.
    """
    fig_num = 1
    remaining: list[tuple[str, str]] = []  # (caption, html)

    for fig_key, fig_value in figures.items():
        # Resolve PNG path
        if isinstance(fig_value, dict):
            png_path = fig_value.get("png") or fig_value.get("chart")
        elif isinstance(fig_value, str) and fig_value.endswith(".png"):
            png_path = fig_value
        else:
            continue

        b64 = _png_to_base64(png_path)
        if not b64:
            continue

        caption_tpl = FIGURE_CAPTIONS.get(fig_key, f"Figure {{n}} — {fig_key}.")
        caption = caption_tpl.format(n=fig_num)
        fig_html = _make_figure_html(b64, caption)
        fig_num += 1

        # Find target section
        targets = FIGURE_TARGETS.get(fig_key, [])
        injected = False
        for target_word in targets:
            # Find h2 heading containing the target word (case-insensitive)
            pattern = re.compile(
                rf'(<h2[^>]*>(?:(?!<h2).)*?{re.escape(target_word)}(?:(?!<h2).)*?</h2>)',
                re.IGNORECASE | re.DOTALL,
            )
            match = pattern.search(body_html)
            if match:
                # Inject after the first </p> following this heading
                insert_start = match.end()
                para_match = re.search(r"</p>", body_html[insert_start:])
                if para_match:
                    pos = insert_start + para_match.end()
                    body_html = body_html[:pos] + "\n" + fig_html + "\n" + body_html[pos:]
                    injected = True
                    break

        if not injected:
            remaining.append((caption, fig_html))

    # Append remaining figures at end
    if remaining:
        appendix = '<h2>Annexe — Figures supplémentaires</h2>\n'
        appendix += "\n".join(fh for _, fh in remaining)
        body_html += "\n" + appendix

    return body_html


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------

def _build_html(
    title: str,
    markdown_text: str,
    figures: dict,
    keywords: list[str],
    format: str,
    institution: str = "Université de Montréal",
) -> str:
    """Convert markdown + figures to a complete HTML document for WeasyPrint."""

    # Load CSS from external file
    css_content = CSS_PATH.read_text(encoding="utf-8")

    # Markdown → HTML
    body_html = md_lib.markdown(
        markdown_text,
        extensions=["tables", "fenced_code", "attr_list", "nl2br"],
    )

    # Add heading IDs for TOC
    body_html = _add_heading_ids(body_html)

    # Inject figures inline
    if figures:
        body_html = _inject_figures_inline(body_html, figures)

    # Wrap references section
    body_html = re.sub(
        r'(<h2[^>]*>\s*R[eé]f[eé]rences\s*</h2>)',
        r'<div class="references">\1',
        body_html,
        flags=re.IGNORECASE,
    )
    if '<div class="references">' in body_html:
        body_html += "</div>"

    # Build TOC
    toc_html = _build_toc(body_html)

    # Date
    now = datetime.now()
    date_str = f"{now.day} {FRENCH_MONTHS[now.month]} {now.year}"

    # Keywords line
    kw_line = ", ".join(keywords) if keywords else ""

    # Subtitle based on format
    subtitle_map = {
        "academic": "Rapport de recherche académique",
        "article":  "Article de recherche",
        "brief":    "Synthèse de recherche",
    }
    subtitle = subtitle_map.get(format, "Rapport de recherche")

    # Title page
    title_page_html = f"""
<div class="title-page">
    <div class="top-bar"></div>
    <h1>{title}</h1>
    <p class="subtitle">{subtitle}</p>
    <div class="title-spacer"></div>
    <div class="bottom-meta">
        <div>{institution}</div>
        <div>Geographic AI Research System</div>
        <div>{date_str}</div>
        {f'<div class="keywords">Mots-clés : {kw_line}</div>' if kw_line else ''}
    </div>
</div>
"""

    full_html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
{css_content}
</style>
</head>
<body>
{title_page_html}
{toc_html}
<div class="content">
{body_html}
</div>
</body>
</html>"""

    return full_html


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_pdf_rendering(
    title: str,
    markdown: str,
    figures: Optional[dict] = None,
    output_path: str = "report.pdf",
    keywords: Optional[list[str]] = None,
    format: str = "academic",
    institution: str = "Université de Montréal",
) -> dict:
    """
    Render markdown text + figures to a professional PDF.
    Returns {"pdf_path": str, "html_path": str}.
    Sync — no async, no API calls.
    """
    figures   = figures or {}
    keywords  = keywords or []
    out_path  = Path(output_path)
    html_path = out_path.with_suffix(".html")

    print(f"[pdf] Rendering: '{title}'")

    full_html = _build_html(title, markdown, figures, keywords, format, institution)

    # Save HTML (for debugging / fallback)
    html_path.write_text(full_html, encoding="utf-8")

    # PDF via WeasyPrint
    pdf_path = str(out_path)
    try:
        from weasyprint import HTML as WP_HTML
        WP_HTML(string=full_html, base_url=str(SKILL_DIR)).write_pdf(pdf_path)
        print(f"[pdf] Saved PDF → {pdf_path}")
    except Exception as e:
        print(f"[pdf] WeasyPrint failed ({type(e).__name__}: {e})")
        print(f"[pdf] HTML fallback saved → {html_path}")
        pdf_path = str(html_path)

    return {"pdf_path": pdf_path, "html_path": str(html_path)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="pdf-rendering-skill — professional PDF layout")
    parser.add_argument("--title",    required=True, help="Report title")
    parser.add_argument("--markdown", required=True, help="Path to .md file")
    parser.add_argument("--figures",  default=None,  help="figures.json manifest")
    parser.add_argument("--output",   default="report.pdf")
    parser.add_argument("--keywords", default=None,  help="Comma-separated keywords")
    parser.add_argument("--format",   default="academic",
                        choices=["academic", "article", "brief"])
    parser.add_argument("--institution", default="Université de Montréal")
    args = parser.parse_args()

    markdown_text = Path(args.markdown).read_text(encoding="utf-8")
    figures = json.loads(Path(args.figures).read_text()) if args.figures else {}
    keywords = [k.strip() for k in args.keywords.split(",")] if args.keywords else []

    result = run_pdf_rendering(
        title=args.title,
        markdown=markdown_text,
        figures=figures,
        output_path=args.output,
        keywords=keywords,
        format=args.format,
        institution=args.institution,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
