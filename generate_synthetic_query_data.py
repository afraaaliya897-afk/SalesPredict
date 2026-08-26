"""
Synthetic sales_order + inventory_transaction data for the query engine.

A few hundred rows over 120 days, mixed channels/customers/items,
Cancelled / Do Not Process = Yes rows, and non-"Sales order" references
so exclusion logic can be verified.

Usage:
    python generate_synthetic_query_data.py
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from query_engine import DB_PATH, REFERENCE_SALES_ORDER_VALUE

OUT_DIR = Path(__file__).resolve().parent / "data"
SEED = 42

ITEMS = [
    "PLANT-102",
    "SOIL-045",
    "POT-210",
    "FERT-018",
    "SEED-331",
    "TOOL-007",
    "HOSE-112",
    "TRAY-088",
]
CUSTOMERS = [
    ("C-1001", "Greenfield Retail"),
    ("C-1002", "Oasis Wholesale"),
    ("C-1003", "Bloom Online"),
    ("C-1004", "Desert Gardens"),
    ("C-1005", "Palm Grove"),
    ("C-1006", "Nile Nursery"),
    ("C-1007", "Cedar Home"),
    ("C-1008", "Sand & Stone"),
]
CHANNELS = ["Retail Store", "Online", "Wholesale"]
SITES = ["DXB-01", "AUH-02"]
WAREHOUSES = ["WH-A", "WH-B"]
ORDER_TYPES = ["Standard", "Rush"]


def generate(n_orders: int = 420, span_days: int = 120, seed: int = SEED) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    rng = random.Random(seed)
    end = datetime(2026, 8, 26)
    start = end - timedelta(days=span_days - 1)

    orders = []
    txns = []
    log = {
        "orders_generated": 0,
        "inventory_rows_generated": 0,
        "cancelled_orders": 0,
        "do_not_process_yes": 0,
        "non_sales_order_reference_rows": 0,
    }

    for i in range(n_orders):
        day_offset = rng.randrange(span_days)
        order_date = start + timedelta(days=day_offset, hours=rng.randrange(8, 18))
        so_num = f"SO-{10000 + i}"
        cust_id, cust_name = CUSTOMERS[rng.randrange(len(CUSTOMERS))]
        channel = CHANNELS[rng.randrange(len(CHANNELS))]
        site = SITES[rng.randrange(len(SITES))]
        warehouse = WAREHOUSES[rng.randrange(len(WAREHOUSES))]

        status = "Open"
        do_not_process = "No"
        roll = rng.random()
        if roll < 0.08:
            status = "Cancelled"
            log["cancelled_orders"] += 1
        elif roll < 0.12:
            do_not_process = "Yes"
            log["do_not_process_yes"] += 1

        orders.append(
            {
                "sales_order_number": so_num,
                "customer_account": cust_id,
                "customer_name": cust_name,
                "order_type": ORDER_TYPES[rng.randrange(len(ORDER_TYPES))],
                "channel": channel,
                "status": status,
                "do_not_process": do_not_process,
                "site": site,
                "warehouse": warehouse,
                "invoice_date": order_date.date(),
            }
        )
        log["orders_generated"] += 1

        n_lines = rng.randrange(1, 4)
        for _ in range(n_lines):
            item = ITEMS[rng.randrange(len(ITEMS))]
            qty = rng.randrange(1, 25)
            txns.append(
                {
                    "item_number": item,
                    "reference": REFERENCE_SALES_ORDER_VALUE,
                    "number": so_num,
                    "receipt": 0,
                    "issue": qty,
                    "physical_date": order_date,
                    "financial_date": order_date,
                }
            )
            log["inventory_rows_generated"] += 1

    # Non-sales-order inventory rows (purchase receipts, transfers) — must be excluded from sales
    for j in range(40):
        day_offset = rng.randrange(span_days)
        d = start + timedelta(days=day_offset)
        ref = "Purchase order" if j % 2 == 0 else "Transfer"
        txns.append(
            {
                "item_number": ITEMS[rng.randrange(len(ITEMS))],
                "reference": ref,
                "number": f"PO-{20000 + j}",
                "receipt": rng.randrange(10, 80),
                "issue": 0,
                "physical_date": d,
                "financial_date": d,
            }
        )
        log["non_sales_order_reference_rows"] += 1
        log["inventory_rows_generated"] += 1

    # One cancelled order with huge issue qty — if exclusion is broken, this item wins every "top item" query
    poison_so = "SO-POISON"
    poison_date = end - timedelta(days=5)
    orders.append(
        {
            "sales_order_number": poison_so,
            "customer_account": "C-9999",
            "customer_name": "Should Be Excluded",
            "order_type": "Standard",
            "channel": "Wholesale",
            "status": "Cancelled",
            "do_not_process": "No",
            "site": "DXB-01",
            "warehouse": "WH-A",
            "invoice_date": poison_date.date(),
        }
    )
    log["orders_generated"] += 1
    log["cancelled_orders"] += 1
    txns.append(
        {
            "item_number": "POISON-ITEM",
            "reference": REFERENCE_SALES_ORDER_VALUE,
            "number": poison_so,
            "receipt": 0,
            "issue": 999_999,
            "physical_date": poison_date,
            "financial_date": poison_date,
        }
    )
    log["inventory_rows_generated"] += 1

    return pd.DataFrame(orders), pd.DataFrame(txns), log


def load_tables(sales_order: pd.DataFrame, inventory: pd.DataFrame, db_path: str = DB_PATH) -> dict:
    """Write the two query-engine tables into DuckDB. Logs rows in/out."""
    log = {
        "sales_order_rows_in": len(sales_order),
        "inventory_rows_in": len(inventory),
    }
    so = sales_order.copy()
    it = inventory.copy()

    required_so = [
        "sales_order_number",
        "customer_account",
        "status",
        "do_not_process",
        "channel",
        "site",
        "warehouse",
    ]
    before = len(so)
    so = so.dropna(subset=required_so)
    log["sales_order_dropped_missing_required"] = before - len(so)

    required_it = ["item_number", "reference", "number", "issue", "receipt", "physical_date", "financial_date"]
    before = len(it)
    it = it.dropna(subset=required_it)
    log["inventory_dropped_missing_required"] = before - len(it)

    it["physical_date"] = pd.to_datetime(it["physical_date"], errors="coerce")
    it["financial_date"] = pd.to_datetime(it["financial_date"], errors="coerce")
    before = len(it)
    it = it.dropna(subset=["physical_date", "financial_date"])
    log["inventory_dropped_unparseable_dates"] = before - len(it)

    con = duckdb.connect(db_path)
    con.execute("CREATE OR REPLACE TABLE sales_order AS SELECT * FROM so")
    con.execute("CREATE OR REPLACE TABLE inventory_transaction AS SELECT * FROM it")
    n_so = con.execute("SELECT COUNT(*) FROM sales_order").fetchone()[0]
    n_it = con.execute("SELECT COUNT(*) FROM inventory_transaction").fetchone()[0]
    con.close()

    log["sales_order_rows_loaded"] = n_so
    log["inventory_rows_loaded"] = n_it
    return log


def main():
    OUT_DIR.mkdir(exist_ok=True)
    so, it, gen_log = generate()
    so_path = OUT_DIR / "synthetic_sales_order.csv"
    it_path = OUT_DIR / "synthetic_inventory_transaction.csv"
    so.to_csv(so_path, index=False)
    it.to_csv(it_path, index=False)
    load_log = load_tables(so, it)

    print("=== Generate log ===")
    for k, v in gen_log.items():
        print(f"  {k}: {v}")
    print("\n=== Load log ===")
    for k, v in load_log.items():
        print(f"  {k}: {v}")
    print(f"\nWrote {so_path} and {it_path}")
    print(f"Loaded into {DB_PATH}")


if __name__ == "__main__":
    main()
