"""Load D365 Excel extracts into DuckDB with production cleaning."""
import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent  # Go up to project root
sys.path.insert(0, str(ROOT))  # Add project root to Python path

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
            "Unit": "unit",
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
        "unit",
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
    
    # Print basic counts
    print("="*80)
    print("DATA LOAD SUMMARY")
    print("="*80)
    print("sales_order", con.execute("SELECT COUNT(*) FROM sales_order").fetchone()[0])
    print(
        "inventory_transaction",
        con.execute("SELECT COUNT(*) FROM inventory_transaction").fetchone()[0],
    )
    
    # Relationship validation reporting
    print("\n" + "="*80)
    print("RELATIONSHIP VALIDATION")
    print("="*80)
    
    # Check for sales order inventory records without matching header
    orphaned_sales = con.execute("""
        SELECT COUNT(*) 
        FROM inventory_transaction 
        WHERE reference = 'Sales order' 
          AND (number IS NULL OR number NOT IN (SELECT sales_order_number FROM sales_order))
    """).fetchone()[0]
    
    total_sales_inv = con.execute("""
        SELECT COUNT(*) 
        FROM inventory_transaction 
        WHERE reference = 'Sales order'
    """).fetchone()[0]
    
    if total_sales_inv > 0:
        orphan_pct = (orphaned_sales / total_sales_inv * 100)
        print(f"Sales order inventory records: {total_sales_inv:,}")
        print(f"  - Without matching sales_order header: {orphaned_sales:,} ({orphan_pct:.2f}%)")
        if orphaned_sales > 100:
            print(f"  WARNING: {orphaned_sales} sales transactions lack order header data!")
    
    # Check total orphaned records (all reference types)
    total_orphaned = con.execute("""
        SELECT COUNT(*) 
        FROM inventory_transaction 
        WHERE number IS NULL OR number NOT IN (SELECT sales_order_number FROM sales_order)
    """).fetchone()[0]
    
    total_inv = con.execute("SELECT COUNT(*) FROM inventory_transaction").fetchone()[0]
    orphan_pct_all = (total_orphaned / total_inv * 100) if total_inv > 0 else 0
    
    print(f"\nTotal orphaned inventory records (all types): {total_orphaned:,} ({orphan_pct_all:.1f}%)")
    print("  (This includes non-sales transactions: Transfers, Purchases, BOM, etc.)")
    
    # Create views
    print("\n" + "="*80)
    print("CREATING BUSINESS VIEWS")
    print("="*80)
    
    from src.core.text_to_sql import VIEW_DDL

    for ddl in VIEW_DDL:
        con.execute(ddl)
    print("v_orders", con.execute("SELECT COUNT(*) FROM v_orders").fetchone()[0])
    print("v_sold", con.execute("SELECT COUNT(*) FROM v_sold").fetchone()[0])
    
    # Check data completeness in v_sold
    null_customers = con.execute("""
        SELECT COUNT(*) 
        FROM v_sold 
        WHERE customer_account IS NULL
    """).fetchone()[0]
    
    if null_customers > 0:
        v_sold_total = con.execute("SELECT COUNT(*) FROM v_sold").fetchone()[0]
        null_pct = (null_customers / v_sold_total * 100) if v_sold_total > 0 else 0
        print(f"  - Records with NULL customer: {null_customers:,} ({null_pct:.2f}%)")
    
    print("\n" + "="*80)
    print("DATABASE BUILD COMPLETE")
    print("="*80)
    print("\nNOTE: Missing D365 relationship:")
    print("  Current: InventTrans -> SalesTable (direct join)")
    print("  D365 Model: InventTrans -> SalesLine -> SalesTable")
    print("  Impact: Cannot link to line-item details (price, line amounts)")
    print("  See D365_DATA_MODEL.md for details")
    
    con.close()


if __name__ == "__main__":
    build_database()
