"""Load D365 Excel extracts into DuckDB with production cleaning."""
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB_PATH = ROOT / "sales_inventory.duckdb"


def _excel(pattern: str) -> Path:
    matches = sorted(DATA.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"No file matching {pattern} in {DATA}")
    return matches[-1]


def load_sales_orders(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path)
    df = df.rename(
        columns={
            "Sales order": "sales_order_number",
            "Customer account": "customer_account",
            "Customer name": "customer_name",
            "Order type": "order_type",
            "Invoice account": "invoice_account",
            "Channel": "channel",
            "Status": "status",
            "Release status": "release_status",
            "Delivery status": "delivery_status",
            "Do not process": "do_not_process",
            "Sales taker": "sales_taker",
            "Site": "site",
            "Warehouse": "warehouse",
            "Invoice Date": "invoice_date",
        }
    )
    df = df.drop_duplicates()
    df["invoice_date"] = pd.to_datetime(df["invoice_date"], errors="coerce")
    return df


def load_inventory(path: Path) -> pd.DataFrame:
    df = pd.read_excel(
        path,
        dtype={"Issue": "string", "Receipt": "string", "Reference": "string"},
    )
    df = df.rename(
        columns={
            "Product number": "product_number",
            "Item number": "item_number",
            "Physical date": "physical_date",
            "Financial date": "financial_date",
            "Reference": "reference",
            "Number": "number",
            "Receipt": "receipt",
            "Issue": "issue",
            "Quantity": "quantity",
            "Cost amount": "cost_amount",
            "Site": "site",
            "Warehouse": "warehouse",
        }
    )
    keep = [
        "number",
        "item_number",
        "product_number",
        "physical_date",
        "financial_date",
        "reference",
        "receipt",
        "issue",
        "quantity",
        "cost_amount",
        "site",
        "warehouse",
    ]
    df = df[keep]
    df["physical_date"] = pd.to_datetime(df["physical_date"], errors="coerce")
    df["financial_date"] = pd.to_datetime(df["financial_date"], errors="coerce")
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    df["cost_amount"] = pd.to_numeric(df["cost_amount"], errors="coerce")
    before = len(df)
    df = df.drop_duplicates()
    print(f"inventory exact-duplicate rows dropped: {before - len(df)}")
    return df


def build_database() -> None:
    so = load_sales_orders(_excel("Sales orders*.xlsx"))
    inv = load_inventory(_excel("Inventory transactions*.xlsx"))
    con = duckdb.connect(str(DB_PATH))
    con.register("so_df", so)
    con.register("inv_df", inv)
    con.execute("CREATE OR REPLACE TABLE sales_order AS SELECT * FROM so_df")
    con.execute(
        """
        CREATE OR REPLACE TABLE inventory_transaction AS
        SELECT
            i.*,
            so.customer_account
        FROM inv_df i
        LEFT JOIN sales_order so ON i.number = so.sales_order_number
        """
    )
    print("sales_order", con.execute("SELECT COUNT(*) FROM sales_order").fetchone()[0])
    print(
        "inventory_transaction",
        con.execute("SELECT COUNT(*) FROM inventory_transaction").fetchone()[0],
    )
    con.close()


if __name__ == "__main__":
    build_database()
