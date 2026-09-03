# Adoption Shortlist: What to Borrow from Industry Leaders

**Created:** September 3, 2026  
**Purpose:** Prioritized list of patterns from production systems worth adopting

---

## 🎯 Top 3 High-Impact Additions (Do These First)

### 1. Embedding-Based Memory Retrieval (from Vanna AI)
**Current State:** Last 8 queries from JSONL (chronological)  
**Industry Pattern:** Semantic search of past Q&A pairs using embeddings  
**Impact:** +10-15% accuracy on diverse questions  

**Code Changes:**
```python
# Install
pip install sentence-transformers chromadb

# Replace text_to_sql._memory_shots()
from sentence_transformers import SentenceTransformer
import chromadb

model = SentenceTransformer('all-MiniLM-L6-v2')
chroma = chromadb.PersistentClient(path="./sql_memory_db")
collection = chroma.get_or_create_collection("sql_memory")

def remember_sql(question: str, sql: str):
    """Store with semantic embedding instead of append-only JSONL"""
    embedding = model.encode(question).tolist()
    collection.add(
        embeddings=[embedding],
        documents=[sql],
        metadatas=[{"question": question}],
        ids=[f"q_{datetime.utcnow().isoformat()}"]
    )

def _memory_shots(question: str, limit: int = 3) -> str:
    """Retrieve semantically similar past queries"""
    embedding = model.encode(question).tolist()
    results = collection.query(
        query_embeddings=[embedding],
        n_results=limit
    )
    # Format as few-shot examples for LLM
    shots = []
    for i in range(len(results['ids'][0])):
        q = results['metadatas'][0][i]['question']
        sql = results['documents'][0][i]
        shots.append(f"Q: {q}\n{sql}")
    return "\n\n".join(shots)
```

**Why It Matters:**
- User asks "best customers" → retrieves past "top buyers" queries (semantic match)
- Current approach only works if recent questions are similar
- Vanna AI (23.8k★) built entire product on this pattern

**Effort:** 2-3 hours (straightforward library integration)

---

### 2. Show Generated SQL in UI (from LinkedIn/Uber)
**Current State:** User sees chart, SQL is hidden  
**Industry Pattern:** Display SQL with optional toggle  
**Impact:** Builds trust, easier debugging, user education  

**Code Changes:**
```python
# backend/main.py - Add sql_executed to response
class ChatResponse(BaseModel):
    answer: str
    answer_text: str | None = None
    sql_executed: str | None = None  # Add this
    chart_type: str | None = None
    chart_data: dict | None = None
    table_data: list | None = None

@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    result = answer_question(request.question.strip(), ...)
    return ChatResponse(
        answer=text,
        sql_executed=result.get("sql_executed"),  # Pass through
        ...
    )
```

```javascript
// frontend/app.js - Add SQL display section
function displayAnswer(data) {
    // ... existing chart/table code ...
    
    // Add SQL display (collapsible)
    if (data.sql_executed) {
        const sqlSection = document.createElement('details');
        sqlSection.innerHTML = `
            <summary>🔍 View SQL Query</summary>
            <pre><code class="language-sql">${escapeHtml(data.sql_executed)}</code></pre>
        `;
        resultDiv.appendChild(sqlSection);
    }
}
```

**Why It Matters:**
- Uber QueryGPT shows query plan before execution
- Users understand what data they're seeing
- Easier to spot misinterpretations ("Oh, it used orders not sales volume")

**Effort:** 1 hour (pass-through + UI toggle)

---

### 3. Test Suite for Common Queries (Industry Standard)
**Current State:** No automated tests  
**Industry Pattern:** 20-50 test queries with expected results  
**Impact:** Prevent regressions, document expected behavior  

**Code Changes:**
```python
# test_queries.py (new file)
import pytest
from query_engine import answer_question

TEST_CASES = [
    {
        "question": "top 5 items by quantity sold in the last 90 days",
        "expected_sql_contains": ["v_sold", "SUM(sold_qty)", "ORDER BY", "LIMIT 5"],
        "expected_chart_type": "bar",
    },
    {
        "question": "total sales this month",
        "expected_sql_contains": ["v_sold", "SUM(sold_qty)", "date_trunc('month'"],
        "expected_chart_type": "stat",
    },
    {
        "question": "forecast sales for next 30 days",
        "expected_chart_type": "forecast",
        "expected_no_sql": True,  # Should trigger Prophet, not SQL
    },
    # Add 17 more...
]

@pytest.mark.parametrize("case", TEST_CASES)
def test_query_behavior(case):
    result = answer_question(case["question"], model="llama3.2:3b")
    
    if case.get("expected_no_sql"):
        assert result.get("sql_executed") is None
    else:
        sql = result.get("sql_executed", "")
        for fragment in case["expected_sql_contains"]:
            assert fragment in sql, f"Expected '{fragment}' in SQL"
    
    assert result["chart_type"] == case["expected_chart_type"]

# Run with: pytest test_queries.py -v
```

**Why It Matters:**
- DB-GPT (18.9k★), Wren AI (16.5k★) have 1000+ tests
- Catches breaking changes: "Oh, the ORDER BY got removed by accident"
- Documents expected behavior for new developers

**Effort:** 3-4 hours (20 test cases + pytest setup)

---

## 📊 Medium Priority (Do After Top 3)

### 4. Human Feedback Loop (👍/👎 Buttons)
**Pattern:** Uber's post-execution feedback, LinkedIn's quarterly review  
**Code:**
```python
# feedback.jsonl logging
{
  "question": "top 5 customers",
  "sql": "SELECT ...",
  "thumbs_up": true,
  "timestamp": "2026-09-03T15:30:00Z",
  "user_comment": "Perfect!"
}
```

**UI Change:**
```html
<div class="feedback-buttons">
  <button onclick="submitFeedback(true)">👍 Helpful</button>
  <button onclick="submitFeedback(false)">👎 Not helpful</button>
</div>
```

**Future Use:**
- Fine-tune prompts on thumbs-up examples
- Identify common failure patterns (thumbs-down)
- Track accuracy over time (% thumbs-up)

**Effort:** 2 hours

---

### 5. Row-Level Security by User Role
**Pattern:** Enterprise SaaS requirement (Snowflake, Databricks)  
**Code:**
```python
# Add user context to query_engine
def answer_question(question: str, model: str, user_id: str):
    user = get_user(user_id)
    
    # Modify VIEW_DDL dynamically
    if not user.is_admin:
        # Restrict to user's sales_taker
        view_ddl_filtered = f"""
        CREATE OR REPLACE TEMP VIEW v_sold AS
        SELECT * FROM v_sold
        WHERE sales_taker = '{user.sales_taker}'
        """
        con.execute(view_ddl_filtered)
```

**Why It Matters:**
- Sales rep A can't see rep B's customer data
- Compliance requirement (GDPR, SOC 2)
- Currently, all users see all data

**Effort:** 4 hours (requires user auth system)

---

### 6. Prophet Hyperparameter Tuning
**Current:** Default Prophet parameters  
**Industry:** Grid search on `changepoint_prior_scale`, `seasonality_prior_scale`  
**Code:**
```python
# forecast_service.py
from sklearn.model_selection import ParameterGrid

def tune_prophet(daily: pd.DataFrame, days_ahead: int):
    param_grid = {
        'changepoint_prior_scale': [0.01, 0.05, 0.1, 0.5],
        'seasonality_prior_scale': [0.1, 1.0, 10.0],
    }
    
    best_wape = float('inf')
    best_params = None
    
    for params in ParameterGrid(param_grid):
        # Backtest with these params
        wape = backtest_prophet(daily, params)
        if wape < best_wape:
            best_wape = wape
            best_params = params
    
    # Train final model with best params
    return Prophet(**best_params).fit(daily)
```

**Impact:** 5-10% WAPE improvement (from academic papers)  
**Effort:** 3 hours

---

## ⚠️ Low Priority (Don't Do Yet)

### 7. Multi-Agent Architecture (LinkedIn SQL Bot)
**Why Skip:** Over-engineering for current scale  
- Your single-agent + governed views gets you to ~85% accuracy (estimated)
- Multi-agent adds complexity (5 LLM calls instead of 1)
- LinkedIn needed it for 100+ tables — you have 2 views

**When to Revisit:** If accuracy drops below 70% on common questions

---

### 8. Cloud LLM Migration (GPT-4o)
**Why Skip:** Local-first is a strategic advantage  
- DeepSeek R1 14B: Free, private, 2-8 sec latency
- GPT-4o: $0.03/query, 3x faster, but data leaves network

**When to Revisit:**
- User complaints about speed (hasn't happened yet)
- Need for hosted service (external users can't run Ollama)

---

### 9. Fine-Tuned SQL Model (defog-ai/sqlcoder)
**Why Skip:** Requires training data you don't have  
- sqlcoder (4.0k★): Fine-tuned on 20,000+ (question, SQL) pairs
- Your `sql_memory.jsonl`: Maybe 100 queries (not enough)

**When to Revisit:** After 1 year of production usage (10,000+ logged queries)

---

## 📋 Implementation Checklist

**Week 1: Quick Wins**
- [ ] Install sentence-transformers + chromadb
- [ ] Replace `_memory_shots` with embedding search
- [ ] Add `sql_executed` to API response
- [ ] Add collapsible SQL display in UI
- [ ] Write 20 test cases in `test_queries.py`
- [ ] Run tests, fix any failures

**Week 2: Feedback & Security**
- [ ] Add 👍/👎 buttons to UI
- [ ] Log feedback to `feedback.jsonl`
- [ ] Review feedback weekly (manual process for now)
- [ ] Plan row-level security (if multi-user deployment)

**Month 2: Forecasting Improvements**
- [ ] Add Prophet hyperparameter tuning
- [ ] Benchmark WAPE improvement (before/after)
- [ ] Document tuned params in code comments

**Month 3+: Advanced**
- [ ] Consider cloud LLM for speed (if needed)
- [ ] Explore external regressors (holidays, promotions)
- [ ] Revisit multi-agent if accuracy plateaus

---

## 🚫 Don't Copy Blindly

**Academic Research Code (Avoid):**
- MAC-SQL (339★), RESDSQL (280★), CHESS (279★): Single-paper research repos
- No ongoing maintenance, not production-tested
- Useful for citations, not for adoption

**Over-Engineered Patterns (Avoid):**
- PICARD's token-level SQL constraints: You don't need this complexity
- Large-schema retrieval (CHESS): You have 2 views, not 500 tables
- Benchmark leaderboard chasing: Spider/BIRD scores don't predict production accuracy

---

## 📚 Reference: Production Systems Studied

| System | Key Pattern Learned | Adopted? |
|--------|---------------------|----------|
| **Vanna AI** (23.8k★) | RAG-based few-shot retrieval | ✅ Top priority |
| **Wren AI** (16.5k★) | Semantic-layer-first (you already do this) | ✅ Already aligned |
| **Uber QueryGPT** | Human confirmation step | ⚠️ Medium priority |
| **Pinterest** | 20-40% accuracy without governance | ❌ Don't copy (you're better) |
| **LinkedIn SQL Bot** | Multi-agent refinement | ❌ Over-engineering |
| **Snowflake Cortex** | Native semantic views | ✅ Already aligned |
| **dbt Semantic Layer** | Metrics as code (YAML) | ⚠️ Future consideration |

---

## 🎓 Key Insight from Industry Review

**The 80/20 Rule:**
- **80% of accuracy** comes from: Governed views + SQL validation + few-shot examples
- **Next 10%** comes from: Embedding retrieval + human feedback
- **Last 10%** comes from: Multi-agent, fine-tuning, external data (diminishing returns)

**You've already nailed the 80%.**  
The top 3 recommendations get you the next 10% with minimal effort.

---

**Document Owner:** Development Team  
**Last Updated:** September 3, 2026  
**Next Review:** After implementing Top 3 recommendations
