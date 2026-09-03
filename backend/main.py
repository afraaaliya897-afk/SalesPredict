"""
Sales Intelligence API
FastAPI backend — chat uses the generalized query engine.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import sys
from pathlib import Path

# Add parent directory to path for src imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.core.query_engine import answer_question, list_chat_models, resolve_model

app = FastAPI(title="Sales Intelligence API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8001",
        "http://127.0.0.1:8001",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

frontend_path = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")


class ChatRequest(BaseModel):
    question: str
    model: str | None = None


class ChatResponse(BaseModel):
    answer: str
    answer_text: str | None = None
    error: str | None = None
    debug: dict | None = None
    chart_type: str | None = None
    chart_data: dict | None = None
    table_data: list | None = None
    plan_used: dict | None = None
    metric_label: str | None = None
    dimension_label: str | None = None


@app.get("/")
def root():
    return FileResponse(str(frontend_path / "index.html"))


@app.get("/favicon.ico")
@app.get("/favicon.svg")
def favicon():
    return FileResponse(str(frontend_path / "favicon.svg"), media_type="image/svg+xml")


@app.get("/api")
def api_info():
    return {
        "service": "Sales Intelligence API",
        "status": "running",
        "endpoints": {"chat": "/api/chat", "health": "/health"},
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/models")
def models():
    return {"models": list_chat_models()}


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    try:
        import time

        start = time.time()
        result = answer_question(request.question.strip(), model=resolve_model(request.model))
        debug = result.get("debug") or {}
        debug["execution_time_ms"] = round((time.time() - start) * 1000, 2)
        debug["chart_type"] = result.get("chart_type")
        debug["plan_used"] = result.get("plan_used")

        text = result.get("answer_text") or ""
        return ChatResponse(
            answer=text,
            answer_text=text,
            debug=debug,
            chart_type=result.get("chart_type"),
            chart_data=result.get("chart_data"),
            table_data=result.get("table_data"),
            plan_used=result.get("plan_used"),
            metric_label=result.get("metric_label"),
            dimension_label=result.get("dimension_label"),
        )
    except Exception as e:
        return ChatResponse(
            answer="",
            error=f"Failed to process question: {str(e)}",
        )


if __name__ == "__main__":
    import uvicorn

    print("\n" + "=" * 60)
    print("  Sales Intelligence Platform Starting...")
    print("=" * 60)
    print("\n  Frontend: http://localhost:8001")
    print("  API:      http://localhost:8001/api")
    print("\n  Press Ctrl+C to stop")
    print("=" * 60 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8001)
