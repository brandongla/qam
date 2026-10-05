# QAM

Agentic quantitative research system: hierarchical teams of AI agents investigate trading
strategies under strict false-positive controls. Initial focus: cryptocurrencies on
decentralized exchanges.

- **Design (single source of truth):** [`docs/DESIGN.md`](docs/DESIGN.md)
- **Deploying the recorders:** [`deploy/README.md`](deploy/README.md)

## Status

Phase 1 (data foundation) in progress:

- [x] Raw archive: crash-safe, hourly-rotated zstd JSONL with SHA-256 manifests (`qam.data.raw`)
- [x] Forward recorders for Hyperliquid and Lighter (`qam.recorders`)
- [x] VM installer + systemd services (`deploy/`)
- [ ] Live smoke test on the VM
- [ ] Backups to the local machine
- [ ] Hyperliquid archive backfill, CEX history connectors, normalizers

## Development

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests
pytest -q
```

CI runs on both x86-64 and ARM64 (D-028).
