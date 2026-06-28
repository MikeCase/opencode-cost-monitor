# OpenCode Cost Monitor — AGENTS.md

## Project Identity

Live TUI that watches opencode.db and shows real-time cost stats, model breakdown, cache economics, and daily trends. Single-file Python app.

## Remotes

- **GitHub:** `github.com/MikeCase/opencode-cost-monitor.git`
- **Forgejo:** `fj.splaq.us/splaq/opencode-cost-monitor.git`

Push to both on every change.

## Stack & Key Files

| File | Role |
|------|------|
| `opencode-cost.py` | Single-file app — SessionData data layer + CostMonitor TUI |
| `pricing.json` | Per-model token pricing for Go ($10/mo) and Zen (pay-per-token) plans |

No Docker, no tests, no container orchestration. Runs directly with Python 3.12+ and `textual`.

## Commands

```sh
# Setup (one-time)
python3 -m venv .venv
.venv/bin/pip install textual

# Run
./opencode-cost.py
./opencode-cost.py --db /custom/path/opencode.db

# Syntax check
python3 -m py_compile opencode-cost.py
```

## Committing

Commit to `main`, then push to both remotes:

```sh
git add opencode-cost.py pricing.json
git commit -m "desc"
git push origin main && git push github main
```

After push, send email report to michael.e.case@gmail.com via Brevo (homelab account).

## Keybindings

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Refresh |
| `m` | Cycle time period (all / month / week / day) |
| `p` | Cycle pricing plan (Go / Zen) |

## Decisions Log

- **2026-06-28:** Daily trend sparkline had a hardcoded 30-day cutoff independent of the `m`-key time period. Removed the redundant filter — trend now respects the selected mode. Trend-sum label made dynamic instead of hardcoded "last 30d".

## Known Patterns / Gotchas

- `SessionData.recompute(period)` filters `sessions_list` by the period cutoff. All downstream aggregates (totals, model breakdown, daily trend) must use `sessions_list` — do not add a second independent filter.
- `_poll_db()` calls `data.refresh()` (which calls `recompute("all")`) then `data.recompute(self._time_mode)` — two recomputes per poll. This is intentional: refresh resets from DB, then the mode filter is applied on top.
- `pricing.json` is the authoritative pricing source. Add new models there, not in `opencode-cost.py`.

## Style

- Black formatting, single-file app kept under 650 lines.
- No emoji in UI or code.
