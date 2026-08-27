# WrenAI vs Your Query Engine: Detailed Comparison

After studying WrenAI's SDK implementation (sdk/wren-langchain and sdk/wren-pydantic), here's how their "governed text-to-SQL" approach compares to your `query_engine.py`.

---

## a) How They Describe the Database Schema to the LLM

### WrenAI's Approach

They use **MDL (Modeling Definition Language)** — a semantic layer stored in `target/mdl.json`:

**MDL Structure** (from _providers/mdl_source.py):
```python
class ProjectMDLSource:
    def load_manifest(self) -> dict[str, Any]:
        # Reads target/mdl.json from disk
        # Contains: models, columns, relationships, views, cubes, metrics
        return json.loads(self._mdl_path.read_text())
```

**Schema Delivery** (from _prompt.py lines 47-50):
```python
_STEP_FETCH = """2. Fetch schema and business context:
   `wren_fetch_context(question="<user's question>")`
   Optionally narrow scope with `model="<name>"` or
   `item_type="model" | "column" | "relationship" | "view"`."""
```

The LLM calls `wren_fetch_context()` which retrieves:
- Relevant models and their columns
- Relationships (JOINs) between models
- Business definitions from `instructions.md`
- Past successful queries from memory (few-shot examples)

**Key Point**: The LLM writes SQL against **MDL model names**, not raw database tables. Example:
```sql
-- LLM writes this (targeting MDL):
SELECT customer_id, SUM(revenue) FROM orders GROUP BY customer_id

-- WrenEngine translates to actual DB:
SELECT t1.customer_id, SUM(t1.amount * t2.price) 
FROM raw_orders t1 
JOIN raw_items t2 ON t1.item_id = t2.id
GROUP BY t1.customer_id
```

### Your Query Engine's Approach

You use a **static schema description** with explicit table/column mappings:

```python
# From query_engine.py (lines 66-86)
SCHEMA_DESCRIPTION = f"""
You have two tables: sales_order and inventory_transaction.

sales_order: orders placed, with customer, channel, invoice_date, status, ...
inventory_transaction: item fulfillment, with item_number, quantity (negative = sold), ...

Join: inventory_transaction.number = sales_order.sales_order_number,
      only where inventory_transaction.reference = 'Sales order'
      AND inventory_transaction.quantity < 0 (negative = sold out)
"""
```

### Comparison

| Aspect | WrenAI | Your Query Engine | Better For Your Case? |
|--------|---------|-------------------|----------------------|
| **Schema Source** | MDL semantic layer (JSON manifest) | Static Python constant | **Your approach is better** - 2 tables don't need MDL |
| **Abstraction Level** | LLM writes SQL against virtual models | LLM picks from metric/dimension vocab | **Your approach is more constrained** (safer) |
| **Business Logic** | MDL defines JOINs, calculations | Hardcoded in Python compiler | **WrenAI wins for complex logic**, but overkill for 2 tables |
| **Dynamic Updates** | MDL can be rebuilt without code changes | Requires code changes | **WrenAI wins** - but only matters if schema changes often |

**Verdict**: WrenAI's MDL is designed for **20+ complex databases with evolving schemas**. Your static description + constrained vocabulary is **perfect for 2 stable tables**. WrenAI's abstraction layer (MDL models → raw SQL) is more flexible but adds complexity you don't need.

---

## b) Does the LLM Write Raw SQL or Fill a Restricted Plan?

### WrenAI's Approach

**The LLM writes SQL** targeting MDL model names, **then WrenEngine validates and translates it**:

From _prompt.py (lines 56-57, 59-62):
```python
_STEP_COMPOSE = (
    "{n}. Compose SQL targeting Wren model names — NEVER raw database tables."
)

_STEP_DRY_PLAN = """{n}. (Complex queries only) Verify with `wren_dry_plan(sql="...")` before
   executing. "Complex" = subqueries, multi-step CTEs, or JOINs not
   already defined as MDL relationships. Simple GROUP BY or
   model-defined JOINs can skip this step."""
```

**Example Workflow**:
```
1. LLM writes: SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id
2. Agent calls: wren_dry_plan(sql="...")
3. Engine validates against MDL and returns expanded SQL
4. Agent calls: wren_query(sql="...", limit=100)
5. Engine executes validated SQL
```

From _tools.py (lines 82-101):
```python
@tool("wren_dry_plan")
def wren_dry_plan(sql: str) -> dict[str, Any]:
    """Plan SQL through MDL and return the expanded target-dialect SQL.
    
    Use this to verify your SQL targets Wren models correctly before
    running wren_query. Cheap (no DB round-trip).
    """
    try:
        dialect_sql = toolkit.dry_plan(sql)  # Validates + translates
    except Exception as exc:
        return make_error(exc)  # Returns structured error
    
    return make_success(
        content=format_dry_plan_content(dialect_sql),
        data={"dialect_sql": dialect_sql},
    )
```

**Validation happens in two stages**:
1. **dry_plan** (optional): Validates SQL syntax and MDL model references
2. **query execution**: Final validation + execution with row limits

### Your Query Engine's Approach

**The LLM fills a restricted plan**, then Python compiles it to SQL:

From query_engine.py (lines 143-159):
```python
PLAN_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {"type": "string", "enum": ["order_count", "issue_quantity", "forecast_sales", "unsupported"]},
        "dimension": {"type": "string", "enum": ["item_number", "sale_date", "customer_account", ...] + [None]},
        "date_range_days": {"type": ["integer", "null"]},
        "sort": {"type": "string", "enum": ["asc", "desc"]},
        "limit": {"type": ["integer", "null"]},
    },
}
```

Then `build_sql()` compiles this to SQL — no LLM writes any SQL.

### Comparison

| Aspect | WrenAI | Your Query Engine | Better For Your Case? |
|--------|---------|-------------------|----------------------|
| **LLM Output** | SQL (targeting MDL models) | Structured plan (metric/dimension/filters) | **Your approach is more constrained** |
| **SQL Compiler** | WrenEngine (Rust/Python) | Deterministic Python | **Tie** - both are deterministic |
| **Validation** | dry_plan validates MDL references | validate_plan checks allowlist | **Your approach is stricter** |
| **Hallucination Risk** | Medium (can write invalid JOINs, wrong column names) | **Zero** - can't pick non-existent metrics | **Your approach wins** |
| **Flexibility** | High (can write any SQL targeting MDL) | Low (fixed metrics/dimensions only) | **Trade-off** - depends on use case |
| **Correctness** | Depends on MDL definitions + LLM | **Guaranteed by compiler** | **Your approach wins** |

**Verdict**: WrenAI's approach is **hybrid**: LLM writes SQL but it's **validated against a semantic layer** (MDL). This is more flexible than your plan-based approach but less safe:

- ✅ **Safer than DB-GPT** (raw SQL with no semantic layer)
- ❌ **Less safe than yours** (LLM can still hallucinate model/column names or write bad JOINs)

Your **zero-hallucination plan-based approach** is stricter and more appropriate for a system with fixed metrics.

---

## c) Is There Validation Before SQL Runs?

### WrenAI's Approach

**Three validation layers**:

**Layer 1: dry_plan Validation** (optional, recommended for complex queries)
```python
# From _toolkit.py lines 70-72
def dry_plan(self, sql: str) -> str:
    """Plan SQL through MDL and return the expanded SQL in target dialect."""
    return self._build_engine().dry_plan(sql)
```

This checks:
- SQL syntax
- MDL model/column names exist
- Relationships (JOINs) are valid
- Returns translated SQL for target database

**Layer 2: Structured Error Handling** (from _errors.py lines 82-126)
```python
def _build_message(exc: WrenError) -> str:
    phase = exc.phase
    
    if phase == ErrorPhase.SQL_PARSING:
        framing = f"SQL parse error: {msg}. Fix the SQL syntax and retry."
    elif phase == ErrorPhase.SQL_PLANNING:
        framing = f"SQL planning error: {msg}. Check model/column names and retry."
    elif phase == ErrorPhase.SQL_EXECUTION:
        framing = f"Database execution error: {msg}."
        # Includes dialect SQL excerpt for debugging
    elif phase == ErrorPhase.METADATA_FETCHING:
        framing = f"Metadata lookup failed: {msg}. Verify the model name and retry."
    # ... more phases
```

Error phases:
- `SQL_PARSING`: Syntax errors
- `SQL_PLANNING`: Invalid model/column references
- `SQL_TRANSPILE`: Target dialect issues
- `SQL_DRY_RUN`: Query invalid at planning
- `SQL_EXECUTION`: Database-side errors
- `METADATA_FETCHING`: Model lookup failed
- `MDL_EXTRACTION`: Schema reference errors
- `VALIDATION`: General validation errors

**Layer 3: Row Limits** (from _tools.py lines 26-32, 46-60)
```python
MAX_QUERY_ROWS = 1000

def wren_query(sql: str, limit: int = 100) -> dict[str, Any]:
    """Execute SQL through the Wren semantic layer and return rows.
    
    Default limit is 100 rows; increase only when you need more.
    Hard cap is 1000 rows — beyond that, aggregate in SQL instead.
    """
    if limit < 1 or limit > MAX_QUERY_ROWS:
        err = ValueError(f"limit must be between 1 and {MAX_QUERY_ROWS}")
        return make_error(err)
```

### Your Query Engine's Approach

**Two validation layers**:

**Layer 1: Plan Validation** (lines 266-318):
```python
def validate_plan(plan: dict) -> tuple[bool, str | None]:
    # Check metric is in allowlist
    if metric not in ALLOWED_METRICS:
        return False, "I don't have data for that"
    
    # Check dimension compatibility with metric's source table
    if dim is not None:
        source = METRIC_SOURCE[metric]
        if source != "forecast" and source in DIMENSION_SQL:
            if dim not in DIMENSION_SQL[source]:
                return False, f"I can't group '{metric}' by '{dim}'"
    
    # Validate sort/limit/date_range...
```

**Layer 2: SQL Validation** (lines 218-242):
```python
def validate_sql(sql: str) -> bool:
    forbidden = [
        "DROP", "DELETE", "TRUNCATE", "ALTER", "CREATE", "INSERT",
        "UPDATE", "GRANT", "REVOKE", "EXEC", "EXECUTE", "--", ";", "/*", "*/"
    ]
    for word in forbidden:
        if word in sql_upper:
            return False
    return True
```

Plus: **SQL is compiled from validated plan**, so injection is impossible.

### Comparison

| Aspect | WrenAI | Your Query Engine | Better For Your Case? |
|--------|---------|-------------------|----------------------|
| **Validation Approach** | MDL semantic layer validation | Allowlist + compiled SQL | **Your approach is stricter** |
| **Can Bypass?** | Medium (LLM can write invalid SQL) | **No** - can't pick disallowed metrics | **Your approach wins** |
| **Error Feedback** | Structured errors with phases | Simple error messages | **WrenAI wins** - better LLM recovery |
| **Schema Validation** | Yes (MDL model/column names) | Yes (metric/dimension compatibility) | **Tie** |
| **SQL Injection Risk** | Low (MDL layer + parameterization) | **Zero** (compiled + parameterized) | **Your approach wins** |
| **Complexity** | High (Rust engine, error phases) | Low (simple Python validation) | **Your approach is simpler** |

**Verdict**: WrenAI's **structured error handling with phases** is excellent for LLM self-correction. Your **allowlist validation** is simpler and guarantees zero hallucinations. Both are secure, but:

- **WrenAI**: Better for complex schemas where LLM needs detailed error feedback
- **Yours**: Better for fixed metrics where you want zero risk of hallucination

---

## d) How Does It Decide Chart Type?

### WrenAI's Approach

**WrenAI does NOT handle chart selection** - it focuses on:
1. **Generating SQL** (text-to-SQL)
2. **Building dashboards** (GenBI apps deployed to Vercel/Cloudflare)
3. **Returning tabular data** (pyarrow Tables)

From _tools.py (lines 68-76):
```python
content, warnings = format_query_content(table, total_rows=table.num_rows)
data = {
    "columns": table.column_names,
    "rows": table.to_pylist(),
    "row_count": table.num_rows,
    "content_truncated": bool(warnings),
}
return make_success(content=content, data=data, warnings=warnings)
```

They return **raw data** - chart type selection is delegated to:
- The agent building the dashboard
- The GenBI app framework
- External visualization tools

### Your Query Engine's Approach

**Deterministic function** based on plan structure:

From query_engine.py (lines 432-459):
```python
def infer_chart_type(plan: dict) -> str:
    metric = plan.get("metric")
    dim = plan.get("dimension")
    
    # Forecast: special chart type
    if metric == "forecast_sales":
        return "forecast"
    
    # No dimension: single stat
    if dim is None:
        return "stat"
    
    # Date dimension: line chart
    if dim == "sale_date":
        return "line"
    
    # Other dimensions: bar chart
    return "bar"
```

### Comparison

| Aspect | WrenAI | Your Query Engine | Better For Your Case? |
|--------|---------|-------------------|----------------------|
| **Chart Decision** | Not built-in (returns raw data) | Deterministic function | **Your approach is better** for inline chat |
| **Consistency** | N/A | **Guaranteed** - same input → same chart | **Your approach wins** |
| **Customization** | External tools handle it | Fixed rules | **Trade-off** |

**Verdict**: WrenAI's focus is on **governed SQL generation**, not visualization. Your **deterministic chart inference** is perfect for a chat interface where you need inline visualizations.

---

## Overall Summary

### What WrenAI Does Well

1. ✅ **Semantic layer (MDL)** - Abstracts raw tables into business models
2. ✅ **dry_plan validation** - Catches errors before execution
3. ✅ **Structured error handling** - Error phases help LLM self-correct
4. ✅ **Memory system** - Stores successful NL→SQL pairs for few-shot examples
5. ✅ **Enterprise features** - Multi-database support (20+ sources), Git-friendly MDL
6. ✅ **Agent workflow** - Clear steps: recall → fetch → compose → validate → execute → store

### What WrenAI Does Poorly (For Your Case)

1. ❌ **Overkill for 2 tables** - MDL semantic layer is designed for 20+ databases
2. ❌ **More complex** - Rust engine, MDL builder, multiple SDK layers
3. ❌ **LLM writes SQL** - Can still hallucinate model/column names (though MDL catches it)
4. ❌ **No chart inference** - Returns raw data, delegates visualization

### What Your Query Engine Does Well

1. ✅ **Zero hallucination** - Plan-based approach prevents SQL hallucinations
2. ✅ **Perfect for 2 tables** - No unnecessary abstraction layers
3. ✅ **Simpler** - 700 lines vs. multi-package system
4. ✅ **Integrated forecasting** - Prophet built-in (WrenAI doesn't have this)
5. ✅ **Chart inference** - Automatic, consistent visualization

### What WrenAI Does Better (In General)

1. **Structured error feedback** - Error phases help LLM learn and recover
2. **Memory/few-shot system** - Learns from successful queries
3. **Scales to complex schemas** - MDL handles 20+ databases with complex JOINs
4. **Agent workflow** - Clear, testable workflow steps

---

## Key Architectural Differences

### WrenAI: Hybrid Approach

```
User Question
    ↓
LLM writes SQL (targeting MDL model names)
    ↓
dry_plan validates against MDL
    ↓
WrenEngine translates MDL SQL → Database SQL
    ↓
Execute with row limits
    ↓
Store successful query in memory
```

**Philosophy**: LLM has freedom to write SQL, but **MDL semantic layer acts as guardrails**.

### Your Approach: Pure Semantic Layer

```
User Question
    ↓
LLM fills structured plan (metric/dimension/filters)
    ↓
validate_plan checks allowlist
    ↓
build_sql compiles plan → SQL (deterministic)
    ↓
Execute with parameterized queries
    ↓
infer_chart_type → visualization
```

**Philosophy**: LLM **never writes SQL** - it only picks from pre-defined vocabulary.

---

## What You Could Borrow from WrenAI

If you want to adopt **ideas** from WrenAI, consider:

### 1. Structured Error Handling (HIGHLY RECOMMENDED)

Instead of generic error messages, return **error phases**:

```python
class ErrorPhase:
    PLAN_VALIDATION = "plan_validation"  # Invalid metric/dimension
    SQL_COMPILATION = "sql_compilation"   # Compiler error
    SQL_EXECUTION = "sql_execution"       # Database error
    FORECAST_GENERATION = "forecast_generation"  # Prophet error

def answer_question(question: str, ...) -> dict:
    try:
        # ... existing code ...
    except ValueError as e:
        return {
            "error": {
                "phase": ErrorPhase.PLAN_VALIDATION,
                "message": str(e),
                "hint": "Check your question format"
            }
        }
    except duckdb.Error as e:
        return {
            "error": {
                "phase": ErrorPhase.SQL_EXECUTION,
                "message": str(e),
                "sql": sql,  # Show what failed
            }
        }
```

This helps you debug issues and could help if you add LLM self-correction later.

### 2. Memory/Few-Shot System (OPTIONAL, COMPLEX)

Store successful queries for few-shot examples:

```python
# queries.yml (Git-friendly)
- question: "top 5 customers by order count"
  plan:
    metric: "order_count"
    dimension: "customer_account"
    sort: "desc"
    limit: 5
  sql: "SELECT customer_account, COUNT(*) FROM sales_order..."
  tags: ["customers", "ranking"]
```

Then retrieve similar queries:
```python
def get_similar_queries(question: str, limit: int = 3) -> list:
    # Use embedding similarity to find relevant past queries
    # Add them to LLM prompt as few-shot examples
```

**Verdict**: This is a LOT of work (embedding model, vector DB, memory management). Only add if you see the LLM making repeated mistakes.

### 3. Workflow Documentation (EASY WIN)

Add a workflow prompt to your system:

```python
WORKFLOW_PROMPT = """
When answering data questions:
1. Parse the question to identify the metric and filters
2. Check if the metric is in the allowed list
3. Generate a structured plan (JSON)
4. Validate the plan
5. Execute the query
6. Determine the appropriate chart type
7. Format the response
"""
```

This helps the LLM understand your system better.

---

## Final Recommendation

### For Your 2-Table System

| Feature | WrenAI | Your Query Engine | Winner |
|---------|--------|-------------------|--------|
| **Schema Description** | MDL semantic layer | Static constant | **You** (no MDL needed) |
| **SQL Generation** | LLM writes SQL → validate | LLM fills plan → compile | **You** (zero hallucination) |
| **Validation** | dry_plan + structured errors | Allowlist + schema check | **Tie** (different approaches) |
| **Error Feedback** | Structured phases | Simple messages | **WrenAI** (better recovery) |
| **Chart Selection** | Not built-in | Deterministic rules | **You** (needed for chat UI) |
| **Forecasting** | Not built-in | Prophet integration | **You** |
| **Complexity** | High (multi-package, Rust engine) | Low (700 lines) | **You** |
| **Scales to 20+ DBs** | Yes (MDL designed for this) | No (2 tables only) | **WrenAI** (not relevant for you) |

---

## Conclusion

**WrenAI is a production-grade "governed text-to-SQL" platform** designed for:
- **20+ complex databases**
- **Evolving schemas**
- **Multiple teams** needing a shared semantic layer
- **Agent-driven workflows** with memory and self-correction

**Your query engine is a lightweight semantic layer** designed for:
- **2 stable tables**
- **Fixed metrics**
- **Zero-hallucination guarantee**
- **Integrated forecasting and chat visualization**

### Should You Adopt WrenAI's Approach?

**No.** Here's why:

1. **MDL is overkill** - You have 2 tables, WrenAI is designed for 20+
2. **Your approach is stricter** - Zero hallucination vs. validated SQL
3. **Simpler codebase** - 700 lines vs. multi-package Rust+Python system
4. **Different goals** - You need inline charts, they focus on dashboard generation

### What To Borrow (Optional)

Only if you're adding features later:

1. ✅ **Structured error handling** - Error phases for better debugging
2. ⚠️ **Memory/few-shot** - Only if LLM makes repeated mistakes (complex to add)
3. ✅ **Workflow documentation** - Simple prompt improvement

**Bottom Line**: Your architecture is **perfectly suited** for your use case. WrenAI's approach is more sophisticated but also more complex. The only ideas worth borrowing are **structured error handling** (easy win) and **workflow documentation** (simple improvement).

Keep your plan-based semantic layer - it's the right architecture for a 2-table system with fixed metrics!
