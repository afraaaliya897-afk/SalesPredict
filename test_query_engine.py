"""
Tests for query_engine.py.

1. Compiler + exclusion tests against synthetic data (no LLM required).
2. Four expected questions + one unanswerable, using injected plans
   (deterministic) and optionally the real Ollama planner.

Run:
    python generate_synthetic_query_data.py
    python test_query_engine.py
"""

from __future__ import annotations

import json
import sys

from query_engine import (
    ALLOWED_METRICS,
    answer_question,
    build_sql,
    execute,
    infer_chart_type,
    validate_plan,
    validate_sql,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))


def print_run(title: str, result: dict):
    print(f"\n--- {title} ---")
    print("plan:", json.dumps(result.get("plan_used"), default=str))
    debug = result.get("debug") or {}
    if debug.get("sql_query"):
        print("sql:", debug["sql_query"])
        print("params:", debug.get("sql_params"))
    print("chart_type:", result.get("chart_type"))
    print("answer:", result.get("answer_text"))
    print("table_data (first 5):", json.dumps(result.get("table_data", [])[:5], default=str))
    chart = result.get("chart_data") or {}
    print("chart labels:", chart.get("labels", [])[:8], "values:", chart.get("values", [])[:8])


def test_validate_plan():
    print("\n== validate_plan ==")
    ok, _ = validate_plan(
        {"metric": "issue_quantity", "dimension": "item_number", "date_range_days": 90, "sort": "desc", "limit": 5}
    )
    check("valid ranking plan", ok)

    ok, msg = validate_plan(
        {"metric": "unsupported", "dimension": None, "date_range_days": None, "sort": "desc", "limit": None}
    )
    check("unsupported metric rejected", not ok and "don't have data" in (msg or "").lower(), msg)

    ok, msg = validate_plan(
        {"metric": "profit_margin", "dimension": None, "date_range_days": None, "sort": "desc", "limit": None}
    )
    check("unknown metric rejected", not ok and "profit_margin" in (msg or ""), msg)

    ok, msg = validate_plan(
        {"metric": "issue_quantity", "dimension": "category", "date_range_days": None, "sort": "desc", "limit": 10}
    )
    check("unknown dimension rejected", not ok and "category" in (msg or ""), msg)


def test_compiler_and_exclusions():
    print("\n== compiler + exclusions ==")
    plan = {
        "metric": "issue_quantity",
        "dimension": "item_number",
        "date_range_days": None,
        "sort": "desc",
        "limit": 5,
    }
    ok, _ = validate_plan(plan)
    sql, params = build_sql(plan)
    check("validate_sql accepts compiler output", validate_sql(sql))
    check("reference is parameterized", "?" in sql and "Sales order" not in sql.split("WHERE")[0])
    check("Cancelled / Do not process in params", "Cancelled" in params and "Yes" in params)

    df = execute(sql, params)
    items = set(df["group_value"].astype(str)) if len(df) else set()
    check("POISON-ITEM excluded (cancelled)", "POISON-ITEM" not in items, str(items))

    # Non-sales-order references must not inflate issue_quantity
    from query_engine import DB_PATH
    import duckdb

    con = duckdb.connect(DB_PATH, read_only=True)
    raw_poison = con.execute(
        "SELECT SUM(issue) FROM inventory_transaction WHERE item_number = 'POISON-ITEM'"
    ).fetchone()[0]
    con.close()
    check("poison qty exists in raw inventory", raw_poison == 999_999, str(raw_poison))


def test_chart_types():
    print("\n== infer_chart_type ==")
    check("item ranking -> bar", infer_chart_type({"dimension": "item_number"}) == "bar")
    check("sale_date -> line", infer_chart_type({"dimension": "sale_date"}) == "line")
    check("no dimension -> stat", infer_chart_type({"dimension": None}) == "stat")


EXPECTED_PLANS = [
    {
        "question": "top 5 items by quantity sold in the last 90 days",
        "plan": {
            "metric": "issue_quantity",
            "dimension": "item_number",
            "date_range_days": 90,
            "sort": "desc",
            "limit": 5,
        },
        "chart": "bar",
    },
    {
        "question": "daily sales trend this month",
        "plan": {
            "metric": "issue_quantity",
            "dimension": "sale_date",
            "date_range_days": 30,
            "sort": "asc",
            "limit": None,
        },
        "chart": "line",
    },
    {
        "question": "total orders this month",
        "plan": {
            "metric": "order_count",
            "dimension": None,
            "date_range_days": 30,
            "sort": "desc",
            "limit": None,
        },
        "chart": "stat",
    },
    {
        "question": "top customers by order count",
        "plan": {
            "metric": "order_count",
            "dimension": "customer_account",
            "date_range_days": None,
            "sort": "desc",
            "limit": 10,
        },
        "chart": "bar",
    },
]


def test_injected_questions():
    print("\n== four questions (injected plans, no LLM) ==")
    for case in EXPECTED_PLANS:
        result = answer_question(case["question"], plan=case["plan"], skip_llm_explain=True)
        print_run(case["question"], result)
        check(f"{case['question']} chart_type", result["chart_type"] == case["chart"], str(result["chart_type"]))
        check(f"{case['question']} has table_data", len(result["table_data"]) > 0)
        if case["chart"] == "stat":
            check(f"{case['question']} single number", "metric_value" in result["table_data"][0])
        else:
            check(f"{case['question']} has chart labels", len(result["chart_data"]["labels"]) > 0)


def test_unanswerable():
    print("\n== unanswerable ==")
    result = answer_question(
        "what's our profit margin",
        plan={
            "metric": "unsupported",
            "dimension": None,
            "date_range_days": None,
            "sort": "desc",
            "limit": None,
        },
        skip_llm_explain=True,
    )
    print_run("what's our profit margin", result)
    text = (result["answer_text"] or "").lower()
    check("refuses instead of guessing", "don't have data" in text, result["answer_text"])
    check("no chart on refusal", result["chart_type"] is None)
    check("empty table on refusal", result["table_data"] == [])


def test_llm_if_available():
    print("\n== live Ollama planner (optional) ==")
    try:
        import ollama

        ollama.list()
    except Exception as e:
        print(f"  SKIP  Ollama not reachable ({e})")
        return

    from query_engine import get_query_plan

    cases = [
        ("top 5 items by quantity sold in the last 90 days", "issue_quantity", "item_number", "bar"),
        ("daily sales trend this month", "issue_quantity", "sale_date", "line"),
        ("total orders this month", "order_count", None, "stat"),
        ("top customers by order count", "order_count", "customer_account", "bar"),
        ("what's our profit margin", "unsupported", None, None),
    ]
    for question, metric, dimension, chart in cases:
        plan = get_query_plan(question)
        print(f"\n  LLM plan for {question!r}: {json.dumps(plan, default=str)}")
        result = answer_question(question)
        print_run(f"LLM e2e: {question}", result)
        check(f"LLM metric ~ {metric}", plan.get("metric") == metric, str(plan.get("metric")))
        dim = plan.get("dimension")
        if dim in ("", "null"):
            dim = None
        if metric != "unsupported":
            check(f"LLM dimension ~ {dimension}", dim == dimension, str(dim))
            check(f"LLM chart_type {chart}", result.get("chart_type") == chart, str(result.get("chart_type")))
        else:
            text = (result.get("answer_text") or "").lower()
            check("LLM unanswerable refused", "don't have data" in text, result.get("answer_text"))


if __name__ == "__main__":
    test_validate_plan()
    test_compiler_and_exclusions()
    test_chart_types()
    test_injected_questions()
    test_unanswerable()
    test_llm_if_available()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
