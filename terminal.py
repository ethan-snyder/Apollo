"""
Apollo — interactive terminal.

A single shell over every action in the project: collection, signals,
backfill, backtesting, event studies, and paper trading.

Design notes:
  * Commands are typed, not numbered. Numbers stop scaling past ~10 options
    and force a lookup every time; `sig btc` is guessable and stays short.
  * Every command is dispatched through one table so `help` can't drift out
    of sync with what actually exists.
  * The status line reads live from the DB on each redraw, so the shell is
    useful as an at-a-glance monitor even when idle.

Run with `python terminal.py`, or `python main.py term`.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

import ui
from ui import (BEAR, BULL, FG_DIM, FG_FAINT, FG_TEXT, GOLD, INFO, NEUTRAL,
                WARN, c, shade)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

import store                                    # noqa: E402
from config import ASSETS                       # noqa: E402


# ---------------------------------------------------------------------------
# Status line
# ---------------------------------------------------------------------------

def status_line() -> str:
    """Compact live snapshot: article counts, latest signals, price context."""
    try:
        store.init_db()
        conn = store.get_conn()
        total = conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
        hour = conn.execute(
            "SELECT COUNT(*) c FROM articles "
            "WHERE ingested_at >= datetime('now','-1 hour')").fetchone()["c"]
    except Exception:
        return c("  (database unavailable)", WARN)

    bits = [
        c("articles ", FG_FAINT) + c(f"{total:,}", FG_TEXT),
        c("1h ", FG_FAINT) + c(f"{hour:,}", FG_TEXT),
    ]

    for asset in ("BTC", "ETH", "SOL"):
        try:
            row = conn.execute(
                "SELECT direction, net_impact FROM signals "
                "WHERE asset = ? AND window_min = 60 "
                "ORDER BY computed_at DESC LIMIT 1", (asset,)).fetchone()
        except Exception:
            row = None
        if row:
            col = ui.direction_color(row["direction"])
            bits.append(c(f"{asset} ", FG_FAINT)
                        + c(ui.arrow(row["direction"]).strip(), col)
                        + c(f"{row['net_impact']:+.2f}", col))
        else:
            bits.append(c(f"{asset} ", FG_FAINT) + c("—", FG_FAINT))

    sep = c("  │  ", shade(GOLD, 0.25))
    return "  " + sep.join(bits)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_help(args: list[str]) -> None:
    print(ui.header("COMMANDS"))
    groups = [
        ("collection", [
            ("run [names]", "start the collector daemon (Ctrl+C to stop)"),
            ("once", "one collection pass across every source"),
            ("feeds", "reachability check on all feeds"),
            ("stats", "database statistics"),
            ("sources", "top sources by article volume"),
        ]),
        ("signals", [
            ("sig [asset]", "current signals, all windows"),
            ("top [asset]", "highest-impact recent articles"),
            ("watch [asset]", "auto-refreshing signal monitor"),
            ("why <id>", "explain one article's score"),
        ]),
        ("history & testing", [
            ("backfill [start] [end]", "download historical prices + news"),
            ("coverage", "what history is loaded"),
            ("bt [asset] [start] [end]", "run a backtest"),
            ("events [asset] [start] [end]", "event study (CAR by category)"),
            ("runs", "saved backtest / event-study runs"),
        ]),
        ("validation", [
            ("ll [asset] [start] [end]", "lead-lag, split by lexicon half"),
            ("val [asset] [start] [end]", "lead-lag + four null controls"),
            ("controls [start] [end]", "fetch SPY/GLD control series"),
        ]),
        ("paper trading", [
            ("paper [dur] [assets]", "start a run (simulated, $10k)"),
            ("papers", "list saved paper runs"),
            ("prun <id>", "trade log for one run"),
        ]),
        ("shell", [
            ("sec <ticker>", "recent SEC filings"),
            ("export [path]", "dump articles to JSONL"),
            ("clear", "clear the screen"),
            ("help", "this list"),
            ("quit", "exit"),
        ]),
    ]
    for title, items in groups:
        print(ui.section(title))
        for cmdname, desc in items:
            print(c(f"    {cmdname:<28}", GOLD) + c(desc, FG_DIM))
    print()


def cmd_sig(args: list[str]) -> None:
    import main as app
    asset = args[0].upper() if args else None
    store.init_db()

    sql = ("SELECT s.* FROM signals s JOIN (SELECT asset, window_min, "
           "MAX(computed_at) mx FROM signals GROUP BY asset, window_min) l "
           "ON s.asset=l.asset AND s.window_min=l.window_min "
           "AND s.computed_at=l.mx")
    params: list = []
    if asset:
        sql += " WHERE s.asset = ?"
        params.append(asset)
    sql += " ORDER BY s.asset, s.window_min"

    rows = store.get_conn().execute(sql, params).fetchall()
    if not rows:
        print(ui.warn("No signals yet. Run `once` first."))
        return

    print(ui.header("SIGNALS"))
    by_asset: dict[str, list] = {}
    for r in rows:
        by_asset.setdefault(r["asset"], []).append(r)

    for a, sigs in by_asset.items():
        pair = ASSETS.get(a, {}).get("pair") or "market-wide"
        print(ui.section(f"{a}  ({pair})"))
        print(c(f"    {'window':>7}  {'direction':<16} {'impact':>9} "
                f"{'conf':>6} {'n':>5}   strength", FG_FAINT))
        for s in sigs:
            w = s["window_min"]
            label = f"{w}m" if w < 60 else f"{w // 60}h"
            col = ui.direction_color(s["direction"])
            bar = ui.sparkbar(s["net_impact"] or 0.0, -3.0, 3.0, 20)
            direction, impact = s["direction"], s["net_impact"]
            conf, count = s["confidence"], s["article_count"]
            print("    "
                  + c(f"{label:>7}", FG_DIM) + "  "
                  + c(ui.arrow(direction), col)
                  + c(f"{direction:<14}", col) + " "
                  + c(f"{impact:>+9.3f}", col) + " "
                  + c(f"{conf:>6.2f}", FG_TEXT) + " "
                  + c(f"{count:>5}", FG_DIM) + "   " + bar)

        hour = next((s for s in sigs if s["window_min"] == 60), None)
        if hour and hour["detail"]:
            try:
                drivers = json.loads(hour["detail"]).get("top_drivers", [])
            except json.JSONDecodeError:
                drivers = []
            if drivers:
                print(c("\n    top drivers (1h)", FG_FAINT))
                for d in drivers[:3]:
                    col = BULL if d["impact"] > 0 else BEAR
                    print("      "
                          + c(f"{d['impact']:+.2f}", col) + "  "
                          + c(d["title"][:62], FG_TEXT) + "  "
                          + c(f"[{d['source']}]", FG_FAINT))
    print()


def cmd_watch(args: list[str]) -> None:
    """Auto-refreshing monitor. Ctrl+C returns to the shell."""
    import time
    asset = args[0].upper() if args else None
    interval = 15
    print(ui.note(f"refreshing every {interval}s — Ctrl+C to stop"))
    try:
        while True:
            ui.clear()
            print(ui.banner("live signal monitor"))
            print(status_line())
            cmd_sig([asset] if asset else [])
            print(c(f"  updated {datetime.now(timezone.utc):%H:%M:%S} UTC",
                    FG_FAINT))
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n" + ui.note("stopped"))


def cmd_top(args: list[str]) -> None:
    asset = args[0].upper() if args and args[0].upper() in ASSETS else None
    store.init_db()
    rows = store.recent_articles(limit=25, asset=asset, minutes=1440,
                                 min_abs_impact=0.05)
    print(ui.header("HIGHEST IMPACT", f"last 24h · {asset or 'all assets'}"))
    if not rows:
        print(ui.warn("Nothing scored in this window yet."))
        return
    for r in sorted(rows, key=lambda x: -abs(x["impact"] or 0)):
        col = BULL if (r["impact"] or 0) > 0 else BEAR
        assets = ", ".join(json.loads(r["assets"] or "[]")) or "-"
        when = (r["published_at"] or "")[:16].replace("T", " ")
        corro = (c(f"  ×{r['corroboration']}", WARN)
                 if r["corroboration"] > 1 else "")
        print("\n  "
              + c(f"{r['impact']:+.3f}", col, bold=True) + "  "
              + c(f"[{assets[:22]}]", FG_FAINT) + "  "
              + c(when, FG_FAINT) + "  " + c(r["source"], INFO) + corro)
        print("      " + c(r["title"][:92], FG_TEXT))
        print(c(f"      id {r['id']} — `why {r['id']}` to explain", FG_FAINT))
    print()


def cmd_why(args: list[str]) -> None:
    """Explain exactly why one article scored the way it did."""
    if not args:
        print(ui.warn("usage: why <article_id>"))
        return
    store.init_db()
    try:
        row = store.get_conn().execute(
            "SELECT * FROM articles WHERE id = ?", (int(args[0]),)).fetchone()
    except (ValueError, TypeError):
        print(ui.warn("article id must be a number"))
        return
    if not row:
        print(ui.warn(f"no article #{args[0]}"))
        return

    print(ui.header(f"ARTICLE #{row['id']}"))
    print(f"\n  {c(row['title'], FG_TEXT, bold=True)}")
    print(ui.kv("source", f"{row['source']}  ({row['source_category']})"))
    print(ui.kv("published", str(row["published_at"])[:19]))
    print(ui.kv("corroboration", f"{row['corroboration']} outlet(s)"))
    if row["url"]:
        print(ui.kv("url", row["url"][:70], value_rgb=INFO))

    col = BULL if (row["impact"] or 0) > 0 else BEAR
    print(ui.section("scores"))
    print(ui.kv("sentiment", f"{row['sentiment']:+.3f}",
                value_rgb=BULL if row["sentiment"] > 0 else BEAR))
    print(ui.kv("relevance", f"{row['relevance']:.3f}"))
    print(ui.kv("source weight", f"{row['source_weight']:.2f}"))
    print(ui.kv("impact", f"{row['impact']:+.4f}", value_rgb=col))

    try:
        detail = json.loads(row["score_detail"] or "{}")
    except json.JSONDecodeError:
        detail = {}

    rel = detail.get("asset_relevance") or {}
    if rel:
        print(ui.section("asset relevance"))
        for a, v in sorted(rel.items(), key=lambda kv: -kv[1]):
            print(f"    {c(f'{a:<8}', GOLD)}{c(f'{v:.3f}', FG_TEXT)}  "
                  f"{ui.sparkbar(v, 0, 1, 24)}")

    matches = detail.get("matches") or []
    if matches:
        print(ui.section("lexicon matches"))
        for m in matches:
            mc = BULL if m["weight"] > 0 else BEAR
            print("    " + c(f"{m['weight']:+.3f}", mc) + "  "
                  + c(m["term"], FG_TEXT))
    else:
        print(ui.section("lexicon matches"))
        print(ui.note("none — no phrase in the lexicon matched this text"))

    print(ui.section("modifiers"))
    print(ui.kv("recency factor", f"{detail.get('recency', '?')}"))
    print(ui.kv("high-impact event",
                "yes" if detail.get("high_impact_event") else "no",
                value_rgb=WARN if detail.get("high_impact_event") else FG_DIM))
    print()


def cmd_sources(args: list[str]) -> None:
    store.init_db()
    rows = store.get_conn().execute(
        "SELECT source, COUNT(*) c, AVG(ABS(impact)) ai FROM articles "
        "GROUP BY source ORDER BY c DESC LIMIT 25").fetchall()
    print(ui.header("SOURCES", "by article volume"))
    if not rows:
        print(ui.warn("No articles yet."))
        return
    mx = max(r["c"] for r in rows)
    print(c(f"\n  {'source':<28} {'articles':>9}  {'avg |impact|':>12}", FG_FAINT))
    print(ui.subrule())
    for r in rows:
        src, n, avg = r["source"][:27], r["c"], (r["ai"] or 0)
        print("  " + c(f"{src:<28}", FG_TEXT)
              + c(f"{n:>9,}", GOLD) + "  "
              + c(f"{avg:>12.4f}", FG_DIM) + "  "
              + ui.sparkbar(n, 0, mx, 16))
    print()


def cmd_stats(args: list[str]) -> None:
    import main as app
    app.show_stats()


def cmd_feeds(args: list[str]) -> None:
    import main as app
    app.check_feeds()


def cmd_once(args: list[str]) -> None:
    import main as app
    app.collect_once()


def cmd_run(args: list[str]) -> None:
    import daemon
    print(ui.note("daemon starting — Ctrl+C returns to the shell"))
    try:
        daemon.main(only=args[0].split(",") if args else None)
    except KeyboardInterrupt:
        pass
    print("\n" + ui.note("daemon stopped"))


def cmd_backfill(args: list[str]) -> None:
    import main as app
    app.do_backfill(args[0] if args else "2025-01-01",
                    args[1] if len(args) > 1 else "2026-01-01")


def cmd_coverage(args: list[str]) -> None:
    import main as app
    app.show_coverage()


def cmd_bt(args: list[str]) -> None:
    import main as app
    app.do_backtest((args[0].upper() if args else "BTC"),
                    args[1] if len(args) > 1 else "2025-01-01",
                    args[2] if len(args) > 2 else "2026-01-01")


def cmd_events(args: list[str]) -> None:
    import main as app
    app.do_event_study((args[0].upper() if args else "BTC"),
                       args[1] if len(args) > 1 else "2025-01-01",
                       args[2] if len(args) > 2 else "2026-01-01")


def _range_args(args: list[str], n: int = 0) -> tuple[str, str]:
    """Pull [start] [end] out of argv, defaulting to the full 2025 window."""
    return (args[n] if len(args) > n else "2025-01-01",
            args[n + 1] if len(args) > n + 1 else "2026-01-01")


def cmd_leadlag(args: list[str]) -> None:
    import main as app
    start, end = _range_args(args, 1)
    app.do_leadlag((args[0].upper() if args else "BTC"), start, end)


def cmd_validate(args: list[str]) -> None:
    import main as app
    start, end = _range_args(args, 1)
    print(ui.note("running bootstrap and placebo studies — this takes a "
                  "minute"))
    app.do_validate((args[0].upper() if args else "BTC"), start, end)


def cmd_controls(args: list[str]) -> None:
    import main as app
    start, end = _range_args(args, 0)
    app.do_controls(start, end)


def cmd_runs(args: list[str]) -> None:
    store.init_db()
    rows = store.list_backtests(30)
    print(ui.header("SAVED RUNS", "backtests & event studies"))
    if not rows:
        print(ui.warn("None yet."))
        return
    print(c(f"\n  {'id':<5} {'when':<17} {'asset':<7} {'range':<26} label",
            FG_FAINT))
    print(ui.subrule())
    for r in rows:
        rng = f"{r['start_date']} .. {r['end_date']}"
        rid, when = f"#{r['id']}", r["run_at"][:16]
        asset = r["asset"] or ""
        print("  " + c(f"{rid:<5}", GOLD) + " "
              + c(f"{when:<17}", FG_DIM) + " "
              + c(f"{asset:<7}", FG_TEXT) + " "
              + c(f"{rng:<26}", FG_DIM) + " "
              + c(r["label"] or "", FG_TEXT))
    print()


def cmd_paper(args: list[str]) -> None:
    import main as app
    app.do_paper(args[0] if args else "1h",
                 args[1] if len(args) > 1 else None)


def cmd_papers(args: list[str]) -> None:
    store.init_db()
    rows = store.list_paper_runs(30)
    print(ui.header("PAPER RUNS", "simulated — no real orders"))
    if not rows:
        print(ui.warn("None yet. Try `paper 1h`."))
        return
    print(c(f"\n  {'id':<5} {'started':<17} {'status':<11} {'start':>10} "
            f"{'end':>11} {'return':>9}", FG_FAINT))
    print(ui.subrule())
    for r in rows:
        end_eq = r["ending_equity"] or 0.0
        ret = (100 * (end_eq / r["starting_cash"] - 1)
               if end_eq and r["starting_cash"] else 0.0)
        col = BULL if ret > 0 else (BEAR if ret < 0 else NEUTRAL)
        rid, when = f"#{r['id']}", r["started_at"][:16]
        status, start_cash = r["status"] or "", r["starting_cash"]
        print("  " + c(f"{rid:<5}", GOLD) + " "
              + c(f"{when:<17}", FG_DIM) + " "
              + c(f"{status:<11}", FG_TEXT) + " "
              + c(f"${start_cash:>9,.0f}", FG_DIM) + " "
              + c(f"${end_eq:>10,.2f}", FG_TEXT) + " "
              + c(f"{ret:>+8.2f}%", col))
    print()


def cmd_prun(args: list[str]) -> None:
    import main as app
    if not args:
        print(ui.warn("usage: prun <id>"))
        return
    app.show_paper_run(args[0])


def cmd_sec(args: list[str]) -> None:
    import main as app
    if not args:
        print(ui.warn("usage: sec <ticker>"))
        return
    app.lookup_ticker(args[0].upper())


def cmd_export(args: list[str]) -> None:
    import main as app
    app.export(args[0] if args else "apollo_export.jsonl")


def cmd_clear(args: list[str]) -> None:
    ui.clear()
    print(ui.banner())
    print(status_line())
    print()


COMMANDS = {
    "help": cmd_help, "?": cmd_help, "h": cmd_help,
    "sig": cmd_sig, "signals": cmd_sig, "s": cmd_sig,
    "watch": cmd_watch, "w": cmd_watch,
    "top": cmd_top, "t": cmd_top,
    "why": cmd_why,
    "sources": cmd_sources,
    "stats": cmd_stats,
    "feeds": cmd_feeds,
    "once": cmd_once,
    "run": cmd_run, "daemon": cmd_run,
    "backfill": cmd_backfill,
    "coverage": cmd_coverage,
    "bt": cmd_bt, "backtest": cmd_bt,
    "events": cmd_events, "es": cmd_events,
    "ll": cmd_leadlag, "leadlag": cmd_leadlag,
    "val": cmd_validate, "validate": cmd_validate,
    "controls": cmd_controls,
    "runs": cmd_runs, "backtests": cmd_runs,
    "paper": cmd_paper,
    "papers": cmd_papers,
    "prun": cmd_prun,
    "sec": cmd_sec,
    "export": cmd_export,
    "clear": cmd_clear, "cls": cmd_clear,
}

EXIT = {"quit", "exit", "q", ":q"}


def dispatch(line: str) -> bool:
    """Run one command. Returns False when the shell should exit."""
    parts = line.strip().split()
    if not parts:
        return True
    name, args = parts[0].lower(), parts[1:]

    if name in EXIT:
        return False

    fn = COMMANDS.get(name)
    if not fn:
        print(ui.fail(f"unknown command: {name}"))
        near = [k for k in COMMANDS if k.startswith(name[:2])][:5]
        if near:
            print(ui.note("did you mean: " + ", ".join(sorted(set(near)))))
        else:
            print(ui.note("`help` lists everything"))
        return True

    try:
        fn(args)
    except KeyboardInterrupt:
        print("\n" + ui.note("interrupted"))
    except Exception as exc:
        print(ui.fail(f"{type(exc).__name__}: {exc}"))
    return True


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else []

    # Non-interactive: `python terminal.py sig BTC` runs one command and exits.
    if argv:
        store.init_db()
        dispatch(" ".join(argv))
        return

    ui.clear()
    print(ui.banner())
    print(status_line())
    print()
    print(ui.note("`help` for commands, `quit` to exit"))

    while True:
        try:
            print()
            line = input(ui.prompt())
        except (EOFError, KeyboardInterrupt):
            print("\n" + ui.note("bye"))
            break
        if not dispatch(line):
            print(ui.note("bye"))
            break


if __name__ == "__main__":
    main(sys.argv[1:])
