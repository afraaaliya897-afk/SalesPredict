"""
Spider + BIRD style evaluation of the existing text-to-SQL pipeline.

Does not change generate_sql / check_sql / execute. Official Spider/BIRD
leaderboards use many SQLite databases; this run uses the same metrics on
a gold set for sales_inventory.duckdb (v_orders / v_sold).
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

import pandas as pd

from query_engine import execute, resolve_model
from text_to_sql import check_sql, enforce_limit, generate_sql

ROOT = Path(__file__).resolve().parent
GOLD_PATH = ROOT / "eval" / "spider_bird_gold.json"
OUT_PATH = ROOT / "eval" / "spider_bird_results.json"
DB_PATH = str(ROOT / "sales_inventory.duckdb")


def _cell(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float)):
        return round(float(val), 4)
    text = str(val).strip()
    return None if text.lower() in ("none", "nan", "") else text


def _rows(df: pd.DataFrame, numeric_only: bool = False) -> list[tuple]:
    if df is None or len(df) == 0:
        return []
    work = df
    if numeric_only:
        cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        work = df[cols] if cols else df
    out = []
    for _, row in work.iterrows():
        out.append(tuple(_cell(v) for v in row.tolist()))
    return out


def execution_match(gold_df, pred_df, ordered: bool, numeric_only: bool = False) -> bool:
    g = _rows(gold_df, numeric_only)
    p = _rows(pred_df, numeric_only)
    if ordered:
        return g == p
    return Counter(g) == Counter(p)


def soft_f1(gold_df, pred_df) -> float:
    g = Counter(_rows(gold_df))
    p = Counter(_rows(pred_df))
    if not g and not p:
        return 1.0
    if not g or not p:
        return 0.0
    overlap = sum((g & p).values())
    precision = overlap / sum(p.values())
    recall = overlap / sum(g.values())
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def canonical_sql(sql: str | None) -> str | None:
    if not sql:
        return None
    try:
        import sqlglot

        parsed = sqlglot.parse_one(sql, read="duckdb")
        return parsed.sql(dialect="duckdb") if parsed is not None else sql.strip()
    except Exception:
        return " ".join(sql.split())


def exact_match(gold_sql: str, pred_sql: str | None) -> bool:
    g = canonical_sql(gold_sql)
    p = canonical_sql(pred_sql)
    return bool(g and p and g == p)


def rves_reward(gold_ms: float, pred_ms: float) -> float:
    """BIRD Mini-Dev reward bins on time_gold / time_pred."""
    if pred_ms <= 0:
        return 0.25
    ratio = gold_ms / pred_ms
    if ratio >= 2:
        return 1.25
    if ratio >= 1:
        return 1.0
    if ratio >= 0.5:
        return 0.75
    if ratio >= 0.25:
        return 0.5
    return 0.25


def run_sql(sql: str) -> tuple[pd.DataFrame | None, float, str | None]:
    t0 = time.perf_counter()
    try:
        df = execute(sql, db_path=DB_PATH)
        ms = (time.perf_counter() - t0) * 1000
        return df, ms, None
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        return None, ms, str(exc)


def evaluate_one(ex: dict, model: str, skip_llm: bool) -> dict:
    gold_sql = ex["gold_sql"]
    gold_df, gold_ms, gold_err = run_sql(gold_sql)
    rec = {
        "id": ex["id"],
        "question": ex["question"],
        "spider_difficulty": ex["spider_difficulty"],
        "bird_difficulty": ex["bird_difficulty"],
        "gold_sql": gold_sql,
        "gold_error": gold_err,
        "pred_sql": None,
        "pred_error": None,
        "ex": 0,
        "em": 0,
        "soft_f1": 0.0,
        "rves": 0.0,
        "gold_ms": round(gold_ms, 2),
        "pred_ms": None,
        "llm_ms": 0,
    }
    if gold_err or gold_df is None:
        rec["pred_error"] = "gold SQL failed"
        return rec

    if skip_llm:
        rec["pred_sql"] = gold_sql
        rec["pred_ms"] = rec["gold_ms"]
        rec["ex"] = 1
        rec["em"] = 1
        rec["soft_f1"] = 1.0
        rec["rves"] = 1.0
        return rec

    generated = generate_sql(ex["question"], model, DB_PATH)
    rec["llm_ms"] = generated.get("llm_ms") or 0
    pred = generated.get("sql")
    rec["pred_sql"] = pred
    rec["sql_raw"] = (generated.get("raw") or "")[:500]

    if pred in (None, "FORECAST", "UNSUPPORTED"):
        rec["pred_error"] = f"no executable SQL ({pred})"
        return rec

    ok, reason = check_sql(pred)
    if not ok:
        rec["pred_error"] = f"sql guard: {reason}"
        return rec

    pred_sql = enforce_limit(pred)
    pred_df, pred_ms, pred_err = run_sql(pred_sql)
    rec["pred_ms"] = round(pred_ms, 2)
    if pred_err or pred_df is None:
        rec["pred_error"] = pred_err
        return rec

    numeric_only = bool(ex.get("numeric_only"))
    ordered = bool(ex.get("ordered"))
    rec["em"] = int(exact_match(gold_sql, pred))
    rec["soft_f1"] = round(soft_f1(gold_df, pred_df), 4)
    if execution_match(gold_df, pred_df, ordered, numeric_only):
        rec["ex"] = 1
        rec["rves"] = rves_reward(gold_ms, pred_ms)
    elif numeric_only and execution_match(gold_df, pred_df, False, True):
        rec["ex"] = 1
        rec["rves"] = rves_reward(gold_ms, pred_ms)
    return rec


def summarize(rows: list[dict], model: str) -> dict:
    n = len(rows)
    def avg(key):
        return round(sum(r[key] for r in rows) / n, 4) if n else 0.0

    def slice_acc(field, value):
        part = [r for r in rows if r[field] == value]
        if not part:
            return None
        return {
            "n": len(part),
            "ex": round(sum(r["ex"] for r in part) / len(part), 4),
            "em": round(sum(r["em"] for r in part) / len(part), 4),
            "soft_f1": round(sum(r["soft_f1"] for r in part) / len(part), 4),
            "rves": round(sum(r["rves"] for r in part) / len(part), 4),
        }

    return {
        "model": model,
        "n": n,
        "spider": {
            "execution_accuracy": avg("ex"),
            "exact_match": avg("em"),
            "by_difficulty": {
                k: slice_acc("spider_difficulty", k)
                for k in ("easy", "medium", "hard", "extra")
                if slice_acc("spider_difficulty", k)
            },
        },
        "bird": {
            "execution_accuracy": avg("ex"),
            "soft_f1": avg("soft_f1"),
            "rves": avg("rves"),
            "by_difficulty": {
                k: slice_acc("bird_difficulty", k)
                for k in ("simple", "moderate", "challenging")
                if slice_acc("bird_difficulty", k)
            },
        },
        "published_context": {
            "spider_official": "Yale Spider: cross-domain SQLite, 200 DBs. Typical GPT-4 EX ~86% on dev; human ~92%.",
            "bird_official": "BIRD: 95 large DBs. Human EX ~92.96% (engineers). GPT-4 EX ~46-54% without extras. SOTA 2025 ~75% EX.",
            "this_run": "Same metric definitions on the SalesPrediction gold set. Scores are not leaderboard-comparable.",
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=None)
    parser.add_argument("--skip-llm", action="store_true", help="Sanity-check gold SQL only")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    spec = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    examples = spec["examples"]
    if args.limit:
        examples = examples[: args.limit]
    model = resolve_model(args.model)

    print(f"Evaluating {len(examples)} questions  model={model}  skip_llm={args.skip_llm}")
    rows = []
    for i, ex in enumerate(examples, 1):
        print(f"[{i}/{len(examples)}] {ex['id']} ...", flush=True)
        rec = evaluate_one(ex, model, args.skip_llm)
        mark = "EX" if rec["ex"] else "miss"
        print(f"    {mark}  EM={rec['em']}  F1={rec['soft_f1']}  {rec.get('pred_error') or ''}", flush=True)
        rows.append(rec)

    summary = summarize(rows, model)
    payload = {"summary": summary, "examples": rows, "gold_meta": {k: spec[k] for k in spec if k != "examples"}}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
