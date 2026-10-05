# CLAUDE.md

## Project
QAM is an agentic quantitative research system. Hierarchical teams of AI agents research trading
strategies, and the system is built to minimize false positives. The initial focus is crypto on
DEXs. The full specification is in `docs/DESIGN.md`.

## Living design document — maintenance rules
- `docs/DESIGN.md` is the single source of truth for specifications. Read it before making design or implementation decisions.
- Any change to scope, architecture, agent roles, lifecycle gates, statistical thresholds, data sources, or tech stack **must update `docs/DESIGN.md` in the same commit**.
- For every revision:
  - Bump the version in the header table (semver: major = restructure, minor = new/changed spec, patch = clarifications) and update "Last updated".
  - Add a row to the **Changelog** (§19).
  - Record decisions in the **Decision Log** (§17) with the next `D-NNN` ID. When an open question (§18) is resolved, move it into the Decision Log and reference its `Q-NN` ID.
- Don't silently change `[DEFAULT]` thresholds. Each change needs a Decision Log entry with its rationale.
