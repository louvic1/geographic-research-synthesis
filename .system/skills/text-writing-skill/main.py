"""
text-writing-skill
Writes scientific research text in French — no HTML, no PDF.
Produces clean Markdown that can be fed to pdf-rendering-skill.

Role: pure text generation. Knows nothing about layout or PDF.
Receives: research context + debate + extracted data + figure summaries.
Returns:  title, full markdown text, word count, keywords.

Usage:
    python main.py --topic "Urbanisation en Afrique subsaharienne" \\
                   --research research.json \\
                   --debate debate.json \\
                   --extracted extracted.json \\
                   --format academic \\
                   --output report.md
"""

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

load_dotenv()

CLAUDE_FAST  = "claude-haiku-4-5-20251001"
CLAUDE_SMART = "claude-sonnet-4-6"

MAX_SOURCES_IN_PROMPT = 30
MAX_CHARS_PER_SOURCE  = 400
MAX_TOKENS_TEXT       = 8000


# ---------------------------------------------------------------------------
# Format specifications
# ---------------------------------------------------------------------------

FORMAT_SPECS: dict[str, dict] = {
    "academic": {
        "word_target": "2500–3500 mots",
        "max_sources_in_prompt": 30,
        "max_tokens": 8000,
        "sections": [
            "## Résumé\n(150–200 mots, synthèse autonome lisible sans le reste du rapport)",
            "## 1. Introduction\n(contexte, question de recherche, pertinence, portée de l'étude)",
            "## 2. Revue de littérature\n(synthèse des sources en 2–3 sous-sections thématiques avec ### sous-titres)",
            "## 3. Résultats clés\n(données concrètes, métriques, tendances — tableaux Markdown pour données comparatives)",
            "## 4. Discussion\n(débats, contradictions, interprétations — s'appuyer sur les perspectives du débat)",
            "## 5. Limites\n(contraintes méthodologiques, lacunes des données, frontières de l'étude)",
            "## 6. Conclusion\n(synthèse finale, implications pratiques, pistes de recherche futures — OBLIGATOIRE, ≥200 mots)",
            "## Références\n(liste numérotée format APA)",
        ],
        "tone": "académique formel, troisième personne, précis, fondé sur les sources",
        "audience": "chercheurs et académiciens francophones",
        "citation_style": "citations numérotées en ligne [1][2], références complètes APA à la fin",
        "extra": (
            "Utiliser des tableaux Markdown (| col | col |) pour les données comparatives. "
            "Mettre en **gras** les termes clés à leur première occurrence. "
            "Chaque affirmation factuelle doit avoir une citation [N]. "
            "La section Conclusion doit être substantielle (au moins 200 mots). "
            "Ne jamais répéter la même information dans deux sections différentes."
        ),
    },
    "deep": {
        "word_target": "5000–7000 mots",
        "max_sources_in_prompt": 80,
        "max_tokens": 14000,
        "sections": [
            "## Résumé\n(250–300 mots, synthèse complète et autonome couvrant tous les axes du rapport)",
            "## 1. Introduction\n(contexte élargi, question de recherche, pertinence, portée et délimitation de l'étude — ≥300 mots)",
            "## 2. Contexte théorique et cadre conceptuel\n(définitions des concepts clés, cadres théoriques mobilisés, positionnement dans la littérature existante — 2–3 ### sous-sections)",
            "## 3. Revue de littérature étendue\n(synthèse approfondie en 3–4 sous-sections thématiques avec ### sous-titres; couvrir les courants principaux ET secondaires)",
            "## 4. Résultats et données\n(données concrètes, métriques, tendances temporelles et spatiales — tableaux Markdown obligatoires pour les comparaisons; analyse des figures disponibles)",
            "## 5. Discussion approfondie\n(interprétation des résultats, mécanismes explicatifs, comparaisons inter-régionales ou temporelles — ≥600 mots)",
            "## 6. Débats et controverses\n(positions divergentes détaillées, tensions épistémologiques, désaccords méthodologiques — s'appuyer sur toutes les contradictions identifiées)",
            "## 7. Implications et perspectives\n(implications pour les politiques publiques, l'aménagement du territoire, les pratiques; pistes de recherche futures — ≥400 mots)",
            "## 8. Limites méthodologiques\n(contraintes des données, biais potentiels, lacunes de la littérature, frontières de l'étude)",
            "## 9. Conclusion\n(synthèse finale intégrant tous les axes — OBLIGATOIRE, ≥350 mots)",
            "## Références\n(liste numérotée format APA — inclure TOUTES les sources citées)",
        ],
        "tone": "académique formel, troisième personne, analytique et nuancé, fondé exhaustivement sur les sources",
        "audience": "chercheurs spécialisés, comités de lecture de revues scientifiques francophones",
        "citation_style": "citations numérotées en ligne [1][2], références complètes APA à la fin",
        "extra": (
            "Ce rapport est de niveau recherche approfondie — chaque section doit être substantielle. "
            "Utiliser des tableaux Markdown (| col | col |) pour toutes les données comparatives. "
            "Mettre en **gras** les termes clés à leur première occurrence. "
            "Chaque affirmation factuelle doit avoir une citation [N]. "
            "La section Conclusion doit être ≥350 mots et synthétiser tous les axes du rapport. "
            "Exploiter le maximum des sources fournies — ne pas se limiter aux 10 premières. "
            "Ne jamais répéter la même information dans deux sections différentes. "
            "Les sections Débats et Implications doivent chacune avoir ≥2 sous-sections thématiques (###)."
        ),
    },
    "article": {
        "word_target": "900–1300 mots",
        "sections": [
            "## Mise en contexte\n(accroche, pourquoi c'est important maintenant)",
            "## Ce que la recherche révèle\n(résultats clés en langage accessible, listes libres)",
            "## Le débat\n(points de vue divergents, tensions, désaccords entre experts)",
            "## Ce que ça change\n(implications pratiques pour les politiques, l'aménagement, le quotidien)",
            "## Conclusion\n(question ouverte ou appel à l'action — OBLIGATOIRE)",
        ],
        "tone": "accessible, engageant, conversationnel mais informé, en français",
        "audience": "grand public éduqué, lecteurs de Medium francophone",
        "citation_style": "parenthèse (Auteur, Année) — pas de notes de bas de page",
        "extra": (
            "Commencer par le résultat le plus surprenant. "
            "Paragraphes courts (3–4 phrases max). "
            "Tout en français. Éviter le jargon."
        ),
    },
    "brief": {
        "word_target": "350–500 mots",
        "sections": [
            "## Synthèse\n(1 paragraphe, l'essentiel du sujet)",
            "## Résultats principaux\n(exactement 3 points avec chiffres ou faits concrets)",
            "## Implications\n(2–3 phrases sur ce que ça signifie en pratique)",
            "## Sources clés\n(3–5 sources les plus pertinentes avec URLs)",
        ],
        "tone": "direct, sans jargon, en français",
        "audience": "décideurs, lecteurs de README GitHub",
        "citation_style": "liens inline [Titre](URL)",
        "extra": "Chaque point doit contenir un fait ou un chiffre concret. Tout en français.",
    },
}


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------

def _build_sources_context(research: Optional[dict], max_sources: int = MAX_SOURCES_IN_PROMPT) -> tuple[str, list[dict]]:
    if not research:
        return "", []
    sources = research.get("sources", [])
    top = sorted(sources, key=lambda s: s.get("relevance_score", 0), reverse=True)[:max_sources]
    lines: list[str] = []
    ref_list: list[dict] = []
    for i, s in enumerate(top, 1):
        text    = (s.get("summary") or s.get("abstract") or "")[:MAX_CHARS_PER_SOURCE]
        title   = s.get("title", "")
        url     = s.get("url", "")
        year    = s.get("year", "")
        authors = ", ".join((s.get("authors") or [])[:3])
        line = f"[{i}] {title}"
        if authors:
            line += f" — {authors}"
        if year:
            line += f" ({year})"
        if text:
            line += f"\n    {text}"
        lines.append(line)
        ref_list.append({"index": i, "title": title, "url": url, "authors": authors, "year": year})
    synthesis = research.get("synthesis", "")
    block = "SOURCES:\n" + "\n\n".join(lines)
    if synthesis:
        block = f"SYNTHÈSE NOTEBOOKLM:\n{synthesis[:3000]}\n\n" + block
    return block, ref_list


def _build_extractions_context(extractions: Optional[list[dict]]) -> str:
    if not extractions:
        return ""
    all_metrics: list[str] = []
    all_locations: set[str] = set()
    all_concepts: set[str] = set()
    domain_metrics: dict[str, list[str]] = {}

    for record in extractions:
        domain = record.get("domain", "")
        for m in record.get("metrics", []):
            metric_str = f"{m.get('value')} {m.get('unit')} ({m.get('context')})"
            all_metrics.append(metric_str)
            if domain:
                domain_metrics.setdefault(domain, []).append(metric_str)
        for loc in record.get("locations", []):
            all_locations.add(loc.get("name", ""))
        for c in record.get("concepts", []):
            all_concepts.add(c)

    parts = ["DONNÉES EXTRAITES:"]
    if domain_metrics:
        for domain, metrics in domain_metrics.items():
            parts.append(f"\nMétriques [{domain}]:\n" + "\n".join(f"  - {m}" for m in metrics[:10]))
    elif all_metrics:
        parts.append("Métriques clés:\n" + "\n".join(f"  - {m}" for m in all_metrics[:20]))
    if all_locations:
        parts.append("Lieux: " + ", ".join(sorted(all_locations)[:15]))
    if all_concepts:
        parts.append("Concepts clés: " + ", ".join(sorted(all_concepts)[:20]))
    return "\n".join(parts)


def _build_debate_context(debate: Optional[dict]) -> str:
    if not debate:
        return ""
    parts = ["ANALYSE DU DÉBAT:"]
    for p in debate.get("perspectives", []):
        parts.append(f"Perspective — {p.get('label')}: {p.get('claim')}")
        for ev in (p.get("evidence") or [])[:2]:
            parts.append(f"  Evidence: {ev}")
    for c in debate.get("contradictions", []):
        parts.append(
            f"Contradiction: «{c.get('claim_a')}» vs «{c.get('claim_b')}» — {c.get('note', '')}"
        )
    gaps = debate.get("gaps", [])
    if gaps:
        parts.append("Lacunes de recherche:\n" + "\n".join(f"  - {g}" for g in gaps))
    synthesis = debate.get("synthesis", "")
    if synthesis:
        parts.append(f"Synthèse du débat: {synthesis}")
    return "\n".join(parts)


def _build_figures_context(figures_summary: Optional[dict]) -> str:
    if not figures_summary:
        return ""
    parts = ["FIGURES DISPONIBLES — à analyser explicitement dans le texte:"]
    fig_num = 1
    for key, info in figures_summary.items():
        if isinstance(info, dict):
            title = info.get("title", key)
            fig_type = info.get("type", key)
        else:
            title = str(info)
            fig_type = key
        parts.append(f"  Figure {fig_num} ({fig_type}): {title}")
        fig_num += 1
    parts.append(
        "\n→ OBLIGATION: chaque figure doit faire l'objet d'un court paragraphe analytique (3-5 phrases) "
        "dans la section Résultats ou Discussion. Ce paragraphe doit: "
        "(1) introduire ce que la figure montre, "
        "(2) relier les données au sujet de recherche, "
        "(3) formuler une conclusion ou interprétation concrète tirée de la visualisation. "
        "Référencer avec (voir Figure 1), (Figure 2) etc. au moment pertinent."
    )
    return "\n".join(parts)


def _build_references_md(ref_list: list[dict]) -> str:
    if not ref_list:
        return ""
    lines = ["## Références", ""]
    for ref in ref_list:
        authors = ref.get("authors") or "Auteur inconnu"
        year    = ref.get("year") or "s.d."
        title   = ref.get("title") or "Sans titre"
        url     = ref.get("url") or ""
        line    = f"{ref['index']}. {authors} ({year}). *{title}*."
        if url:
            line += f" Récupéré de [{url}]({url})"
        lines.append(line)
    return "\n".join(lines)


def _extract_keywords(topic: str) -> list[str]:
    stop = {
        "de", "du", "des", "le", "la", "les", "en", "et", "au", "aux",
        "un", "une", "sur", "dans", "par", "pour", "avec", "une", "the",
        "of", "in", "and", "a", "an",
    }
    words = re.findall(r"\b\w{4,}\b", topic.lower())
    return [w for w in words if w not in stop][:6]


def _count_words(text: str) -> int:
    return len(text.split())


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

TITLE_PROMPT = """Tu es un chercheur en géographie. Génère un titre de rapport académique court, intelligent et percutant en français.

Sujet: {topic}

Règles:
- Maximum 12 mots
- En français
- Précis et académique, pas générique
- Pas de sous-titre
- Refléter l'angle géographique ET l'impact ou l'enjeu principal

Retourne UNIQUEMENT le titre, sans guillemets ni ponctuation finale."""


REPORT_PROMPT = """Tu es un expert en rédaction académique spécialisé en géographie et sciences de l'environnement.

Rédige un rapport de recherche de type **{format}** sur le sujet : **{topic}**

EXIGENCES DE FORMAT:
- Longueur cible : {word_target}
- Sections obligatoires (titres Markdown ##) :
{sections}
- Ton : {tone}
- Public cible : {audience}
- Citations : {citation_style}
- {extra}

MATÉRIAU DE RECHERCHE:
{sources_context}

{extractions_context}

{debate_context}

{figures_context}

RÈGLES DE RÉDACTION:
- Utiliser UNIQUEMENT les informations fournies — ne jamais inventer de statistiques, auteurs ou résultats
- Citer chaque affirmation factuelle avec [N] correspondant aux sources ci-dessus
- OBJECTIF CITATIONS: utiliser AU MINIMUM 60% des sources fournies dans les références. Diversifier les citations — ne pas se limiter aux 5-10 premières sources. Chaque sous-section devrait citer au moins 2-3 sources différentes.
- Représenter équitablement les tensions identifiées dans l'analyse du débat
- Ne pas répéter les mêmes informations dans plusieurs sections
- Utiliser des tableaux Markdown (| col | col |) pour les données comparatives
- Mettre en **gras** les résultats clés et termes importants à leur première occurrence
- Ne pas ajouter de mention "généré par IA", ne pas inclure de titre principal (il sera ajouté séparément)
- Écrire intégralement en FRANÇAIS
- Conclusion COMPLÈTE et substantielle (≥200 mots)
- FIGURES: pour chaque figure listée dans FIGURES DISPONIBLES, écrire un paragraphe analytique de 3-5 phrases qui (1) décrit ce que la figure montre, (2) relie les données au sujet de la recherche, (3) formule une conclusion ou interprétation concrète. Ce paragraphe est OBLIGATOIRE — une figure sans analyse est inacceptable.

Rédige le rapport maintenant :"""


# ---------------------------------------------------------------------------
# Claude calls
# ---------------------------------------------------------------------------

async def generate_title(topic: str, client: anthropic.AsyncAnthropic) -> str:
    """Generate a short, intelligent French academic title (Claude Haiku)."""
    try:
        msg = await client.messages.create(
            model=CLAUDE_FAST,
            max_tokens=60,
            messages=[{"role": "user", "content": TITLE_PROMPT.format(topic=topic)}],
        )
        return msg.content[0].text.strip().strip('"').strip("'")
    except Exception:
        words = [w for w in topic.split() if len(w) > 3][:8]
        return " ".join(words).title()


async def write_report_text(
    topic: str,
    sources_context: str,
    extractions_context: str,
    debate_context: str,
    figures_context: str,
    format: str,
    client: anthropic.AsyncAnthropic,
) -> str:
    """Call Claude Sonnet to write the full report text in Markdown."""
    spec = FORMAT_SPECS.get(format, FORMAT_SPECS["academic"])
    sections_str = "\n".join(spec["sections"])

    prompt = REPORT_PROMPT.format(
        format=format,
        topic=topic,
        word_target=spec["word_target"],
        sections=sections_str,
        tone=spec["tone"],
        audience=spec["audience"],
        citation_style=spec["citation_style"],
        extra=spec["extra"],
        sources_context=sources_context or "(aucune source disponible)",
        extractions_context=extractions_context or "",
        debate_context=debate_context or "",
        figures_context=figures_context or "",
    )

    max_tokens = spec.get("max_tokens", MAX_TOKENS_TEXT)
    print(f"[text] Sending to Claude ({CLAUDE_SMART}, max_tokens={max_tokens})...")
    msg = await client.messages.create(
        model=CLAUDE_SMART,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_text_writing(
    topic: str,
    research: Optional[dict] = None,
    extractions: Optional[list[dict]] = None,
    debate: Optional[dict] = None,
    format: str = "academic",
    figures_summary: Optional[dict] = None,
    output_path: Optional[str] = None,
) -> dict:
    """
    Write a research report in French Markdown.
    Returns {"title": str, "markdown": str, "word_count": int, "keywords": list[str]}.
    """
    print(f"\n[text] Topic: '{topic}' | Format: {format}")

    client = anthropic.AsyncAnthropic()

    # Extract format-specific limits
    spec = FORMAT_SPECS.get(format, FORMAT_SPECS["academic"])
    max_sources = spec.get("max_sources_in_prompt", MAX_SOURCES_IN_PROMPT)

    # Build context
    sources_context, ref_list = _build_sources_context(research, max_sources=max_sources)
    extractions_context       = _build_extractions_context(extractions)
    debate_context            = _build_debate_context(debate)
    figures_context           = _build_figures_context(figures_summary)

    # Generate title and report text in parallel
    title_task = asyncio.create_task(generate_title(topic, client))
    text_task  = asyncio.create_task(write_report_text(
        topic, sources_context, extractions_context,
        debate_context, figures_context, format, client,
    ))

    title, body_text = await asyncio.gather(title_task, text_task)
    print(f"[text] Title: {title}")

    # Append references if not already present
    refs_md = _build_references_md(ref_list)
    if refs_md and "## Références" not in body_text:
        body_text = body_text + "\n\n" + refs_md

    word_count = _count_words(body_text)
    keywords   = _extract_keywords(topic)

    print(f"[text] Done — {word_count} words")

    result = {
        "title":      title,
        "markdown":   body_text,
        "word_count": word_count,
        "keywords":   keywords,
    }

    if output_path:
        Path(output_path).write_text(body_text, encoding="utf-8")
        print(f"[text] Saved → {output_path}")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="text-writing-skill — scientific report text")
    parser.add_argument("--topic",     required=True)
    parser.add_argument("--research",  default=None, help="research.json")
    parser.add_argument("--debate",    default=None, help="debate.json")
    parser.add_argument("--extracted", default=None, help="extracted.json")
    parser.add_argument("--figures",   default=None, help="figures.json manifest")
    parser.add_argument("--format",    default="academic",
                        choices=["academic", "article", "brief"])
    parser.add_argument("--output",    default=None, help="output .md file path")
    args = parser.parse_args()

    research   = json.loads(Path(args.research).read_text())   if args.research  else None
    debate     = json.loads(Path(args.debate).read_text())     if args.debate    else None
    extractions = json.loads(Path(args.extracted).read_text()) if args.extracted else None
    figures    = json.loads(Path(args.figures).read_text())    if args.figures   else None

    result = asyncio.run(run_text_writing(
        topic=args.topic,
        research=research,
        extractions=extractions,
        debate=debate,
        format=args.format,
        figures_summary=figures,
        output_path=args.output,
    ))

    if not args.output:
        print(result["markdown"])
    else:
        meta = {k: v for k, v in result.items() if k != "markdown"}
        print(json.dumps(meta, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
