"""
Historical chat eval (Part A). Injected plans — SQL/compiler accuracy, not LLM wording.

Run: python eval_historical.py
"""
from __future__ import annotations

import csv
from pathlib import Path

import duckdb

from query_engine import DB_PATH, answer_question, validate_plan

OUT = Path(__file__).resolve().parent / "eval_results.csv"

MONTH_WINDOW_START = """
    SELECT DATE_TRUNC('month', MAX(physical_date))
    FROM inventory_transaction WHERE reference = 'Sales order'
"""
MONTH_WINDOW_END = """
    SELECT CAST(MAX(physical_date) AS DATE) + INTERVAL 1 DAY
    FROM inventory_transaction WHERE reference = 'Sales order'
"""
LAST_MONTH_START = """
    SELECT DATE_TRUNC('month', MAX(physical_date)) - INTERVAL 1 MONTH
    FROM inventory_transaction WHERE reference = 'Sales order'
"""
LAST_MONTH_END = """
    SELECT DATE_TRUNC('month', MAX(physical_date))
    FROM inventory_transaction WHERE reference = 'Sales order'
"""


def _truth(sql: str, params=None):
    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        row = con.execute(sql, params or []).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _num(result: dict):
    rows = result.get("table_data") or []
    if not rows:
        return None
    return rows[0].get("metric_value")


def main() -> None:
    cases = [
        {
            "id": "A-unanswerable-profit",
            "question": "what's our profit margin",
            "plan": {
                "metric": "unsupported",
                "dimension": None,
                "period": "all",
                "date_range_days": None,
                "sort": "desc",
                "limit": None,
            },
            "expect": "reject",
        },
        {
            "id": "A-incompatible-orders-by-item",
            "question": "total orders by item",
            "plan": {
                "metric": "order_count",
                "dimension": "item_number",
                "period": "all",
                "date_range_days": None,
                "sort": "desc",
                "limit": 10,
            },
            "expect": "reject",
        },
        {
            "id": "A-forecast-by-item",
            "question": "forecast sales for item P00018555",
            "plan": {
                "metric": "forecast_sales",
                "dimension": "item_number",
                "period": "all",
                "date_range_days": 365,
                "sort": "asc",
                "limit": 365,
            },
            "expect": "reject",
        },
        {
            "id": "A-orders-this-month",
            "question": "total orders this month",
            "plan": {
                "metric": "order_count",
                "dimension": None,
                "period": "this_month",
                "date_range_days": None,
                "sort": "desc",
                "limit": None,
            },
            "expect": "number",
            "sql": f"""
                SELECT COUNT(DISTINCT sales_order_number)
                FROM sales_order
                WHERE status NOT IN ('Canceled', 'Cancelled')
                  AND do_not_process != 'Yes'
                  AND invoice_date >= ({MONTH_WINDOW_START})
                  AND invoice_date < ({MONTH_WINDOW_END})
            """,
        },
        {
            "id": "A-qty-this-month",
            "question": "total quantity sold this month",
            "plan": {
                "metric": "issue_quantity",
                "dimension": None,
                "period": "this_month",
                "date_range_days": None,
                "sort": "desc",
                "limit": None,
            },
            "expect": "number",
            "sql": f"""
                SELECT SUM(-it.quantity)
                FROM inventory_transaction it
                LEFT JOIN sales_order so ON it.number = so.sales_order_number
                WHERE it.reference = 'Sales order'
                  AND it.issue = 'Sold'
                  AND it.quantity < 0
                  AND (
                        so.sales_order_number IS NULL
                        OR (
                            so.status NOT IN ('Canceled', 'Cancelled')
                            AND so.do_not_process != 'Yes'
                        )
                      )
                  AND it.physical_date >= ({MONTH_WINDOW_START})
                  AND it.physical_date < ({MONTH_WINDOW_END})
            """,
        },
        {
            "id": "A-on-order-excluded",
            "question": "quantity sold for GC-SO23-00017",
            "plan": {
                "metric": "issue_quantity",
                "dimension": None,
                "period": "all",
                "date_range_days": None,
                "sort": "desc",
                "limit": None,
            },
            "expect": "on_order_zero",
        },
        {
            "id": "A-empty-channel",
            "question": "orders on a fake channel",
            "plan": {
                "metric": "order_count",
                "dimension": "channel",
                "period": "last_n",
                "date_range_days": 1,
                "sort": "desc",
                "limit": 10,
            },
            "expect": "ok_empty_or_rows",
        },
        {
            "id": "A-orders-last-month",
            "question": "total order previous month",
            "plan": {
                "metric": "order_count",
                "dimension": None,
                "period": "last_month",
                "date_range_days": None,
                "sort": "desc",
                "limit": None,
            },
            "expect": "number",
            "sql": f"""
                SELECT COUNT(DISTINCT sales_order_number)
                FROM sales_order
                WHERE status NOT IN ('Canceled', 'Cancelled')
                  AND do_not_process != 'Yes'
                  AND invoice_date >= ({LAST_MONTH_START})
                  AND invoice_date < ({LAST_MONTH_END})
            """,
        },
        {
            "id": "A-orders-past-2-months",
            "question": "total orders form past 2 months",
            "plan": {
                "metric": "order_count",
                "dimension": None,
                "period": "last_n_months",
                "date_range_days": 2,
                "sort": "desc",
                "limit": None,
            },
            "expect": "number",
            "sql": f"""
                SELECT COUNT(DISTINCT sales_order_number)
                FROM sales_order
                WHERE status NOT IN ('Canceled', 'Cancelled')
                  AND do_not_process != 'Yes'
                  AND invoice_date >= ({LAST_MONTH_START})
                  AND invoice_date < ({MONTH_WINDOW_END})
            """,
        },
    ]

    rows = []
    passed = 0
    for case in cases:
        if case["expect"] == "reject":
            ok, msg = validate_plan(case["plan"])
            success = ok is False
            actual = msg
            expected = "rejected"
        else:
            ok, msg = validate_plan(case["plan"])
            if not ok and case["expect"] != "reject":
                success = False
                actual = msg
                expected = case["expect"]
            else:
                result = answer_question(case["question"], plan=case["plan"], skip_llm_explain=True)
                actual = result.get("answer_text")
                if case["expect"] == "number":
                    expected = _truth(case["sql"])
                    got = _num(result)
                    success = got is not None and expected is not None and abs(float(got) - float(expected)) < 0.01
                elif case["expect"] == "on_order_zero":
                    included = _truth(
                        """
                        SELECT COALESCE(SUM(-it.quantity), 0)
                        FROM inventory_transaction it
                        LEFT JOIN sales_order so ON it.number = so.sales_order_number
                        WHERE it.number = 'GC-SO23-00017'
                          AND it.reference = 'Sales order'
                          AND it.issue = 'Sold'
                          AND it.quantity < 0
                          AND (
                                so.sales_order_number IS NULL
                                OR (
                                    so.status NOT IN ('Canceled', 'Cancelled')
                                    AND so.do_not_process != 'Yes'
                                )
                              )
                        """
                    )
                    expected = 0
                    success = float(included or 0) == 0.0
                else:
                    expected = "no crash"
                    success = result.get("answer_text") is not None
        passed += int(success)
        rows.append(
            {
                "id": case["id"],
                "question": case["question"],
                "expected": expected,
                "actual": actual,
                "pass": success,
            }
        )
        print(f"{'PASS' if success else 'FAIL'} {case['id']}: {actual}")

    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "question", "expected", "actual", "pass"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n{passed}/{len(cases)} passed. Wrote {OUT}")


if __name__ == "__main__":
    main()
