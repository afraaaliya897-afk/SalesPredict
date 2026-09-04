"""
Export cleaned data views to Excel files.

This exports the v_sold and v_orders views (the data the LLM actually queries)
to Excel files so you can see exactly what the system is working with.

Usage:
    python scripts/export_clean_data.py
    
Output:
    - data/clean_v_sold.xlsx
    - data/clean_v_orders.xlsx
"""

import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import duckdb
import pandas as pd

DB_PATH = ROOT / "sales_inventory.duckdb"
OUTPUT_DIR = ROOT / "data"


def export_views_to_excel():
    """Export v_sold and v_orders to Excel files."""
    
    print("🔌 Connecting to database...")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    
    try:
        # Export v_sold
        print("\n📊 Exporting v_sold view...")
        df_sold = con.execute("SELECT * FROM v_sold ORDER BY sale_date DESC").df()
        sold_path = OUTPUT_DIR / "clean_v_sold.xlsx"
        df_sold.to_excel(sold_path, index=False, engine='openpyxl')
        print(f"✅ Exported {len(df_sold):,} rows to: {sold_path}")
        print(f"   Columns: {', '.join(df_sold.columns)}")
        print(f"   Date range: {df_sold['sale_date'].min()} to {df_sold['sale_date'].max()}")
        
        # Export v_orders
        print("\n📊 Exporting v_orders view...")
        df_orders = con.execute("SELECT * FROM v_orders ORDER BY invoice_date DESC").df()
        orders_path = OUTPUT_DIR / "clean_v_orders.xlsx"
        df_orders.to_excel(orders_path, index=False, engine='openpyxl')
        print(f"✅ Exported {len(df_orders):,} rows to: {orders_path}")
        print(f"   Columns: {', '.join(df_orders.columns)}")
        print(f"   Date range: {df_orders['invoice_date'].min()} to {df_orders['invoice_date'].max()}")
        
        # Summary statistics
        print("\n📈 Summary:")
        print(f"   Total sold transactions: {len(df_sold):,}")
        print(f"   Total orders: {len(df_orders):,}")
        print(f"   Unique items: {df_sold['item_number'].nunique():,}")
        print(f"   Unique customers: {df_sold['customer_name'].nunique():,}")
        print(f"   Total quantity sold: {df_sold['sold_qty'].sum():,.0f} units")
        print(f"   Total cost amount: ${df_sold['cost_amount'].sum():,.2f}")
        
        print("\n✅ Export complete! Files saved in data/ folder.")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        raise
    finally:
        con.close()


if __name__ == "__main__":
    print("=" * 60)
    print("EXPORT CLEAN DATA TO EXCEL")
    print("=" * 60)
    
    if not DB_PATH.exists():
        print(f"\n❌ Database not found: {DB_PATH}")
        print("   Run 'python src/database/load_data.py' first to create it.")
        sys.exit(1)
    
    OUTPUT_DIR.mkdir(exist_ok=True)
    export_views_to_excel()
