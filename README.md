# System 1 — Research Synthesis System

Pipeline de recherche géographique entièrement automatisé. Donne un sujet, obtiens un package complet : sources académiques, extraction par domaine, débat intellectuel, rapport PDF et figures de données.

## Pipeline (6 étapes)

```
Input: sujet de recherche (texte)
         │
         ▼
[1] notebooklm-research-skill     → 50 sources académiques (Semantic Scholar + OpenAlex + arXiv + NotebookLM)
         │
         ▼
[2] domain-analysis-skill         → 4 agents spécialisés en parallèle (env., social, éco., spatial)
         │
         ▼
[3] debate-generation-skill       → paysage intellectuel : perspectives, contradictions, lacunes
         │
         ├──────────────────────┐
         ▼                      ▼
[4a] chart-skill            [4b] map-skill          ← exécutés en parallèle
     → chart.png                → map.png + map.html
         │
         ▼
[5] text-writing-skill            → rapport.md (3000–5000 mots, en français)
         │
         ▼
[6] pdf-rendering-skill           → report.pdf (mise en page professionnelle)

Output: research-projects/<slug>-<timestamp>/
```

## Démarrage rapide

```bash
cd ~/Desktop/recherche-claude
source .venv/bin/activate

python systems/system-1-research-synthesis-system/main.py \
  --topic "îlots de chaleur urbains Montréal"
```

## Toutes les options CLI

```bash
python systems/system-1-research-synthesis-system/main.py \
  --topic              "îlots de chaleur urbains Montréal"  # obligatoire
  --max-sources        50          # sources à trouver (défaut: config.yaml)
  --min-year           2015        # année de publication minimale
  --peer-reviewed-only             # ignorer les preprints arXiv
  --max-domain-sources 20          # sources envoyées aux agents de domaine
  --domain-agents      environmental social economic spatial  # agents actifs
  --n-perspectives     3           # perspectives du débat (2–5)
  --report-format      academic    # academic | article | brief
  --resume-from        research-projects/mon-run-20260322/  # reprendre un run
```

## Test rapide (peu coûteux)

```bash
python systems/system-1-research-synthesis-system/main.py \
  --topic "permafrost thaw Arctic" \
  --max-sources 5 \
  --report-format brief
```

## Configuration (config.yaml)

```yaml
max_sources: 50
min_year: 2015
peer_reviewed_only: false

max_domain_sources: 20
domain_agents: [environmental, social, economic, spatial]

n_perspectives: 3

report_format: academic

map_formats: [png, html]
chart_formats: [png]
```

Les flags CLI écrasent toujours `config.yaml`.

## Fichiers de sortie

```
research-projects/<slug>-<timestamp>/
├── research.json       — sources avec scores et résumés (NotebookLM)
├── extracted.json      — données extraites par domaine (domain-analysis-skill)
├── debate.json         — perspectives, contradictions, lacunes, hypothèses
├── summary.json        — métadonnées du run (stats, chemins, durée)
└── output/
    ├── report.pdf      — rapport PDF professionnel (titre, TOC, figures intégrées)
    ├── report.md       — version markdown du rapport
    └── figures/
        ├── <slug>-chart.png   — graphique de données (World Bank / NASA)
        ├── <slug>-map.html    — carte interactive (Folium, ouvrir dans le navigateur)
        └── <slug>-map.png     — carte statique (contextily + basemap)
```

## Améliorer les figures avec data-scout-skill

Après un run, `data-scout-skill` peut remplacer les figures par des visualisations
enrichies (GBIF + World Bank + FAOSTAT + NASA, choix automatique du meilleur type) :

```bash
python .claude/skills/data-scout-skill/main.py \
  --topic "îlots de chaleur urbains Montréal" \
  --output-dir research-projects/<slug>-<timestamp>/output/figures
```

## Skills utilisés

| Étape | Skill | Modèle | Rôle |
|-------|-------|--------|------|
| 1 | `notebooklm-research-skill` | Haiku + Sonnet | Découverte de sources + synthèse IA |
| 2 | `domain-analysis-skill` | Haiku (classif.) + Sonnet (extraction) | Extraction spécialisée par domaine en parallèle |
| 3 | `debate-generation-skill` | Sonnet | Analyse du débat académique |
| 4a | `chart-skill` | Haiku + Sonnet | Graphique (World Bank, NASA POWER) |
| 4b | `map-skill` | Haiku + Sonnet | Carte (Folium, contextily, choropleth) |
| 5 | `text-writing-skill` | Haiku + Sonnet | Rédaction scientifique en français |
| 6 | `pdf-rendering-skill` | — | Mise en page PDF (WeasyPrint, pas d'API) |
| — | `data-scout-skill` | Haiku + Sonnet | Chasseur de données open (GBIF, FAO, WB) |

## Prérequis

```bash
pip install -r requirements.txt
pip install "notebooklm-py[browser]"
playwright install chromium
notebooklm login   # authentification Google (une seule fois)
```

`ANTHROPIC_API_KEY` doit être défini dans `.env` à la racine du projet.

## Exemples de sujets testés

```bash
--topic "Déforestation en Amazonie brésilienne"
--topic "Urbanisation en Afrique subsaharienne"
--topic "Urban sprawl Montreal impact forest agricultural land"
```
