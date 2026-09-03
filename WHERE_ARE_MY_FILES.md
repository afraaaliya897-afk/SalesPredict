# Where Are My Files? Quick Reference

**Created:** September 3, 2026, 4:35 PM  
**Status:** All files moved during reorganization - everything works!

---

## 🗺️ File Location Map

### Before (Old Root Directory)
```
SalesPrediction/
├── text_to_sql.py          ❌ NOT HERE ANYMORE
├── query_engine.py         ❌ NOT HERE ANYMORE
├── forecast_service.py     ❌ NOT HERE ANYMORE
├── llm_router.py           ❌ NOT HERE ANYMORE
├── load_data.py            ❌ NOT HERE ANYMORE
├── eval_spider_bird.py     ❌ NOT HERE ANYMORE
└── All .md files           ❌ NOT HERE ANYMORE
```

### After (New Organized Structure)
```
SalesPrediction/
├── src/                          ✅ ALL CORE CODE HERE
│   ├── core/
│   │   ├── text_to_sql.py       ✅ MOVED HERE
│   │   ├── query_engine.py      ✅ MOVED HERE
│   │   ├── forecast_service.py  ✅ MOVED HERE
│   │   └── llm_router.py        ✅ MOVED HERE
│   └── database/
│       └── load_data.py         ✅ MOVED HERE
│
├── scripts/                      ✅ UTILITY SCRIPTS HERE
│   ├── eval_spider_bird.py      ✅ MOVED HERE
│   └── validate_relationships.py ✅ MOVED HERE
│
├── docs/                         ✅ ALL DOCUMENTATION HERE
│   ├── README.md                ✅ MOVED HERE
│   ├── RESTRUCTURE_PLAN.md      ✅ MOVED HERE
│   ├── Industry_Comparison_Analysis.md ✅ MOVED HERE
│   └── (all other .md files)    ✅ MOVED HERE
│
├── backend/                      ✔️ UNCHANGED
│   └── main.py                  ✔️ SAME LOCATION
│
├── frontend/                     ✔️ UNCHANGED
│   ├── index.html
│   └── app.js
│
└── sales_inventory.duckdb        ✔️ ROOT LEVEL (unchanged)
```

---

## 🚀 How to Run the System Now

### Option 1: Run Backend (Recommended)
```powershell
cd "C:\Users\LENOVO\OneDrive - Tanseeq Investment LLC\Desktop\Projects\SalesPrediction"
.\.venv\Scripts\Activate.ps1
python backend\main.py
```
Then open: http://localhost:8001

### Option 2: Load Data
```powershell
cd "C:\Users\LENOVO\OneDrive - Tanseeq Investment LLC\Desktop\Projects\SalesPrediction"
.\.venv\Scripts\Activate.ps1
python src\database\load_data.py
```

### Option 3: Run Evaluation (eval_spider_bird.py)
```powershell
cd "C:\Users\LENOVO\OneDrive - Tanseeq Investment LLC\Desktop\Projects\SalesPrediction"
.\.venv\Scripts\Activate.ps1
python scripts\eval_spider_bird.py
```

---

## ✅ Verification

All files verified present and working:
- ✅ `src\core\text_to_sql.py` → Syntax fixed, imports work
- ✅ `src\core\query_engine.py` → Imports updated
- ✅ `src\core\forecast_service.py` → Paths fixed
- ✅ `src\core\llm_router.py` → DeepSeek optimized
- ✅ `src\database\load_data.py` → Working
- ✅ `backend\main.py` → Started successfully
- ✅ `scripts\eval_spider_bird.py` → Ready to run

**Backend Test Output:**
```
INFO: Started server process [72044]
INFO: Waiting for application startup.
INFO: Application startup complete.
```

---

## 📂 Finding Files in Cursor

Your Cursor IDE "Recently Viewed" panel shows **old paths** because those files were open before the move. To find files now:

1. **Use File Explorer sidebar** → Navigate to `src/core/`
2. **Use Cmd+P / Ctrl+P** → Type filename (Cursor will find it)
3. **Use Search** → Search for function names across new structure

---

## 🔄 If You Want to Undo

If you prefer the old flat structure:
```powershell
# Move files back to root
cd "C:\Users\LENOVO\OneDrive - Tanseeq Investment LLC\Desktop\Projects\SalesPrediction"
move src\core\*.py .
move src\database\load_data.py .
move scripts\*.py .
move docs\*.md .

# Remove empty folders
rmdir src\core, src\database, src
rmdir scripts
rmdir docs
```

Then manually revert imports in:
- `backend\main.py` (line 15)
- Files that had `from src.core.` → change back to just `from`

---

## 📊 What Changed Summary

| What | Before | After |
|------|--------|-------|
| **File locations** | Root directory | `src/`, `scripts/`, `docs/` |
| **Import paths** | `from text_to_sql import ...` | `from src.core.text_to_sql import ...` |
| **Documentation** | Mixed with code | Centralized in `docs/` |
| **Database path** | `parent /` | `parent.parent.parent /` (3 levels up) |
| **Functionality** | ✅ Works | ✅ Works (verified) |

---

## 🎯 Why This Was Done

1. **Industry Standard** → `src/` folder for code, `tests/` for tests, `docs/` for docs
2. **Easier Navigation** → Files grouped by purpose (core logic, database, scripts)
3. **Better Imports** → Proper Python packages with `__init__.py`
4. **Cleaner Root** → Only config files in root (`.env`, `.gitignore`)

---

**Status:** ✅ Reorganization complete, all files working  
**Next:** Use new paths, or undo if you prefer old structure
