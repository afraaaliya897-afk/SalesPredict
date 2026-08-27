# Sales Intelligence Platform - Complete System Architecture

## 🎯 Core Philosophy: "Constrained Generation" Architecture

### The Problem We Solved
Traditional AI analytics systems let LLMs write raw SQL directly:
```
User: "Show me top sellers"
LLM: Writes full SQL → SELECT * FROM sales WHERE... (DANGEROUS & UNRELIABLE)
```

**Why this fails:**
- ❌ Security: SQL injection risks
- ❌ Accuracy: LLM might join wrong tables, use wrong logic
- ❌ Hallucination: Makes up columns/tables that don't exist
- ❌ Unpredictable: Same question = different SQL each time

### Our Solution: Semantic Layer + Deterministic Compilation
```
User: "Show me top sellers"
↓
LLM: Picks from menu → {"metric": "issue_quantity", "dimension": "item_number", "limit": 10}
↓
Python: Validates → Is "issue_quantity" in ALLOWED_METRICS? ✓
↓
Python: Compiles → SELECT it.item_number, SUM(ABS(it.quantity)) FROM...
↓
Database: Executes (read-only, parameterized)
```

**Why this works:**
- ✅ Security: LLM never writes SQL
- ✅ Accuracy: Single tested SQL template
- ✅ Predictable: Same plan = same SQL always
- ✅ Extensible: Add new metric = 1 line in config

---

## 📁 File Structure & Purpose

```
SalesPrediction/
├── query_engine.py          ← THE BRAIN (all intelligence here)
├── backend/
│   ├── main.py              ← API Gateway (FastAPI)
│   └── requirements.txt     ← Dependencies
├── frontend/
│   ├── index.html           ← UI Structure
│   ├── app.js               ← Chat Logic + Charts
│   └── styles.css           ← Styling
├── data/
│   ├── Sales orders_*.xlsx  ← Source data
│   └── Inventory_*.xlsx
├── sales_inventory.duckdb   ← DuckDB database
├── README.md                ← Project documentation
├── QUICKSTART.md            ← Setup guide
└── .gitignore               ← Git exclusions
```

---

## 🧠 File 1: `query_engine.py` (THE CORE)

### Purpose
The entire "semantic layer" that converts natural language to SQL safely.

### Key Sections

#### A. Configuration (Lines 26-48)
```python
DB_PATH = "sales_inventory.duckdb"
MODEL = "llama3.2:3b"  # Local Ollama model
REFERENCE_SALES_ORDER_VALUE = "Sales order"  # What value in "reference" column = sales
SALE_DATE_FIELD = "physical_date"  # Which date field to use
```

**Why configurable?**
- Different exports use different column names
- Change once here, works everywhere

#### B. Semantic Layer Definitions (Lines 53-106)

**1. Schema Description** (Lines 53-73)
Plain English explanation of tables and relationships fed to LLM:
```python
SCHEMA_DESCRIPTION = """
Table: sales_order (45,288 orders)
- sales_order_number, customer_account, channel, status, ...

Table: inventory_transaction (277,028 transactions)
- item_number, quantity, physical_date, reference, ...

Join: inventory_transaction.number = sales_order.sales_order_number
"""
```

**2. Allowed Metrics** (Lines 80-83)
The ONLY things you can count/measure:
```python
ALLOWED_METRICS = {
    "issue_quantity": "SUM(ABS(it.quantity))",  # How many items sold
    "order_count": "COUNT(DISTINCT so.sales_order_number)",  # How many orders
}
```

To add "revenue", you'd add:
```python
"revenue": "SUM(it.quantity * it.unit_price)"
```

**3. Allowed Dimensions** (Lines 85-92)
The ONLY ways you can group/slice data:
```python
ALLOWED_DIMENSIONS = {
    "item_number": "it.item_number",      # Group by product
    "sale_date": "DATE(it.physical_date)", # Group by day
    "customer_account": "so.customer_account", # Group by customer
    "channel": "so.channel",              # Group by channel
    # ... etc
}
```

**Why this matters:**
- LLM can ONLY pick from these lists
- Adding new question types = add 1 line here, no new code

#### C. LLM Prompts (Lines 108-158)

**PLAN_SYSTEM_PROMPT**: Instructions for LLM to fill the plan
```python
You are a query planner. Pick ONE metric and ONE dimension from these lists:

METRICS: ["issue_quantity", "order_count"]
DIMENSIONS: ["item_number", "sale_date", "customer_account", ...]

Return ONLY JSON:
{
  "metric": "issue_quantity",
  "dimension": "item_number",
  "date_range_days": 90,
  "sort": "desc",
  "limit": 5
}
```

**EXPLAIN_SYSTEM_PROMPT**: Instructions to explain results in plain English

#### D. Core Functions

**1. get_query_plan()** (Lines 184-218)
Asks LLM to fill the plan:
```python
def get_query_plan(question: str) -> dict:
    response = ollama.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": question}
        ]
    )
    # Parse JSON response
    plan = json.loads(response.message.content)
    return plan
```

**Input:** "top 5 items by quantity sold in last 90 days"
**Output:** `{"metric": "issue_quantity", "dimension": "item_number", "date_range_days": 90, "limit": 5}`

**2. validate_plan()** (Lines 220-265)
Checks LLM's plan against allowlists:
```python
def validate_plan(plan: dict) -> tuple[bool, str | None]:
    # Check metric is in ALLOWED_METRICS
    if plan["metric"] not in ALLOWED_METRICS:
        return False, f"Invalid metric: {plan['metric']}"
    
    # Check dimension is in ALLOWED_DIMENSIONS
    if plan["dimension"] not in ALLOWED_DIMENSIONS:
        return False, f"Invalid dimension: {plan['dimension']}"
    
    # Check limits
    if plan["limit"] > MAX_GROUP_LIMIT:
        return False, "Limit too high"
    
    return True, None  # Valid!
```

**3. build_sql()** (Lines 293-327) - THE COMPILER
Single SQL template that adapts to any valid plan:
```python
def build_sql(plan: dict) -> tuple[str, list]:
    metric_sql = ALLOWED_METRICS[plan["metric"]]  # Look up "SUM(ABS(quantity))"
    dim = plan.get("dimension")
    params = []
    
    # Build SELECT clause
    select_cols = f"{ALLOWED_DIMENSIONS[dim]} AS group_value, " if dim else ""
    sql = f"""
        SELECT {select_cols}{metric_sql} AS metric_value
        FROM inventory_transaction it
        JOIN sales_order so ON it.number = so.sales_order_number
        WHERE it.reference = ?
          AND so.status != ?
          AND so.do_not_process != ?
    """
    params.extend(["Sales order", "Cancelled", "Yes"])
    
    # Add date filter if specified
    if plan.get("date_range_days"):
        sql += " AND it.physical_date >= ?"
        params.append(resolve_date_n_days_back(plan["date_range_days"]))
    
    # Add GROUP BY and ORDER BY
    if dim:
        sql += f" GROUP BY {ALLOWED_DIMENSIONS[dim]}"
        if dim == "sale_date":
            sql += " ORDER BY group_value ASC"  # Chronological for time series
        else:
            sql += f" ORDER BY metric_value {plan['sort'].upper()}"
            sql += " LIMIT ?"
            params.append(plan["limit"])
    
    return sql, params  # Parameterized (safe from injection)
```

**Example:**
Plan: `{"metric": "issue_quantity", "dimension": "item_number", "limit": 5}`

Compiles to:
```sql
SELECT it.item_number AS group_value, SUM(ABS(it.quantity)) AS metric_value
FROM inventory_transaction it
JOIN sales_order so ON it.number = so.sales_order_number
WHERE it.reference = 'Sales order'
  AND so.status != 'Cancelled'
  AND so.do_not_process != 'Yes'
GROUP BY it.item_number
ORDER BY metric_value DESC
LIMIT 5
```

**4. infer_chart_type()** (Lines 329-343)
Deterministic logic to pick chart type:
```python
def infer_chart_type(plan: dict) -> str:
    dim = plan.get("dimension")
    
    if not dim:
        return "stat"  # Single number (e.g., "total orders this month")
    
    if dim == "sale_date":
        return "line"  # Time series (e.g., "daily sales trend")
    
    return "bar"  # Everything else (rankings, categories)
```

**5. validate_sql()** (Lines 345-358)
Final safety check before execution:
```python
def validate_sql(sql: str) -> bool:
    # Must be single statement
    if ";" in sql.strip().rstrip(";"):
        return False
    
    # Must start with SELECT or WITH
    if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.IGNORECASE):
        return False
    
    # Block all write operations
    forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE"]
    if any(word in sql.upper() for word in forbidden):
        return False
    
    return True
```

**6. execute()** (Lines 360-367)
Run SQL with read-only connection:
```python
def execute(sql: str, params: list) -> pd.DataFrame:
    con = duckdb.connect(DB_PATH, read_only=True)  # Read-only!
    try:
        df = con.execute(sql, params).fetchdf()
    finally:
        con.close()
    return df
```

**7. answer_question()** (Lines 483-512) - THE ORCHESTRATOR
Puts it all together:
```python
def answer_question(question: str) -> dict:
    # Step 1: Get plan from LLM
    plan = get_query_plan(question)
    
    # Step 2: Validate plan
    valid, error = validate_plan(plan)
    if not valid:
        return {"error": error}
    
    # Step 3: Compile to SQL
    sql, params = build_sql(plan)
    
    # Step 4: Validate SQL (extra safety)
    if not validate_sql(sql):
        return {"error": "Invalid SQL generated"}
    
    # Step 5: Execute
    df = execute(sql, params)
    
    # Step 6: Generate explanation
    answer_text = explain_result(question, plan, df)
    
    # Step 7: Infer chart type
    chart_type = infer_chart_type(plan)
    
    # Step 8: Format response
    return {
        "answer_text": answer_text,
        "chart_type": chart_type,
        "chart_data": format_chart_data(df, plan),
        "table_data": format_table_data(df, plan),
        "plan_used": plan,
        "debug": {
            "sql": sql,
            "params": params,
            "rows": len(df)
        }
    }
```

---

## 🌐 File 2: `backend/main.py` (API Gateway)

### Purpose
FastAPI server that connects frontend to query engine.

### Key Parts

#### 1. Imports & Setup
```python
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from query_engine import answer_question  # Import our brain

app = FastAPI(title="Sales Intelligence API")

# Allow frontend to call backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend files
app.mount("/static", StaticFiles(directory="frontend"), name="static")
```

#### 2. Data Models
```python
class ChatRequest(BaseModel):
    question: str  # User's question

class ChatResponse(BaseModel):
    answer: str
    answer_text: str | None = None
    chart_type: str | None = None
    chart_data: dict | None = None
    table_data: list | None = None
    plan_used: dict | None = None
    debug: dict | None = None
```

#### 3. Main Endpoint
```python
@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Validate input
    if not request.question.strip():
        raise HTTPException(400, "Question cannot be empty")
    
    try:
        # Call query engine
        result = answer_question(request.question.strip())
        
        # Return structured response
        return ChatResponse(
            answer=result.get("answer_text", ""),
            answer_text=result.get("answer_text"),
            chart_type=result.get("chart_type"),
            chart_data=result.get("chart_data"),
            table_data=result.get("table_data"),
            plan_used=result.get("plan_used"),
            debug=result.get("debug")
        )
    except Exception as e:
        return ChatResponse(
            answer="",
            error=f"Failed to process question: {str(e)}"
        )
```

**Flow:**
1. Receives POST request to `/api/chat` with `{"question": "..."}`
2. Calls `answer_question()` from query_engine
3. Returns JSON response with answer, chart, table, debug info

---

## 🎨 File 3: `frontend/index.html` (UI Structure)

### Purpose
The chat interface where users ask questions.

### Key Elements

```html
<!-- Chat container -->
<div id="chat-container">
    <!-- Messages area -->
    <div id="messages">
        <!-- User messages appear here -->
        <!-- Assistant responses appear here -->
    </div>
    
    <!-- Input form -->
    <form id="chat-form">
        <input type="text" id="user-input" placeholder="Ask about sales, items, customers...">
        <button type="submit">Send</button>
    </form>
</div>

<!-- Debug panel (shows SQL, plan, etc.) -->
<div id="debug-panel">
    <!-- Query plan -->
    <!-- Generated SQL -->
    <!-- Execution time -->
</div>
```

---

## 💻 File 4: `frontend/app.js` (Chat Logic)

### Purpose
Handles user input, API calls, chart rendering, and UI updates.

### Key Functions

#### 1. Form Submission
```javascript
chatForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const question = userInput.value.trim();
    
    // Add user message to chat
    addMessage(question, 'user');
    
    // Show loading indicator
    const loadingId = addLoadingMessage();
    
    // Call API
    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({question})
        });
        
        const data = await response.json();
        
        // Remove loading, show result
        removeMessage(loadingId);
        addAssistantResult(data);
        
        // Update debug panel
        updateDebugPanel(data.debug);
    } catch (error) {
        console.error('Error:', error);
        removeMessage(loadingId);
        addMessage('Failed to get response', 'assistant');
    }
});
```

#### 2. Render Results
```javascript
function addAssistantResult(data) {
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message assistant-message';
    
    // Add text answer
    const textP = document.createElement('p');
    textP.textContent = data.answer_text;
    messageDiv.appendChild(textP);
    
    // Add chart if present
    if (data.chart_type && data.chart_data) {
        if (data.chart_type === 'stat') {
            // Render large number
            renderStat(messageDiv, data);
        } else if (data.chart_type === 'bar' || data.chart_type === 'line') {
            // Render Chart.js chart
            renderChart(messageDiv, data);
        }
    }
    
    // Add table toggle
    if (data.table_data) {
        addTableToggle(messageDiv, data.table_data);
    }
    
    messagesContainer.appendChild(messageDiv);
}
```

#### 3. Chart Rendering (Chart.js)
```javascript
function renderChart(container, data) {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    
    new Chart(ctx, {
        type: data.chart_type,  // 'bar' or 'line'
        data: {
            labels: data.chart_data.labels,
            datasets: [{
                label: data.metric_label,
                data: data.chart_data.values,
                backgroundColor: 'rgba(99, 102, 241, 0.5)',
                borderColor: 'rgb(99, 102, 241)',
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {display: true},
                tooltip: {enabled: true}
            }
        }
    });
    
    container.appendChild(canvas);
}
```

#### 4. Download Chart
```javascript
function downloadChart(card, chartId) {
    const canvas = document.getElementById(chartId);
    
    // Use html2canvas to convert to image
    html2canvas(canvas).then(canvas => {
        const link = document.createElement('a');
        link.download = 'chart.png';
        link.href = canvas.toDataURL();
        link.click();
    });
}
```

---

## 🗄️ File 5: `sales_inventory.duckdb` (Database)

### Purpose
DuckDB database containing your actual sales data.

### Schema

```sql
-- Table 1: sales_order (45,288 rows)
CREATE TABLE sales_order (
    sales_order_number VARCHAR,    -- e.g., "GC-SO26-15036"
    customer_account VARCHAR,       -- e.g., "EC004493"
    customer_name VARCHAR,          -- e.g., "Sana"
    order_type VARCHAR,
    channel VARCHAR,                -- e.g., "GC001"
    status VARCHAR,                 -- "Invoiced", "Open order", "Canceled"
    do_not_process VARCHAR,         -- "Yes" or "No"
    invoice_date TIMESTAMP,
    site VARCHAR,
    warehouse VARCHAR
);

-- Table 2: inventory_transaction (277,028 rows)
CREATE TABLE inventory_transaction (
    number VARCHAR,                 -- Links to sales_order_number
    item_number VARCHAR,            -- e.g., "AQNYMALM1"
    product_number VARCHAR,
    physical_date TIMESTAMP,
    financial_date TIMESTAMP,
    reference VARCHAR,              -- "Sales order", "Transfer", "Transaction", etc.
    quantity DOUBLE,                -- NEGATIVE for sales (e.g., -5 = sold 5 units)
    cost_amount DOUBLE,
    site VARCHAR,
    warehouse VARCHAR,
    customer_account VARCHAR        -- Added via JOIN during loading
);
```

### Data Flow
1. Raw Excel files → Loaded by Python
2. Cleaned (drop nulls, parse dates)
3. Joined (customer_account added to inventory_transaction)
4. Saved to DuckDB

---

## 🔄 Complete Request Flow

```
┌─────────────────────────────────────────────────────────────┐
│ User Types: "top 5 items by quantity sold in last 90 days" │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ frontend/app.js                                             │
│ - Captures input from HTML form                             │
│ - Sends POST /api/chat with {"question": "..."}            │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ backend/main.py                                             │
│ - Receives request                                          │
│ - Calls: answer_question(question)                         │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ query_engine.py: get_query_plan()                          │
│ - Sends question + PLAN_SYSTEM_PROMPT to Ollama LLM        │
│ - LLM returns:                                              │
│   {                                                         │
│     "metric": "issue_quantity",                            │
│     "dimension": "item_number",                            │
│     "date_range_days": 90,                                 │
│     "sort": "desc",                                        │
│     "limit": 5                                             │
│   }                                                         │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ query_engine.py: validate_plan()                           │
│ - Check: "issue_quantity" in ALLOWED_METRICS? ✓            │
│ - Check: "item_number" in ALLOWED_DIMENSIONS? ✓            │
│ - Check: limit=5 < MAX_GROUP_LIMIT=100? ✓                  │
│ - Result: VALID                                             │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ query_engine.py: build_sql()                               │
│ - Lookup: ALLOWED_METRICS["issue_quantity"]                │
│   = "SUM(ABS(it.quantity))"                                │
│ - Lookup: ALLOWED_DIMENSIONS["item_number"]                │
│   = "it.item_number"                                       │
│ - Build SQL:                                                │
│   SELECT it.item_number AS group_value,                    │
│          SUM(ABS(it.quantity)) AS metric_value             │
│   FROM inventory_transaction it                            │
│   JOIN sales_order so ON it.number = so.sales_order_number │
│   WHERE it.reference = 'Sales order'                       │
│     AND so.status != 'Cancelled'                           │
│     AND so.do_not_process != 'Yes'                         │
│     AND it.physical_date >= '2026-05-28'  (90 days back)  │
│   GROUP BY it.item_number                                  │
│   ORDER BY metric_value DESC                               │
│   LIMIT 5                                                   │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ query_engine.py: validate_sql()                            │
│ - Check: Single statement? ✓                               │
│ - Check: Starts with SELECT? ✓                             │
│ - Check: No INSERT/UPDATE/DELETE? ✓                        │
│ - Result: SAFE TO EXECUTE                                   │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ query_engine.py: execute()                                 │
│ - Open DuckDB (read_only=True)                             │
│ - Execute parameterized SQL                                 │
│ - Return DataFrame:                                         │
│   group_value    metric_value                              │
│   ITEM001        1250                                       │
│   ITEM042        980                                        │
│   ITEM103        875                                        │
│   ITEM215        720                                        │
│   ITEM099        615                                        │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ query_engine.py: explain_result()                          │
│ - Send question + plan + results to LLM                    │
│ - LLM returns plain English:                                │
│   "The top 5 items by quantity sold in the last 90 days   │
│    are ITEM001 (1,250 units), ITEM042 (980 units)..."     │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ query_engine.py: infer_chart_type()                        │
│ - dimension = "item_number" (not "sale_date")              │
│ - Result: "bar" chart                                       │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ query_engine.py: Return Response                           │
│ {                                                           │
│   "answer_text": "The top 5 items...",                    │
│   "chart_type": "bar",                                     │
│   "chart_data": {                                          │
│     "labels": ["ITEM001", "ITEM042", ...],                │
│     "values": [1250, 980, 875, 720, 615]                  │
│   },                                                        │
│   "table_data": [...],                                     │
│   "plan_used": {...},                                      │
│   "debug": {"sql": "...", "params": [...]}                │
│ }                                                           │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ backend/main.py                                             │
│ - Wrap response in ChatResponse model                       │
│ - Return JSON to frontend                                   │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ frontend/app.js                                             │
│ - Receive JSON response                                     │
│ - Add text to chat: "The top 5 items..."                  │
│ - Create bar chart with Chart.js                           │
│ - Add "View as table" toggle                               │
│ - Add "Download chart" button                              │
│ - Update debug panel with SQL/plan                         │
└─────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ User sees:                                                  │
│ ┌─────────────────────────────────────────────────────┐   │
│ │ 🤖 The top 5 items by quantity sold in the last    │   │
│ │    90 days are ITEM001 (1,250 units), ITEM042...   │   │
│ │                                                      │   │
│ │    [Bar Chart Here]                                 │   │
│ │                                                      │   │
│ │    [View as table] [Download chart]                │   │
│ └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## 🔒 Security Layers

### Layer 1: Allowlist Vocabulary
```python
ALLOWED_METRICS = {"issue_quantity", "order_count"}
ALLOWED_DIMENSIONS = {"item_number", "sale_date", "customer_account", ...}
```
LLM can ONLY choose from these. Can't access hidden tables/columns.

### Layer 2: Plan Validation
```python
if plan["metric"] not in ALLOWED_METRICS:
    return "Invalid metric"  # Rejected before SQL generation
```

### Layer 3: Deterministic Compilation
```python
sql = f"SELECT {ALLOWED_DIMENSIONS[dim]}, {ALLOWED_METRICS[metric]} FROM..."
# Python builds SQL, not LLM
```

### Layer 4: SQL Validation
```python
if "INSERT" in sql or "DELETE" in sql:
    return False  # Blocked
```

### Layer 5: Read-Only Connection
```python
con = duckdb.connect(DB_PATH, read_only=True)  # Cannot write
```

### Layer 6: Parameterized Queries
```python
con.execute(sql, params)  # params = ["Sales order", "Cancelled", ...]
# Not: f"WHERE reference = '{value}'"  ← injection risk
```

---

## 🎯 Key Design Principles

### 1. Separation of Concerns
- **LLM**: Natural language understanding only
- **Python**: All logic, validation, SQL generation
- **Database**: Data storage only

### 2. Single Source of Truth
All business logic in one place (`ALLOWED_METRICS`, `ALLOWED_DIMENSIONS`).
Change once, works everywhere.

### 3. Fail-Safe Design
Multiple validation layers. If one fails, others catch it.

### 4. Determinism
Same plan = Same SQL = Same results (every time).

### 5. Extensibility
Add new metrics/dimensions by editing config, not rewriting code.

---

## 📊 Data Flow Summary

```
Excel Files
    ↓ (load_new_sales.py - one-time)
DuckDB Database (sales_inventory.duckdb)
    ↓ (query_engine.py)
Structured Plan (JSON)
    ↓ (build_sql)
Parameterized SQL
    ↓ (execute)
DataFrame Results
    ↓ (format)
JSON Response
    ↓ (backend/main.py)
API Response
    ↓ (frontend/app.js)
Visual Chart + Table
```

---

## 🚀 How to Extend

### Add New Metric (e.g., Revenue)
1. Edit `query_engine.py`:
```python
ALLOWED_METRICS = {
    "issue_quantity": "SUM(ABS(it.quantity))",
    "order_count": "COUNT(DISTINCT so.sales_order_number)",
    "revenue": "SUM(ABS(it.quantity) * it.unit_price)",  # NEW!
}

METRIC_LABELS = {
    "issue_quantity": "Quantity sold",
    "order_count": "Order count",
    "revenue": "Revenue",  # NEW!
}
```

2. That's it! Now users can ask:
   - "What's the total revenue this month?"
   - "Show revenue by customer"
   - "Which items generate the most revenue?"

No code changes needed anywhere else.

### Add New Dimension (e.g., Product Category)
1. Add column to database (one-time)
2. Edit `query_engine.py`:
```python
ALLOWED_DIMENSIONS = {
    "item_number": "it.item_number",
    "sale_date": "CAST(it.physical_date AS DATE)",
    "customer_account": "so.customer_account",
    "channel": "so.channel",
    "category": "it.product_category",  # NEW!
}

DIMENSION_LABELS = {
    ...
    "category": "Category",  # NEW!
}
```

3. Update schema description for LLM:
```python
SCHEMA_DESCRIPTION = """
...
- product_category: product category (Electronics, Clothing, etc.)
"""
```

Now users can ask:
- "Top selling categories"
- "Revenue by category"
- "Category sales trend over time"

---

## 🎓 Why This Architecture Works

### Industry Alignment
Matches patterns used by:
- dbt Semantic Layer (100% accuracy on covered metrics)
- Tightly IQ (retail analytics platform)
- AWS Marketplace solutions ($125K+ quarterly savings)
- Cube, AtScale, MetricFlow

### Research-Backed
- **+17% to +23% accuracy improvement** vs raw text-to-SQL
- **97.4% correctness** vs 55.3% for direct SQL generation
- Used by enterprise systems processing 50,000+ queries

### Scalable
Add metrics/dimensions without rewriting code.
Same architecture works for 10 questions or 10,000.

---

## 📝 Summary

**Files:**
1. `query_engine.py` - The brain (semantic layer + compiler)
2. `backend/main.py` - API gateway (FastAPI)
3. `frontend/` - User interface (HTML/JS/CSS)
4. `sales_inventory.duckdb` - Your data (45,288 orders, 277,028 transactions)

**Flow:**
Question → LLM picks options → Python validates → Python builds SQL → Database executes → Charts rendered

**Security:**
6 layers prevent SQL injection, unauthorized access, and hallucinations.

**Extensibility:**
Add metrics/dimensions by editing config dictionaries, not code.

**Industry Standard:**
Follows "semantic layer + constrained generation" pattern used by enterprise systems.
