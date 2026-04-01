# Bundled Skills for system-1-research-synthesis-system

This folder contains all skills needed to run this system standalone, without external dependencies.

## Skills Included

### 1. **notebooklm-research-skill**
Finds and aggregates academic sources from NotebookLM and web search.
- **Input**: Research topic
- **Output**: JSON with sources, abstracts, citations
- **Used in stage**: Research gathering

### 2. **domain-analysis-skill**
Classifies sources by domain and extracts key insights using parallel agents.
- **Input**: Sources from notebooklm-research-skill
- **Output**: Structured domain analysis (JSON)
- **Used in stage**: Domain classification

### 3. **debate-generation-skill**
Maps the intellectual landscape by identifying perspectives, contradictions, and gaps.
- **Input**: Domain analysis results
- **Output**: Structured debate (perspectives, evidence, tensions)
- **Used in stage**: Debate mapping

### 4. **data-scout-skill**
Hunts for open datasets (GBIF, World Bank, FAOSTAT, NASA) relevant to the topic and renders visualizations.
- **Input**: Research topic
- **Output**: Data files + figures (PNG, HTML)
- **Used in stage**: Data gathering & visualization

### 5. **text-writing-skill**
Writes scientific text in French with proper academic formatting and citations.
- **Input**: All previous stage outputs
- **Output**: Markdown or HTML report
- **Used in stage**: Report writing

### 6. **pdf-rendering-skill**
Assembles all content into a professional, publication-ready PDF.
- **Input**: Text + figures + citations
- **Output**: Final PDF report
- **Used in stage**: PDF export

## Skill Loading Logic

When you run `python main.py`:
1. The loader looks for skills in `.system/skills/` first (bundled)
2. Falls back to `../../.claude/skills/` if not found (local development)
3. This allows the system to work both locally (with all skills) and on GitHub (with bundled skills only)

## For GitHub Publication

When publishing this system to GitHub:
- Include the entire `.system/skills/` folder
- Users will have everything needed to run the system
- No need to install external skills or set up `.claude/`

## For Local Development

When developing locally:
- You can edit skills in `../../.claude/skills/`
- Changes are reflected immediately in the system
- The bundled copies in `.system/skills/` can be ignored during dev
- Before publishing, update `.system/skills/` by re-running the copy command
