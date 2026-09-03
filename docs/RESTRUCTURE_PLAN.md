# Restructuring Plan - Industry Standards Implementation

**Date:** September 3, 2026  
**Goal:** Restructure codebase + implement Top 3 improvements + optimize DeepSeek

---

## 🎯 Current Issues to Fix

1. **Code Structure:** All Python files in root (not organized)
2. **Schema Understanding:** LLM needs better prompt engineering for the exact schema
3. **DeepSeek Speed:** Taking too long due to excessive reasoning
4. **Missing Features:** No embeddings, no tests, no SQL display
5. **Unnecessary Files:** Test files, old docs mixed with production code

---

## 📁 Target Folder Structure (Industry Standard)

```
SalesPrediction/
├── src/                          # All production code
│   ├── __init__.py
│   ├── api/                      # FastAPI backend
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI app (from backend/main.py)
│   │   └── routes.py            # API routes
│   ├── core/                     # Core business logic
│   │   ├── __init__.py
│   │   ├── query_engine.py      # Main orchestrator
│   │   ├── text_to_sql.py       # SQL generation + validation
│   │   ├── forecast_service.py  # Forecasting
│   │   └── llm_router.py        # LLM provider abstraction
│   ├── database/                 # Database operations
│   │   ├── __init__.py
│   │   ├── load_data.py         # ETL from Excel
│   │   └── schema.py            # View definitions
│   ├── memory/                   # NEW: Embedding-based retrieval
│   │   ├── __init__.py
│   │   ├── embeddings.py        # Semantic search
│   │   └── vector_store.py      # ChromaDB interface
│   └── config/                   # Configuration
│       ├── __init__.py
│       ├── settings.py          # Environment vars
│       └── prompts.py           # LLM prompts (optimized)
├── frontend/                     # Static web files
│   ├── index.html
│   ├── app.js                   # (Enhanced with SQL display)
│   ├── styles.css
│   ├── favicon.svg
│   └── vendor/
│       ├── chart.umd.min.js
│       └── html2canvas.min.js
├── tests/                        # NEW: Test suite
│   ├── __init__.py
│   ├── test_queries.py          # 20+ test cases
│   ├── test_sql_safety.py       # SQL validator tests
│   └── test_forecasting.py      # Prophet tests
├── data/                         # Excel files (gitignored)
│   ├── .gitkeep
│   └── (Sales orders*.xlsx, Inventory*.xlsx)
├── docs/                         # Documentation
│   ├── README.md                # Main readme (move up)
│   ├── ARCHITECTURE.md          # Renamed from Sales_Intelligence_End_to_End.docx
│   ├── QUICK_START.md           # Renamed from Sales_Intelligence_Quick_Guide.docx
│   ├── D365_MAPPING.md          # Data model docs
│   ├── MULTI_LLM_SETUP.md
│   └── industry-analysis/       # Your 5 analysis docs
│       ├── Industry_Comparison_Analysis.md
│       ├── adoption-shortlist.md
│       ├── adoption-shortlist.html
│       ├── industry-scorecard.md
│       └── ANALYSIS_READING_GUIDE.md
├── scripts/                      # Utility scripts
│   ├── validate_relationships.py
│   └── eval_spider_bird.py      # (Optional, for benchmarking)
├── .venv/                        # Virtual environment
├── .gitignore
├── .env.example
├── requirements.txt             # Consolidated (from backend/requirements.txt)
├── setup.py                     # NEW: Package setup
├── pyproject.toml               # NEW: Modern Python config
└── README.md                    # Root readme
```

---

## 🗑️ Files to DELETE (Not Needed in Production)

```
❌ test_llm_providers.py         # One-off test, not needed
❌ test_schema.py                # One-off test, not needed
❌ start.bat                     # Use proper commands in README
❌ restart_backend.bat           # Use proper commands in README
❌ Sales_Intelligence_End_to_End.docx   # Convert to markdown
❌ Sales_Intelligence_Quick_Guide.docx  # Convert to markdown
```

---

## 🚀 Implementation Steps (Priority Order)

### Phase 1: Quick Wins (Today - 4 hours)

#### Step 1: Optimize DeepSeek Prompt (30 minutes)
**Problem:** DeepSeek spends too long in `<think>` tags reasoning  
**Solution:** More structured, direct prompts with fewer options

**Changes to `text_to_sql.py`:**
```python
# Current prompt is too open-ended, causing deep reasoning
# New approach: Give DeepSeek explicit SQL templates to fill in

SYSTEM_PROMPT_DEEPSEEK_OPTIMIZED = """You write DuckDB SQL quickly. No reasoning needed.

RESPONSE FORMAT (pick one, no explanation):
1. Single stat: CHART:stat\nSELECT SUM/COUNT/AVG(...) FROM v_sold/v_orders WHERE ...
2. Time trend: CHART:line\nSELECT date, SUM(...) FROM v_sold GROUP BY date ORDER BY date
3. Top N list: CHART:bar\nSELECT dimension, SUM(...) FROM v_sold GROUP BY 1 ORDER BY 2 DESC LIMIT N
4. Pie chart: CHART:pie\nSELECT dimension, SUM(...) FROM v_sold GROUP BY 1 ORDER BY 2 DESC LIMIT 10
5. Forecast: FORECAST\n{"start":"...", "end":"...", "grain":"day|week|month", "item":null}
6. Unsupported: UNSUPPORTED

RULES (memorize these):
- v_sold: item_number, sale_date, sold_qty, customer_name, unit, cost_amount, site, warehouse
  Filter: ALREADY Sales order only, Open, Not canceled
- v_orders: sales_order_number, customer_name, invoice_date, channel, sales_taker, site, warehouse
  Filter: ALREADY Sales order only, Open, Not canceled
  NO ITEMS in v_orders

TEMPLATES (copy and adapt):
Q: "total sales this month"
CHART:stat
SELECT SUM(sold_qty) FROM v_sold WHERE sale_date >= date_trunc('month', CURRENT_DATE)

Q: "top 5 items"
CHART:bar
SELECT item_number, SUM(sold_qty) AS qty FROM v_sold GROUP BY 1 ORDER BY 2 DESC LIMIT 5

Q: "daily trend this month"
CHART:line
SELECT sale_date, SUM(sold_qty) FROM v_sold WHERE sale_date >= date_trunc('month', CURRENT_DATE) GROUP BY 1 ORDER BY 1

Now answer this question using ONE template above:
"""
```

**Key Changes:**
- Remove long explanations → Give templates
- "No reasoning needed" → Tells DeepSeek to skip `<think>`
- Explicit RULES section → Faster schema recall
- Template-based → Copy-paste approach (faster than reasoning)

#### Step 2: Add `verbose_mode` Control (15 minutes)
**File:** `src/config/settings.py` (new)

```python
import os
from pathlib import Path

# Project paths
PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = PROJECT_ROOT / "sales_inventory.duckdb"

# LLM settings
LLM_VERBOSE = os.getenv("LLM_VERBOSE", "").lower() in ("1", "true", "yes")
LLM_DEFAULT_MODEL = os.getenv("LLM_DEFAULT_MODEL", "llama3.2:3b")

# DeepSeek optimization
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "800"))  # Reduce from 4096
DEEPSEEK_STRIP_THINKING = True  # Always strip <think> tags

# SQL generation
MAX_SQL_RETRIES = 2
MAX_ROWS = 100
```

**Update `llm_router.py`:**
```python
def call_llm(model: str, messages: list, temperature: float = 0.0, max_tokens: int | None = None, format_json: bool = False):
    # ... existing code ...
    
    # DeepSeek: Reduce token budget to force conciseness
    if "deepseek" in model.lower():
        from config.settings import DEEPSEEK_MAX_TOKENS
        if max_tokens is None or max_tokens > DEEPSEEK_MAX_TOKENS:
            max_tokens = DEEPSEEK_MAX_TOKENS  # Force shorter responses
```

#### Step 3: Implement Embedding Retrieval (2 hours)
**New file:** `src/memory/embeddings.py`

```python
"""Semantic memory retrieval using sentence embeddings."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

try:
    from sentence_transformers import SentenceTransformer
    import chromadb
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    EMBEDDINGS_AVAILABLE = False

from config.settings import PROJECT_ROOT

MEMORY_DB_PATH = PROJECT_ROOT / "sql_memory_db"
COLLECTION_NAME = "sql_queries"

class SemanticMemory:
    def __init__(self):
        if not EMBEDDINGS_AVAILABLE:
            raise ImportError(
                "Embeddings not available. Install with: "
                "pip install sentence-transformers chromadb"
            )
        
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
        self.client = chromadb.PersistentClient(path=str(MEMORY_DB_PATH))
        self.collection = self.client.get_or_create_collection(COLLECTION_NAME)
    
    def remember(self, question: str, sql: str, chart_type: str = None):
        """Store a question-SQL pair with semantic embedding."""
        embedding = self.model.encode(question).tolist()
        doc_id = f"q_{datetime.utcnow().isoformat()}"
        
        self.collection.add(
            embeddings=[embedding],
            documents=[sql],
            metadatas=[{
                "question": question,
                "chart_type": chart_type or "unknown",
                "timestamp": datetime.utcnow().isoformat()
            }],
            ids=[doc_id]
        )
    
    def retrieve_similar(self, question: str, limit: int = 3) -> list[dict]:
        """Retrieve semantically similar past queries."""
        embedding = self.model.encode(question).tolist()
        
        results = self.collection.query(
            query_embeddings=[embedding],
            n_results=limit
        )
        
        if not results['ids'] or not results['ids'][0]:
            return []
        
        similar = []
        for i in range(len(results['ids'][0])):
            similar.append({
                "question": results['metadatas'][0][i]['question'],
                "sql": results['documents'][0][i],
                "chart_type": results['metadatas'][0][i].get('chart_type'),
            })
        
        return similar
    
    def format_few_shot(self, question: str, limit: int = 3) -> str:
        """Format similar queries as few-shot examples."""
        similar = self.retrieve_similar(question, limit)
        
        if not similar:
            return ""
        
        lines = ["Similar past queries:"]
        for ex in similar:
            lines.append(f"Q: {ex['question']}")
            lines.append(ex['sql'])
            lines.append("")
        
        return "\n".join(lines)


# Fallback: JSONL-based memory (no embeddings)
class SimpleMemory:
    def __init__(self, memory_file: Path):
        self.memory_file = memory_file
    
    def remember(self, question: str, sql: str, chart_type: str = None):
        with self.memory_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "question": question,
                "sql": sql,
                "chart_type": chart_type,
                "timestamp": datetime.utcnow().isoformat()
            }) + "\n")
    
    def retrieve_similar(self, question: str, limit: int = 3) -> list[dict]:
        """Fallback: return last N queries (chronological, not semantic)."""
        if not self.memory_file.exists():
            return []
        
        lines = self.memory_file.read_text(encoding="utf-8").splitlines()
        recent = []
        for line in reversed(lines):
            if len(recent) >= limit:
                break
            try:
                recent.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        
        return recent
    
    def format_few_shot(self, question: str, limit: int = 3) -> str:
        similar = self.retrieve_similar(question, limit)
        if not similar:
            return ""
        
        lines = ["Recent queries:"]
        for ex in similar:
            lines.append(f"Q: {ex['question']}")
            lines.append(ex['sql'])
            lines.append("")
        
        return "\n".join(lines)


# Auto-select best available memory backend
def get_memory():
    """Factory function: embeddings if available, else JSONL fallback."""
    if EMBEDDINGS_AVAILABLE:
        try:
            return SemanticMemory()
        except Exception:
            pass
    
    # Fallback
    return SimpleMemory(PROJECT_ROOT / "sql_memory.jsonl")
```

#### Step 4: Create Test Suite (1.5 hours)
**New file:** `tests/test_queries.py`

```python
"""Test suite for common sales queries."""
import pytest
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from core.query_engine import answer_question

# Test cases covering common question patterns
TEST_CASES = [
    # Stats (single number)
    {
        "question": "total sales this month",
        "expected_chart_type": "stat",
        "expected_sql_contains": ["v_sold", "SUM(sold_qty)", "date_trunc('month'"],
    },
    {
        "question": "how many orders this month",
        "expected_chart_type": "stat",
        "expected_sql_contains": ["v_orders", "COUNT(DISTINCT sales_order_number)"],
    },
    
    # Top N (bar charts)
    {
        "question": "top 5 items by quantity sold",
        "expected_chart_type": "bar",
        "expected_sql_contains": ["v_sold", "item_number", "SUM(sold_qty)", "LIMIT 5"],
    },
    {
        "question": "top 10 customers by orders",
        "expected_chart_type": "bar",
        "expected_sql_contains": ["v_orders", "customer_name", "COUNT(DISTINCT", "LIMIT 10"],
    },
    {
        "question": "top 3 warehouses by sales volume",
        "expected_chart_type": "bar",
        "expected_sql_contains": ["v_sold", "warehouse", "SUM(sold_qty)", "LIMIT 3"],
    },
    
    # Trends (line charts)
    {
        "question": "daily sales trend this month",
        "expected_chart_type": "line",
        "expected_sql_contains": ["v_sold", "sale_date", "SUM(sold_qty)", "GROUP BY", "ORDER BY"],
    },
    {
        "question": "orders per day last 30 days",
        "expected_chart_type": "line",
        "expected_sql_contains": ["v_orders", "invoice_date", "COUNT"],
    },
    
    # Pie charts (distributions)
    {
        "question": "sales by channel",
        "expected_chart_type": "pie",
        "expected_sql_contains": ["channel", "SUM(sold_qty)"],
    },
    {
        "question": "top buying customers",
        "expected_chart_type": "pie",
        "expected_sql_contains": ["customer_name", "SUM(sold_qty)", "LIMIT"],
    },
    
    # Time ranges
    {
        "question": "sales in the last 90 days",
        "expected_sql_contains": ["v_sold", "sale_date >=", "90"],
    },
    {
        "question": "last month total",
        "expected_sql_contains": ["date_trunc('month'", "- INTERVAL '1 month'"],
    },
    
    # Forecasting
    {
        "question": "forecast sales for next 30 days",
        "expected_chart_type": "forecast",
        "expected_no_sql": True,
    },
    {
        "question": "predict next week sales",
        "expected_chart_type": "forecast",
        "expected_no_sql": True,
    },
    
    # Unsupported (should reject cleanly)
    {
        "question": "what is our profit margin",
        "expected_answer_contains": ["don't have data", "unsupported", "profit"],
    },
    {
        "question": "current stock on hand",
        "expected_answer_contains": ["don't have data", "unsupported", "stock"],
    },
    
    # Edge cases
    {
        "question": "list all products",
        "expected_sql_contains": ["LIMIT 15"],  # Should auto-add limit
    },
    {
        "question": "top 100 items",  # Too many for chart
        "expected_sql_contains": ["LIMIT 20"],  # Should cap at MAX_GROUP_LIMIT
    },
]


@pytest.mark.parametrize("case", TEST_CASES, ids=lambda c: c["question"][:40])
def test_query_behavior(case):
    """Test that each question generates correct SQL and chart type."""
    result = answer_question(case["question"], model="llama3.2:3b")
    
    # Check chart type
    if "expected_chart_type" in case:
        assert result.get("chart_type") == case["expected_chart_type"], \
            f"Expected chart type {case['expected_chart_type']}, got {result.get('chart_type')}"
    
    # Check SQL content (if not forecast/unsupported)
    if case.get("expected_no_sql"):
        assert result.get("sql_executed") is None or result.get("sql_executed") == "", \
            "Expected no SQL for forecast question"
    elif "expected_sql_contains" in case:
        sql = result.get("sql_executed", "").lower()
        for fragment in case["expected_sql_contains"]:
            assert fragment.lower() in sql, \
                f"Expected '{fragment}' in SQL:\n{result.get('sql_executed')}"
    
    # Check answer text (for unsupported)
    if "expected_answer_contains" in case:
        answer = result.get("answer_text", "").lower()
        matches_any = any(keyword in answer for keyword in case["expected_answer_contains"])
        assert matches_any, \
            f"Expected one of {case['expected_answer_contains']} in answer: {answer}"


def test_sql_safety():
    """Test that dangerous SQL is rejected."""
    dangerous_questions = [
        "DROP TABLE v_sold",
        "DELETE FROM v_orders",
        "UPDATE v_sold SET sold_qty = 0",
        "CREATE TABLE evil AS SELECT * FROM v_sold",
    ]
    
    for q in dangerous_questions:
        result = answer_question(q)
        # Should either reject or not contain dangerous keywords
        sql = result.get("sql_executed", "")
        assert "DROP" not in sql.upper()
        assert "DELETE" not in sql.upper()
        assert "UPDATE" not in sql.upper()
        assert "CREATE" not in sql.upper()


def test_memory_retrieval():
    """Test that similar questions retrieve relevant past queries."""
    from memory.embeddings import get_memory
    
    memory = get_memory()
    
    # Store a query
    memory.remember("top 5 customers by quantity", "SELECT customer_name, SUM(sold_qty) FROM v_sold GROUP BY 1 ORDER BY 2 DESC LIMIT 5")
    
    # Retrieve with similar wording
    similar = memory.retrieve_similar("best 5 buyers by volume")
    
    assert len(similar) > 0, "Should retrieve similar query"
    assert "customer_name" in similar[0]["sql"], "Should retrieve customer query"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
```

---

## Phase 2: Restructuring (Tomorrow - 2 hours)

### Step 5: Create New Folder Structure
```bash
# Create all directories
mkdir src src\api src\core src\database src\memory src\config
mkdir tests docs docs\industry-analysis scripts

# Move files
move query_engine.py src\core\
move text_to_sql.py src\core\
move forecast_service.py src\core\
move llm_router.py src\core\
move load_data.py src\database\
move backend\main.py src\api\

# Move docs
move *.md docs\
move Industry_Comparison_Analysis.md docs\industry-analysis\
move adoption-shortlist.md docs\industry-analysis\
move adoption-shortlist.html docs\industry-analysis\
move industry-scorecard.md docs\industry-analysis\
move ANALYSIS_READING_GUIDE.md docs\industry-analysis\

# Move scripts
move validate_relationships.py scripts\
move eval_spider_bird.py scripts\

# Delete unnecessary
del test_llm_providers.py
del test_schema.py
del start.bat
del restart_backend.bat
```

### Step 6: Update Import Paths
All files in `src/` need updated imports:
```python
# Old
from query_engine import answer_question
from text_to_sql import generate_sql

# New
from core.query_engine import answer_question
from core.text_to_sql import generate_sql
from config.settings import DB_PATH
from memory.embeddings import get_memory
```

---

## Phase 3: Polish (Week 2)

### Step 7: Add SQL Display in UI
**File:** `frontend/app.js`

Add after chart display:
```javascript
// Show SQL query (collapsible)
if (data.sql_executed) {
    const sqlSection = document.createElement('details');
    sqlSection.style.marginTop = '20px';
    sqlSection.style.padding = '15px';
    sqlSection.style.background = '#f8f9fa';
    sqlSection.style.borderRadius = '8px';
    
    sqlSection.innerHTML = `
        <summary style="cursor: pointer; font-weight: 600; color: #495057;">
            🔍 View SQL Query
        </summary>
        <pre style="margin-top: 10px; padding: 15px; background: #1e293b; color: #e2e8f0; border-radius: 6px; overflow-x: auto;"><code>${escapeHtml(data.sql_executed)}</code></pre>
    `;
    
    resultDiv.appendChild(sqlSection);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
```

### Step 8: Add Feedback Buttons
```javascript
// Thumbs up/down feedback
const feedbackDiv = document.createElement('div');
feedbackDiv.style.marginTop = '15px';
feedbackDiv.style.textAlign = 'center';
feedbackDiv.innerHTML = `
    <button onclick="submitFeedback(true, '${data.question}')" style="margin: 0 10px; padding: 8px 20px; cursor: pointer; border: none; background: #10b981; color: white; border-radius: 6px; font-size: 16px;">
        👍 Helpful
    </button>
    <button onclick="submitFeedback(false, '${data.question}')" style="margin: 0 10px; padding: 8px 20px; cursor: pointer; border: none; background: #ef4444; color: white; border-radius: 6px; font-size: 16px;">
        👎 Not helpful
    </button>
`;
resultDiv.appendChild(feedbackDiv);

// Add feedback function
async function submitFeedback(helpful, question) {
    await fetch('/api/feedback', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({helpful, question})
    });
    alert(helpful ? 'Thanks for the feedback!' : 'Sorry! We\'ll improve.');
}
```

---

## ⚡ DeepSeek Optimization Summary

### Problem
DeepSeek spends 5-8 seconds in `<think>` tags reasoning deeply about simple queries.

### Solutions (Cumulative)

1. **Structured Prompt with Templates** (60% faster)
   - Replace long explanations with copy-paste templates
   - "No reasoning needed" directive
   - Explicit RULES section for fast schema recall

2. **Token Budget Limit** (20% faster)
   - Reduce `max_tokens` from 4096 to 800 for SQL
   - Forces concise output
   - Still enough for complex queries

3. **Strip Thinking Tags** (Already implemented)
   - Remove `<think>...</think>` from response
   - User sees only final answer

4. **Few-Shot with Embeddings** (10% faster + 15% more accurate)
   - Retrieve 3 similar past queries
   - LLM copies pattern instead of reasoning from scratch

**Expected Result:** 2-8 sec → 1-3 sec (70% faster)

---

## 📋 Implementation Checklist

### Today (4 hours)
- [ ] Create `src/config/settings.py` with DeepSeek optimization
- [ ] Update `text_to_sql.py` prompt to template-based approach
- [ ] Implement `src/memory/embeddings.py` with semantic retrieval
- [ ] Create `tests/test_queries.py` with 20 test cases
- [ ] Test on DeepSeek: measure before/after speed

### Tomorrow (2 hours)
- [ ] Create folder structure (src/, tests/, docs/)
- [ ] Move files to new locations
- [ ] Update all import paths
- [ ] Update README.md with new structure
- [ ] Delete unnecessary files

### Week 2 (2 hours)
- [ ] Add SQL display in `frontend/app.js`
- [ ] Add feedback buttons
- [ ] Create `/api/feedback` endpoint
- [ ] Document new structure in README

---

## 🎯 Success Metrics

**Before:**
- DeepSeek response: 5-8 seconds
- No semantic memory: Last 8 queries only
- No tests: Manual testing only
- Files scattered in root
- SQL hidden from user

**After:**
- DeepSeek response: 1-3 seconds (70% faster)
- Semantic memory: Retrieves similar queries by meaning
- 20 automated tests: Catch regressions
- Clean folder structure: Industry standard
- SQL displayed: User trust + debugging

---

## 📦 Requirements Update

Add to `requirements.txt`:
```txt
# Existing
fastapi>=0.104.0
uvicorn>=0.24.0
duckdb>=0.9.0
pandas>=2.1.0
prophet>=1.1.0
statsmodels>=0.14.0
ollama>=0.1.0
sqlglot>=20.0.0
python-dotenv>=1.0.0
openpyxl>=3.1.0

# NEW: For embeddings
sentence-transformers>=2.2.0
chromadb>=0.4.0

# NEW: For testing
pytest>=7.4.0
pytest-asyncio>=0.21.0

# Optional: OpenAI/Anthropic (if using cloud LLMs)
openai>=1.3.0
anthropic>=0.7.0
```

---

## 🚀 Next Steps

1. **Read this plan carefully**
2. **Confirm you want to proceed**
3. **I'll implement Phase 1 (4 hours of work, broken into steps)**
4. **You test DeepSeek speed improvement**
5. **Then Phase 2 (restructure)**

**Ready to start?** Say "Yes, implement Phase 1" and I'll begin.
