# Deploying the recorders (MVP VM)

Target: a single Linux VM, ARM64 or x86-64 (DESIGN.md §17.1, D-027/D-028). The recorders run as
systemd services, one process per venue: `qam-recorder@hyperliquid` and `qam-recorder@lighter`.

## First install

```bash
git clone https://github.com/brandongla/qam.git && cd qam
git checkout claude/ai-agent-trading-research-7aa0xw   # until merged
sudo ./deploy/install.sh --smoke
```

`--smoke` first runs a 60-second live test per venue into a scratch directory. It prints what
arrived and fails before touching the services if a venue returns no data or reports errors.
**This is the first time the code talks to the real venues**: the development sandbox can't
reach them. Please send me the smoke output.

What the installer does (idempotent, safe to re-run for upgrades):

| Item | Location |
|------|----------|
| Python 3.12 (uv-managed, no OS Python needed) + venv | `/opt/qam/python`, `/opt/qam/venv` |
| Config (installed once, never overwritten) | `/etc/qam/recorder.yaml` |
| Data (raw archive, manifests, health, locks) | `/srv/qam/data` |
| Services | `/etc/systemd/system/qam-recorder@.service` |
| System user | `qam` (no login shell) |

## Day-to-day

```bash
# Health: connection states, data freshness, records per stream, reconnects
sudo -u qam /opt/qam/venv/bin/qam record status --data-root /srv/qam/data

# Logs
journalctl -u 'qam-recorder@*' -f

# Archive contents and integrity
sudo -u qam /opt/qam/venv/bin/qam raw summary --data-root /srv/qam/data
sudo -u qam /opt/qam/venv/bin/qam raw verify  --data-root /srv/qam/data          # checksums
sudo -u qam /opt/qam/venv/bin/qam raw verify  --data-root /srv/qam/data --deep   # + decode all

# What would be subscribed (live market discovery, no websocket)
/opt/qam/venv/bin/qam record plan --venue hyperliquid --config /etc/qam/recorder.yaml

# Upgrade after pulling new code
git pull && sudo ./deploy/install.sh
```

## Data layout

```
/srv/qam/data/
  raw/<venue>/<stream>/<YYYY-MM-DD>/<venue>.<stream>.<YYYYMMDDTHH>.<start_ns>.jsonl.zst
  manifest/<venue>/<YYYY-MM-DD>.jsonl      # one entry per closed file: sha256, records, bytes
  health/<venue>.json                      # refreshed every 10 s
  locks/<venue>.lock
```

- Files rotate hourly (UTC). A file is only listed in the manifest once closed, and it's never
  modified afterwards. Those are the files the backup copies (D-029).
- The file being written has a `.part` suffix. After a crash, its contents up to the last
  flush (at most 5 s earlier) are recovered on the next start.
- Streams: Hyperliquid `trades`, `bbo`, `asset_ctx`, `l2`, `l2_sf4`, `l2_sf3`; Lighter
  `trades`, `order_book`, `market_stats`. Both venues also have `meta` (connects, disconnects,
  subscriptions: gaps are recorded, not silent), `control` (pongs/acks), `reference` (REST
  market metadata snapshots), and `unknown` (anything unclassified, kept verbatim).

## Backups

Backups to the local Windows/WSL2 machine (D-029) come in the next step.
