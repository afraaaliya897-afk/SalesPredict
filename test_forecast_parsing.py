# Test: What dates is the LLM parsing?

import sys
sys.path.insert(0, '.')

from src.core.text_to_sql import generate_sql, extract_forecast_window

# Simulate your exact query
question = "forecast sales from January 2026 to January 2027"
model = "deepseek-r1:8b"
db_path = "sales_inventory.duckdb"

result = generate_sql(question, model, db_path)

print("=== LLM OUTPUT ===")
print(result.get("raw", "")[:500])
print("\n=== EXTRACTED WINDOW (with auto-correction) ===")

# Extract forecast window with auto-correction
window = extract_forecast_window(result.get("raw", ""), question)
if window:
    print(f"Start: {window.get('start')}")
    print(f"End: {window.get('end')}")
    print(f"Grain: {window.get('grain')}")
    print(f"Item: {window.get('item')}")
else:
    print("NO FORECAST WINDOW PARSED!")
