# Quick Start Guide

## The Platform is Live!

**Backend API:** http://localhost:8000  
**Frontend UI:** http://localhost:3000

Both servers are currently running in your terminals.

## Using the Platform

1. **Open your browser** to http://localhost:3000
2. **Ask questions** in the chat interface:
   - "What item sold the most?"
   - "Which product had the highest sales in December 2010?"
3. **Forecasting section** is a placeholder for now (coming soon)

## Stopping the Servers

- Find the terminal windows running the backend and frontend
- Press `Ctrl+C` in each terminal
- Or close the terminal windows

## Restarting Later

**Option 1: Use the startup script**
```bash
# Double-click this file:
start.bat   (or)   start.ps1
```

**Option 2: Manual start**
```bash
# Terminal 1 - Backend
python backend/main.py

# Terminal 2 - Frontend  
python -m http.server 3000 --directory frontend
```

## Testing the API Directly

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "what item sold the most"}'
```

## Current Features

- ✓ Natural language chat interface
- ✓ Sales data queries (top selling items)
- ✓ Date range filtering
- ✓ Clean, professional UI
- ⏳ Forecasting (coming soon)

## Technical Details

- **Frontend:** Vanilla JS, clean CSS (no frameworks)
- **Backend:** FastAPI + Ollama (llama3.2:3b)
- **Database:** DuckDB (530K sales transactions)
- **Data:** UCI Online Retail dataset (2010-2011)
