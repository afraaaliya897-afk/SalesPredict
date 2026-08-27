# Sales Intelligence Platform

A chat-based sales intelligence platform that combines **natural language querying** with **automated forecasting** powered by local LLMs and statistical models.

---

## Core Idea

Ask questions in plain English about your sales data — the system automatically:
1. **Converts questions to structured plans** (LLM-driven)
2. **Generates safe SQL or forecasts** (deterministic Python)
3. **Renders charts and tables** (Chart.js frontend)

**New Feature**: Now includes **time series forecasting** directly in chat! Ask "forecast sales for next 30 days" and get historical + predicted data with confidence intervals.

---

## What You Can Ask

### Sales Analysis
- "top 5 items by quantity sold in the last 90 days"
- "daily sales trend this month"
- "total orders this month"
- "which customers bought the most"
- "sales by channel"

### Forecasting (NEW!)
- **"forecast sales for next 30 days"** — Prophet-based forecast with confidence bands
- **"predict next 2 weeks"** — 14-day forecast
- **"what will sales look like next month"** — Automatic historical + predicted chart

The LLM recognizes forecast/predict questions and triggers the **Prophet forecasting model** instead of SQL queries.

---

## Quick Start

### 1. Install Dependencies
```bash
pip install pandas duckdb ollama fastapi uvicorn prophet
```

### 2. Set Up Ollama
Download and run [Ollama](https://ollama.ai/), then pull a model:
```bash
ollama pull llama3.2:3b
```

### 3. Load Your Data
Place your sales Excel files in `data/`:
- `Sales orders_*.xlsx`
- `Inventory transactions originator_*.xlsx`

Run the loader (modify column mapping if needed):
```bash
python load_new_sales.py
```

### 4. Start the Server
```bash
cd backend
python main.py
```

Open http://localhost:8000 and start asking questions!

---

## Current Data

The database (`sales_inventory.duckdb`) contains **45,288 orders** and **277,028 inventory transactions**.

Two tables:

**`sales_order`**: Order header info
- `sales_order_number`, `customer_account`, `channel`, `status`, `invoice_date`, etc.

**`inventory_transaction`**: Line-item fulfillment
- `item_number`, `quantity` (negative = outbound), `issue` (`Sold` vs `On order`), `physical_date`, `reference`, etc.

**Metrics**:
- `order_count`: Total orders (from `sales_order`)
- `issue_quantity`: Units sold (from `inventory_transaction`, filtered for sales)
- `forecast_sales`: Time series forecast (Prophet model)

**Dimensions**: `item_number`, `customer_account`, `channel`, `site`, `warehouse`, `sale_date`

---

## Architecture Highlights

### Security Layers
1. **LLM outputs structured JSON plans** (not raw SQL)
2. **Python validates and compiles SQL** from allowlisted metrics/dimensions
3. **SQL validator blocks dangerous keywords** (DROP, DELETE, etc.)
4. **DuckDB uses parameterized queries** (no injection risk)

### Forecasting Pipeline
1. **Detect forecast intent** via LLM plan (`metric: "forecast_sales"`)
2. **Load historical data** (daily aggregated sales, configurable history window)
3. **Train Prophet model** (automatic seasonality detection)
4. **Generate forecast** (configurable days ahead, with 95% confidence intervals)
5. **Render multi-line chart** (historical line + forecast line + shaded confidence band)

### Two-Table Architecture
- `order_count` queries `sales_order` only (no JOIN)
- `issue_quantity` queries `inventory_transaction` JOIN `sales_order`
- `forecast_sales` triggers Prophet model (no SQL)

This ensures:
- **Correct results** (order counts don't duplicate item rows)
- **Fast queries** (no unnecessary JOINs)
- **Predictive insights** (forecast future sales)

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Frontend** | HTML, CSS, JavaScript (Chart.js for charts) |
| **Backend** | FastAPI (Python web framework) |
| **Database** | DuckDB (embedded analytical DB) |
| **LLM** | Ollama (local model: llama3.2:3b) |
| **Forecasting** | Prophet (Facebook's time series library) |
| **Charts** | Chart.js (line, bar, forecast charts with confidence intervals) |

---

## API

### `POST /api/chat`
**Request**:
```json
{
  "question": "forecast sales for next 30 days"
}
```

**Response (Forecast)**:
```json
{
  "answer_text": "Forecast generated using Prophet with 179 days of history...",
  "chart_type": "forecast",
  "chart_data": {
    "labels": ["2026-01-01", "2026-01-02", ...],
    "historical": [120, 150, ...],
    "forecast": [null, null, ..., 180, 200],
    "lower": [null, null, ..., 150, 170],
    "upper": [null, null, ..., 210, 230]
  },
  "table_data": [...],
  "plan_used": {"metric": "forecast_sales", ...},
  "debug": {...}
}
```

**Response (SQL Query)**:
```json
{
  "answer_text": "The top 5 items by quantity sold...",
  "chart_type": "bar",
  "chart_data": {"labels": [...], "values": [...]},
  "table_data": [...],
  "plan_used": {"metric": "issue_quantity", ...}
}
```

---

## Troubleshooting

### Ollama Not Running
**Error**: `Failed to connect to Ollama`  
**Fix**: Start Ollama desktop app or run `ollama serve`

### Port 8000 in Use
**Error**: `[Errno 10048] error while attempting to bind`  
**Fix**: Kill the process:
```bash
netstat -ano | findstr :8000
taskkill /PID <pid> /F
```

### Prophet Installation Issues
**Error**: `Prophet not installed`  
**Fix**: Install with all dependencies:
```bash
pip install prophet
```

On Windows, you may need C++ build tools from Visual Studio.

### Forecast Errors
**Issue**: "No historical data available"  
**Fix**: Ensure `inventory_transaction` has `issue = 'Sold'`, `quantity < 0`, and dates in the past.

**Issue**: Prophet training fails  
**Fix**: Requires at least 14 days of historical data. Check your date range.

---

## File Structure

```
SalesPrediction/
├── backend/
│   └── main.py               # FastAPI server
├── frontend/
│   ├── index.html            # Chat UI
│   ├── styles.css            # Styling
│   └── app.js                # Frontend logic + forecast chart rendering
├── data/                     # Excel files (not in git)
├── sales_inventory.duckdb    # Database (not in git)
├── query_engine.py           # Core logic: plan → SQL/forecast
├── forecast_service.py       # Prophet forecasting module
├── load_new_sales.py         # Data loader
├── README.md                 # This file
├── SYSTEM_ARCHITECTURE.md    # Detailed technical docs
└── QUICK_REFERENCE.md        # Visual architecture guide
```

---

## Next Steps

- **Add more metrics**: Revenue, profit margin, customer lifetime value
- **Advanced forecasting**: ARIMA, ensemble models, hyperparameter tuning
- **Real-time updates**: Websocket support for live queries
- **Authentication**: User login and role-based access
- **Export**: Download forecasts as CSV/Excel

---

**Questions?** Check `SYSTEM_ARCHITECTURE.md` for deep dive, or `QUICK_REFERENCE.md` for visual overview.
