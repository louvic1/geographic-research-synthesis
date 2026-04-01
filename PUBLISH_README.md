# system-1-research-synthesis-system — Published Version

This is a standalone, ready-to-use version of the system-1-research-synthesis-system system exported from the main project.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run with a topic
python main.py --topic "urban heat islands in Montreal"

# See help
python main.py --help
```

## What's Included

- `main.py` — Entry point and pipeline orchestrator
- `.system/skills/` — All required skills bundled (no external dependencies)
- `agents/`, `prompts/` — System-specific configuration
- `DEPENDENCIES.yaml` — Declares the system's architecture
- `config.yaml` — Default configuration
- `requirements.txt` — Python dependencies

## System Overview

See `DEPENDENCIES.yaml` for the full pipeline specification and skill details.

See `README.md` for detailed documentation.

## For Local Development

If you want to work on this system or contribute improvements:

1. Clone the main project repository
2. Follow the development setup in that repository
3. Use `systems/system-1-research-synthesis-system/` for development
4. Skills are shared across systems in `.claude/skills/`

## For GitHub Issues / Contributing

Report issues or submit improvements to the main project repository.

---

Generated: recherche-claude
