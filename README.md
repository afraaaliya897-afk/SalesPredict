# Sales Intelligence Platform

Professional sales analytics platform with chat interface and forecasting capabilities.

## Project Structure

```
SalesPrediction/
├── backend/           # FastAPI backend
├── frontend/          # Web UI
├── data/              # Sales data (CSV)
├── load_sales.py      # Data loader
├── chat_pipeline.py   # Chat logic
└── sales_inventory.duckdb  # Database
```

## Setup

### 1. Install Python dependencies
```bash
pip install fastapi uvicorn duckdb pandas ollama
```

### 2. Install and setup Ollama
- Download from https://ollama.com/download
- Pull a model: `ollama pull llama3.2:3b`

### 3. Load data (if not already done)
```bash
python load_sales.py data/online_retail.csv
```

## Running the Platform

**Single command:**

```bash
python backend/main.py
```

Then open: **http://localhost:8000**

**Or just double-click:** `start.bat` (Windows)

That's it! Everything runs on port 8000 now.

## Features

### Chat (Active)
- Natural language queries about sales data
- Currently supports: "What item sold the most?" queries
- Date range filtering: "in December 2010"
- Powered by local Ollama LLM

### Forecasting (Coming Soon)
- Item-level forecasting for high-volume products
- Category-level forecasting for long-tail items
- Intermittent demand handling (Croston's/TSB)

## API Endpoints

- `GET /` - API info
- `GET /health` - Health check
- `POST /api/chat` - Process chat question

Example:
```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "what item sold the most"}'
```
