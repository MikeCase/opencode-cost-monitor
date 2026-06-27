#!/usr/bin/env python3
"""OpenCode Cost Monitor — live TUI.

Watches opencode.db and updates cost stats, model breakdown, cache
economics, and daily trend in real time.

Usage:
    ./tui.py
    ./tui.py --db /path/to/opencode.db
"""

import os
import sys

# ── Bootstrap: use project venv if available ──────────────────────
_venv = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python3")
if os.path.exists(_venv) and sys.executable != _venv:
    os.execv(_venv, [_venv] + sys.argv)

import sqlite3
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Container
from textual.widgets import Static, DataTable
from textual.timer import Timer

# ── Shared data layer (adapted from report.py) ─────────────────────

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.environ.get(
    "OPENCODE_DB_PATH",
    os.path.expanduser("~/.local/share/opencode/opencode.db")
)
PRICING_FILE = os.path.join(REPO_ROOT, "pricing.json")


def load_pricing(plan="go"):
    """Load pricing table for the given plan ('go' or 'zen')."""
    with open(PRICING_FILE) as f:
        data = json.load(f)
    return data.get("plans", {}).get(plan, {}).get("models", {}), data.get("plans", {}).get(plan, {}).get("name", plan.capitalize())


def _parse_model(model_json):
    if not model_json:
        return None, None, None
    try:
        m = json.loads(model_json)
        return m.get("id"), m.get("providerID"), m.get("variant")
    except (json.JSONDecodeError, TypeError):
        return None, None, None


def _ts(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


class SessionData:
    """Aggregated snapshot of opencode.db."""

    def __init__(self, db_path):
        self.total_cost = 0.0
        self.total_sessions = 0
        self.paid_sessions = 0
        self.total_in = 0
        self.total_out = 0
        self.total_cache_r = 0
        self.total_cache_w = 0
        self.cache_hit_rate = 0.0
        self.cache_cost = 0.0
        self.full_price_cost = 0.0
        self.cache_savings = 0.0
        self.models: list = []  # sorted by cost desc
        self.daily_trend: list[float] = []  # last 30 days cost
        self.last_updated = ""
        self.last_session_cost = 0.0
        self.last_session_title = ""
        self.active_sessions = 0
        self.plan = "go"
        self.plan_label = "Go"
        self._pricing, _ = load_pricing("go")
        self._db_path = db_path

    def refresh(self):
        """Re-read DB and store all session data."""
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT id, model, cost, tokens_input, tokens_output,
                       tokens_reasoning, tokens_cache_read, tokens_cache_write,
                       time_created, time_updated, title, slug
                FROM session ORDER BY time_created
            """).fetchall()
            conn.close()
        except (sqlite3.Error, FileNotFoundError):
            return

        all_sessions = []
        max_updated = 0

        for r in rows:
            cost = float(r["cost"] or 0)
            tin = int(r["tokens_input"] or 0)
            tout = int(r["tokens_output"] or 0)
            cr = int(r["tokens_cache_read"] or 0)
            cw = int(r["tokens_cache_write"] or 0)
            created = _ts(r["time_created"])
            mid, prov, _ = _parse_model(r["model"])

            updated = int(r["time_updated"] or 0)
            if updated > max_updated:
                max_updated = updated
                self.last_session_cost = cost
                self.last_session_title = r["title"] or r["slug"] or ""

            all_sessions.append({
                "cost": cost, "tin": tin, "tout": tout,
                "cr": cr, "cw": cw, "mid": mid, "prov": prov,
                "created": created,
            })

        self._all_sessions = all_sessions
        self.last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.recompute("all", self.plan)

    def recompute(self, period: str = "all", plan: str | None = None):
        """Recompute aggregates from _all_sessions filtered by time period.

        Periods: 'all', 'month' (30d), 'week' (7d), 'day' (today).
        """
        if plan:
            self.plan = plan
            self._pricing, self.plan_label = load_pricing(plan)
        now = datetime.now(timezone.utc)
        cutoff = {
            "all": None,
            "month": now - timedelta(days=30),
            "week": now - timedelta(days=7),
            "day": now.replace(hour=0, minute=0, second=0, microsecond=0),
        }.get(period)

        sessions_list = self._all_sessions
        if cutoff:
            sessions_list = [s for s in sessions_list if s["created"] and s["created"] >= cutoff]

        self.time_period = period

        # Per-model aggregation
        class _MA:
            def __init__(self):
                self.cost = 0.0
                self.count = 0
                self.tin = 0
                self.tout = 0
                self.cr = 0
                self.cw = 0
                self.model_id = "unknown"
                self.provider = ""
        model_agg: dict[str, _MA] = defaultdict(_MA)

        for s in sessions_list:
            key = s["mid"] or "unknown"
            a = model_agg[key]
            a.cost += s["cost"]
            a.count += 1
            a.tin += s["tin"]
            a.tout += s["tout"]
            a.cr += s["cr"]
            a.cw += s["cw"]
            a.model_id = key
            if s["prov"]:
                a.provider = s["prov"]

        # Totals
        pricing = self._pricing
        self.total_sessions = len(sessions_list)
        self.total_cost = sum(s["cost"] for s in sessions_list)
        self.paid_sessions = sum(1 for s in sessions_list if s["cost"] > 0)
        self.total_in = sum(s["tin"] for s in sessions_list)
        self.total_out = sum(s["tout"] for s in sessions_list)
        self.total_cache_r = sum(s["cr"] for s in sessions_list)
        self.total_cache_w = sum(s["cw"] for s in sessions_list)

        total_fresh = self.total_in + self.total_cache_r
        self.cache_hit_rate = self.total_cache_r / total_fresh * 100 if total_fresh > 0 else 0.0

        # Cache cost/savings
        self.cache_cost = 0.0
        self.full_price_cost = 0.0
        for s in sessions_list:
            mid = s["mid"]
            p = pricing.get(mid)
            if p:
                cr_rate = p.get("cache_read")
                cw_rate = p.get("cache_write")
                inp_rate = p.get("input")
                if cr_rate and s["cr"]:
                    self.cache_cost += s["cr"] / 1_000_000 * cr_rate
                if inp_rate and s["cr"]:
                    self.full_price_cost += s["cr"] / 1_000_000 * inp_rate
                if cw_rate and s["cw"]:
                    self.cache_cost += s["cw"] / 1_000_000 * cw_rate
        self.cache_savings = self.full_price_cost - self.cache_cost

        # Per-model list (sorted)
        self.models = sorted(model_agg.values(), key=lambda m: m.cost, reverse=True)

        # Daily trend (last 30 days of filtered period)
        trend_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        self.daily_trend = [
            s["cost"] for s in sessions_list
            if s["created"] and s["created"] >= trend_cutoff
        ]

        # Active sessions (last 5 min)
        recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        self.active_sessions = sum(
            1 for s in sessions_list
            if s["created"] and s["created"] >= recent_cutoff
        )


# ── TUI Widgets ────────────────────────────────────────────────────

BAR_CHARS = "▁▂▃▄▅▆▇█"


def _bar(value, max_val, width=10):
    """Render a unicode bar for the given value."""
    if max_val <= 0:
        return " " * width
    ratio = value / max_val
    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)


def _fmt_dollar(v):
    if v is None or v == 0:
        return "$0.00"
    if v < 0.01:
        return f"${v:.4f}"
    if v < 1:
        return f"${v:.3f}"
    if v < 100:
        return f"${v:.2f}"
    return f"${v:,.2f}"


def _fmt_tokens(v):
    if v is None or v == 0:
        return "0"
    if v < 1_000:
        return f"{v}"
    if v < 1_000_000:
        return f"{v/1e3:.1f}K"
    if v < 1_000_000_000:
        return f"{v/1e6:.2f}M"
    return f"{v/1e9:.2f}B"


def _fmt_pct(v):
    return f"{v:.1f}%"


def _sparkline(values, width=30):
    """Render a unicode sparkline."""
    if not values:
        return " " * width
    mn, mx = min(values), max(values)
    rng = mx - mn if mx != mn else 1
    out = []
    for v in values[-width:]:
        idx = int((v - mn) / rng * 7)
        out.append(BAR_CHARS[min(idx, 7)])
    return "".join(out)


# ── Main App ───────────────────────────────────────────────────────


class CostMonitor(App):
    """Live OpenCode cost monitor TUI."""

    CSS = """
    Screen {
        background: #0f0f14;
    }

    #title-bar {
        height: 1;
        content-align: center middle;
        background: #1a1a2e;
        color: #e0e0e0;
        text-style: bold;
        padding: 0 1;
    }

    #top-stats {
        height: 5;
        margin: 0 1;
    }

    .stat-box {
        width: 1fr;
        height: 5;
        border: solid #2a2a3e;
        padding: 0 1;
        background: #16162a;
    }

    .stat-box > Static {
        width: 100%;
    }

    .stat-value {
        color: #00d4aa;
        text-style: bold;
        text-align: center;
    }

    .stat-label {
        color: #8888aa;
        text-align: center;
    }

    .stat-sub {
        color: #666688;
        text-align: center;
        text-style: italic;
    }

    .stat-accent {
        color: #ff7a5c;
        text-style: bold;
        text-align: center;
    }

    #main-panel {
        height: 1fr;
        margin: 0 1;
    }

    #model-table {
        height: 1fr;
        border: solid #2a2a3e;
        background: #16162a;
        overflow-x: hidden;
        overflow-y: auto;
    }

    #model-table DataTable {
        height: 1fr;
    }

    DataTable > .datatable--header {
        background: #1e1e34;
        color: #aaaacc;
        text-style: bold;
    }

    DataTable > .datatable--odd-row {
        background: #16162a;
    }

    DataTable > .datatable--even-row {
        background: #1a1a30;
    }

    #bottom-panel {
        height: 6;
        margin: 0 1;
    }

    .bottom-box {
        width: 1fr;
        height: 6;
        border: solid #2a2a3e;
        background: #16162a;
    }

    .bottom-box > Static {
        width: 100%;
    }

    #trend-box {
        width: 2fr;
        height: 6;
        border: solid #2a2a3e;
        background: #16162a;
    }

    #trend-box > Static {
        width: 100%;
    }

    .sparkline-text {
        color: #00d4aa;
    }

    .highlight {
        color: #ffd700;
    }

    #footer-bar {
        height: 1;
        background: #1a1a2e;
        color: #666688;
        padding: 0 2;
    }

    #footer-bar > Static {
        width: 1fr;
    }
    """

    TITLE = "OpenCode Cost Monitor"

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh now"),
        ("m", "cycle_mode", "Period"),
        ("p", "cycle_plan", "Plan"),
    ]

    def __init__(self, db_path=DEFAULT_DB):
        super().__init__()
        self.data = SessionData(db_path)
        self._poll_timer: Timer | None = None
        self._headers_visible = True
        self._last_session_count = -1
        self._cols_added = False
        self._first_load = True
        self._time_mode = "all"
        self._time_modes = ["all", "month", "week", "day"]
        self._time_labels = {"all": "All Time", "month": "Last 30d", "week": "Last 7d", "day": "Today"}

    def compose(self):
        yield Static(self.TITLE, id="title-bar")

        with Horizontal(id="top-stats"):
            with Container(classes="stat-box"):
                yield Static("", id="stat-cost-val", classes="stat-value")
                yield Static("total cost", classes="stat-label")
                yield Static("", id="stat-cost-sub", classes="stat-sub")
            with Container(classes="stat-box"):
                yield Static("", id="stat-cache-val", classes="stat-value")
                yield Static("cache hit rate", classes="stat-label")
                yield Static("", id="stat-cache-sub", classes="stat-sub")
            with Container(classes="stat-box"):
                yield Static("", id="stat-sess-val", classes="stat-value")
                yield Static("sessions", classes="stat-label")
                yield Static("", id="stat-sess-sub", classes="stat-sub")
            with Container(classes="stat-box"):
                yield Static("", id="stat-save-val", classes="stat-accent")
                yield Static("cache savings", classes="stat-label")
                yield Static("", id="stat-save-sub", classes="stat-sub")

        with Container(id="main-panel"):
            yield DataTable(id="model-table", show_cursor=False)

        with Horizontal(id="bottom-panel"):
            with Container(id="trend-box"):
                yield Static("Daily Cost Trend (last 30 days)", id="trend-label", classes="stat-label")
                yield Static("", id="trend-spark", classes="sparkline-text")
                yield Static("", id="trend-range", classes="stat-sub")
            with Container(classes="bottom-box"):
                yield Static("Cache Economics", id="cache-label", classes="stat-label")
                yield Static("", id="cache-cost")
                yield Static("", id="cache-full")
                yield Static("", id="cache-save")
            with Container(classes="bottom-box"):
                yield Static("Last Session", id="last-label", classes="stat-label")
                yield Static("", id="last-cost")
                yield Static("", id="last-title")
                yield Static("", id="last-time")

        with Horizontal(id="footer-bar"):
            yield Static("", id="footer-status")

    def on_mount(self):
        """Start the poll timer."""
        self._table_timer = 0
        self._poll_timer = self.set_interval(3, self._poll_db)
        self._poll_db()  # immediate first load

    def _poll_db(self):
        """Re-read DB and refresh all widgets."""
        old_count = self.data.total_sessions
        self.data.refresh()
        self.data.recompute(self._time_mode)
        self._last_session_count = old_count
        self._table_timer += 1
        self._update_widgets()

    def action_refresh(self):
        """Manual refresh (keybinding 'r')."""
        self._table_timer = 999  # force table rebuild
        self._poll_db()

    def action_cycle_mode(self):
        """Cycle time period: all / month / week / day (keybinding 'm')."""
        idx = self._time_modes.index(self._time_mode)
        self._time_mode = self._time_modes[(idx + 1) % len(self._time_modes)]
        self.data.recompute(self._time_mode)
        self._table_timer = 999  # force table rebuild
        self._update_widgets()

    def action_cycle_plan(self):
        """Cycle pricing plan: Go / Zen (keybinding 'p')."""
        plans = ["go", "zen"]
        idx = plans.index(self.data.plan)
        new_plan = plans[(idx + 1) % len(plans)]
        self.data.recompute(self._time_mode, plan=new_plan)
        self._table_timer = 999
        self._update_widgets()

    def _update_widgets(self):
        d = self.data
        label = self._time_labels.get(self._time_mode, "All Time")
        rebuild_table = (
            self._first_load
            or d.total_sessions != self._last_session_count
            or self._table_timer >= 20
        )

        # ── Top stats (always update — cheap) ──
        self.query_one("#stat-cost-val", Static).update(_fmt_dollar(d.total_cost))
        self.query_one("#stat-cost-sub", Static).update(
            f"{label} · {d.models[0].model_id if d.models else '?'}"
        )

        self.query_one("#stat-cache-val", Static).update(_fmt_pct(d.cache_hit_rate))
        self.query_one("#stat-cache-sub", Static).update(
            f"{_fmt_tokens(d.total_cache_r)} cached / {_fmt_tokens(d.total_in)} fresh"
        )

        self.query_one("#stat-sess-val", Static).update(str(d.total_sessions))
        self.query_one("#stat-sess-sub", Static).update(
            f"{d.paid_sessions} paid · {d.total_sessions - d.paid_sessions} free"
        )

        self.query_one("#stat-save-val", Static).update(_fmt_dollar(d.cache_savings))
        self.query_one("#stat-save-sub", Static).update(
            f"{_fmt_pct(d.cache_savings / d.full_price_cost * 100) if d.full_price_cost > 0 else '0%'} of full price"
        )

        # ── Model table (only rebuild on change or every 60s) ──
        if rebuild_table:
            self._table_timer = 0
            self._first_load = False
            table = self.query_one("#model-table", DataTable)
            table.clear()  # clears rows only — columns persist
            table.show_header = self._headers_visible
            if d.models:
                if not self._cols_added:
                    cols = table.add_columns("Model", "Sess", "Cost", "In", "Out", "CacheR", "Hit%", "Bar")
                    for c, w in zip(cols, [20, 5, 9, 8, 7, 8, 6, 9]):
                        c.width = w
                    self._cols_added = True
                max_cost = d.models[0].cost if d.models else 1
                for m in d.models:
                    if m.cost < 0.01 and m.count <= 1:
                        continue
                    total_r = m.tin + m.cr
                    hit = m.cr / total_r * 100 if total_r > 0 else 0.0
                    row = [
                        m.model_id[:22],
                        str(m.count),
                        _fmt_dollar(m.cost),
                        _fmt_tokens(m.tin),
                        _fmt_tokens(m.tout),
                        _fmt_tokens(m.cr) if m.cr else "—",
                        _fmt_pct(hit),
                        _bar(m.cost, max_cost, 12),
                    ]
                    table.add_row(*row)
            self._table_timer = 0

        # ── Trend sparkline ──
        trend = d.daily_trend
        self.query_one("#trend-spark", Static).update(
            _sparkline(trend, 40) + f"  last 30d: {_fmt_dollar(sum(trend))}"
        )
        if trend:
            self.query_one("#trend-range", Static).update(
                f"min: {_fmt_dollar(min(trend))}  max: {_fmt_dollar(max(trend))}  avg: {_fmt_dollar(sum(trend)/len(trend))}"
            )

        # ── Cache box (always update) ──
        self.query_one("#cache-cost", Static).update(
            f"Cache cost (at rate):  {_fmt_dollar(d.cache_cost)}"
        )
        self.query_one("#cache-full", Static).update(
            f"Full-price equivalent: {_fmt_dollar(d.full_price_cost)}"
        )
        self.query_one("#cache-save", Static).update(
            f"Savings: {_fmt_dollar(d.cache_savings)} ({_fmt_pct(d.cache_savings / d.full_price_cost * 100) if d.full_price_cost > 0 else '0%'})"
        )

        # ── Last session ──
        self.query_one("#last-cost", Static).update(
            f"Cost: {_fmt_dollar(d.last_session_cost)}"
        )
        title = (d.last_session_title[:42] + "..") if len(d.last_session_title) > 42 else d.last_session_title
        self.query_one("#last-title", Static).update(
            f"Session: {title}" if title else "Session: —"
        )
        self.query_one("#last-time", Static).update(
            f"Updated: {d.last_updated}"
        )

        # ── Footer ──
        status = f" Polling every 3s · {d.active_sessions} active (last 5min)"
        status += f" \\[m]ode:{label} \\[p]lan:{d.plan_label} \\[r]efresh \\[q]uit"
        self.query_one("#footer-status", Static).update(status)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="OpenCode Cost Monitor TUI")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite DB path (default: {DEFAULT_DB})")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Error: DB not found at {args.db}", file=sys.stderr)
        sys.exit(1)

    app = CostMonitor(db_path=args.db)
    app.run()


if __name__ == "__main__":
    main()
