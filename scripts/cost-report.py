#!/usr/bin/env python3
"""Where the money went — spend and cascade report from local data only.

Hermes already records what a hosted tracer would collect: per-session model,
token counts, cache hits and estimated cost land in state.db on every API call,
and the cost-router writes each routing decision to router_log.jsonl. Nothing
about answering "what am I spending, and is the cascade earning its keep" needs
an external service.

That matters beyond convenience. A hosted tracer ships prompts and completions
to a third party by default, and these conversations carry personal material
and fund data. This reads the same numbers off disk instead, so the question
gets answered without the transcripts leaving the machine.

Usage:
    python3 scripts/cost-report.py                # last 30 days
    python3 scripts/cost-report.py --days 7
    python3 scripts/cost-report.py --all
    python3 scripts/cost-report.py --json
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import time
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DB = HOME / "state.db"
# The router log path is taken verbatim from config (`~/.hermes/...`), which the
# container expands against its own HOME — so it can land one level deeper.
ROUTER_LOGS = [HOME / ".hermes" / "router_log.jsonl", HOME / "router_log.jsonl"]

TIERS = {0: "SIMPLE", 1: "MEDIUM", 2: "HARD"}


def _fmt_usd(x: float) -> str:
    return f"${x:,.4f}" if x < 1 else f"${x:,.2f}"


def _rows(since_ts: float | None):
    if not DB.exists():
        raise SystemExit(f"state.db не найден: {DB}")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    # Real column names here are started_at / ended_at (REAL, epoch seconds).
    # Guessing created_at/updated_at silently found nothing, which disabled the
    # filter and made `--days 7` print the all-time totals under a 7-day
    # heading — a wrong number is worse than a missing one.
    ts_col = next((c for c in ("started_at", "ended_at") if c in cols), None)
    if since_ts and not ts_col:
        raise SystemExit(
            "в state.db нет колонки времени (started_at/ended_at) — используйте --all"
        )
    where, args = "estimated_cost_usd IS NOT NULL", []
    if since_ts:
        where += f" AND {ts_col} >= ?"
        args.append(since_ts)
    sel = "model, estimated_cost_usd, input_tokens, output_tokens, cache_read_tokens, api_call_count"
    return list(con.execute(f"SELECT {sel} FROM sessions WHERE {where}", args))


def _router_rows():
    for p in ROUTER_LOGS:
        if p.exists():
            out = []
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            return out, p
    return [], None


def main() -> int:
    ap = argparse.ArgumentParser(description="Отчёт по расходам Hermes (локальные данные).")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--all", action="store_true", help="за всё время")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    since = None if args.all else time.time() - args.days * 86400
    rows = _rows(since)
    if not rows and not args.all:
        # Sessions written before the timestamp column existed have no usable
        # date; say so rather than reporting a confident zero.
        print(f"За последние {args.days} дн. записей нет — попробуйте --all")
        return 0

    by_model = collections.defaultdict(lambda: {"cost": 0.0, "in": 0, "out": 0, "cache": 0, "calls": 0, "n": 0})
    for model, cost, tin, tout, tcache, calls in rows:
        m = (model or "unknown").split("/")[-1]
        d = by_model[m]
        d["cost"] += float(cost or 0)
        d["in"] += int(tin or 0)
        d["out"] += int(tout or 0)
        d["cache"] += int(tcache or 0)
        d["calls"] += int(calls or 0)
        d["n"] += 1

    total = sum(d["cost"] for d in by_model.values())
    rlog, rpath = _router_rows()

    if args.json:
        print(json.dumps({
            "period_days": None if args.all else args.days,
            "total_usd": round(total, 4),
            "sessions": len(rows),
            "by_model": {k: {kk: (round(vv, 6) if kk == "cost" else vv) for kk, vv in v.items()}
                         for k, v in by_model.items()},
            "router_decisions": len(rlog),
        }, ensure_ascii=False, indent=2))
        return 0

    period = "за всё время" if args.all else f"за {args.days} дн."
    print(f"# Расходы Hermes — {period}")
    print(f"  всего: {_fmt_usd(total)}   сессий: {len(rows)}\n")

    print(f"  {'модель':<30} {'$':>10} {'доля':>7} {'сессий':>7} {'вызовов':>8} {'кэш-хиты':>10}")
    for m, d in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"]):
        share = (d["cost"] / total * 100) if total else 0
        print(f"  {m:<30} {d['cost']:>10.4f} {share:>6.1f}% {d['n']:>7} {d['calls']:>8} {d['cache']:>10,}")

    if not rlog:
        print("\n  (router_log.jsonl не найден — каскад не логировался)")
        return 0

    # ── Cascade ──────────────────────────────────────────────────────────────
    print(f"\n# Каскад — {len(rlog)} решений ({rpath})")
    by_pair = collections.Counter((TIERS.get(r.get("tier"), "?"), (r.get("model") or "").split("/")[-1])
                                  for r in rlog)
    for (tier, model), n in by_pair.most_common():
        print(f"  {tier:<7} -> {model:<30} {n:>5}")

    ok = sum(1 for r in rlog if r.get("success") is True)
    esc = sum(1 for r in rlog if r.get("escalated"))
    print(f"\n  успешных: {ok}/{len(rlog)} ({ok/len(rlog)*100:.0f}%)   эскалаций: {esc}")

    # A HARD turn served by a MEDIUM-capability rung is the interesting case:
    # it means the ladder had nothing stronger available, not that the guess was
    # wrong. Worth surfacing rather than averaging away.
    cheap = {"deepseek-v4-flash", "gemini-3-flash-preview"}
    hard_on_cheap = sum(n for (tier, model), n in by_pair.items() if tier == "HARD" and model in cheap)
    if hard_on_cheap:
        print(f"  ⚠ HARD-задач на дешёвой ступени: {hard_on_cheap} "
              "(флагман был недоступен в лесенке или не потребовался)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
