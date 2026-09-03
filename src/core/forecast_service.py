"""
Forecasting service integrated into chat.

Uses Prophet for time series forecasting with automatic seasonality detection.
"""

import pandas as pd
import duckdb
import re
from datetime import timedelta
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent.parent.parent / "sales_inventory.duckdb")

def get_historical_sales(
    days_back: int | None = None,
    db_path: str = DB_PATH,
    item_number: str | None = None,
) -> pd.DataFrame:
    """Daily sold units from v_sold (same filters as chat SQL).

    days_back=None uses every year on file. A positive days_back still trims from the latest date.
    item_number limits the series to one SKU when the user named an item.
    """
    from src.core.text_to_sql import ensure_views

    ensure_views(db_path)
    con = duckdb.connect(db_path, read_only=True)
    try:
        (max_date,) = con.execute("SELECT MAX(sale_date) FROM v_sold").fetchone()
        if max_date is None:
            return pd.DataFrame(columns=["date", "quantity"])
        params = []
        extra_filters = []
        if days_back is not None:
            cutoff_date = pd.Timestamp(max_date) - timedelta(days=int(days_back))
            extra_filters.append("AND sale_date >= ?")
            params.append(cutoff_date)
        if item_number:
            extra_filters.append("AND UPPER(CAST(item_number AS VARCHAR)) = UPPER(?)")
            params.append(item_number)
        date_filter = "\n              ".join(extra_filters)
        sql = f"""
            SELECT sale_date as date, SUM(sold_qty) as quantity
            FROM v_sold
            WHERE 1=1
              {date_filter}
            GROUP BY sale_date
            ORDER BY date ASC
        """
        df = con.execute(sql, params).df()

        if len(df) > 0:
            date_range = pd.date_range(start=df["date"].min(), end=df["date"].max(), freq="D")
            df = df.set_index("date").reindex(date_range, fill_value=0).reset_index()
            df.columns = ["date", "quantity"]

        return df
    finally:
        con.close()


_FORECAST_STOPWORDS = {
    "forecast", "forecasts", "forecasting", "predict", "prediction", "predictions",
    "projection", "projections", "sales", "sale", "sold", "next", "year", "years",
    "month", "months", "week", "weeks", "day", "days", "daily", "weekly", "monthly",
    "item", "items", "product", "products", "sku", "skus", "units", "unit",
    "quantity", "demand", "the", "for", "and", "from", "this", "that", "with",
    "seasonal", "baseline", "confidence", "model", "models", "prophet",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
}


def forecast_item_ranking(
    window_start,
    window_end,
    limit: int = 5,
    db_path: str = DB_PATH,
) -> dict:
    """Rank items for a future window using yearly seasonal naive (same days last year).

    That model won the all-item backtest. Running Prophet on every SKU is not practical.
    """
    start = pd.Timestamp(window_start).normalize()
    end = pd.Timestamp(window_end).normalize()
    prior_start = _prior_calendar_day(start)
    prior_end = _prior_calendar_day(end)
    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute(
            """
            SELECT
                CAST(it.item_number AS VARCHAR) AS item_number,
                SUM(-it.quantity) AS forecast_qty
            FROM inventory_transaction it
            LEFT JOIN sales_order so ON it.number = so.sales_order_number
            WHERE it.reference = 'Sales order'
              AND it.issue = 'Sold'
              AND it.quantity < 0
              AND CAST(it.physical_date AS DATE) BETWEEN ? AND ?
              AND (
                    so.sales_order_number IS NULL
                    OR (
                        so.status NOT IN ('Canceled', 'Cancelled')
                        AND so.do_not_process != 'Yes'
                    )
                  )
            GROUP BY 1
            HAVING SUM(-it.quantity) > 0
            ORDER BY 2 DESC
            LIMIT ?
            """,
            [prior_start.date(), prior_end.date(), int(limit)],
        ).fetchall()
    finally:
        con.close()
    items = [
        {"item_number": row[0], "forecast_qty": round(float(row[1]), 1)}
        for row in rows
    ]
    return {
        "model": "Seasonal naive (yearly)",
        "method": "same calendar period last year",
        "prior_start": prior_start.date().isoformat(),
        "prior_end": prior_end.date().isoformat(),
        "items": items,
    }


def resolve_forecast_item(
    question: str,
    llm_item: str | None = None,
    db_path: str = DB_PATH,
) -> dict:
    """Resolve an item from the question or LLM, against sold SKUs. No hardcoded catalog."""
    hints = []
    if llm_item and str(llm_item).strip():
        hints.append(str(llm_item).strip())
    text = question or ""
    for match in re.finditer(r"(?:item|product|sku)\s+([A-Za-z0-9][A-Za-z0-9\-_/]{2,})", text, re.I):
        hints.append(match.group(1))
    for match in re.finditer(r"['\"]([A-Za-z0-9][A-Za-z0-9\-_/]{2,})['\"]", text):
        hints.append(match.group(1))
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-_/]{3,}", text):
        if token.lower() in _FORECAST_STOPWORDS or token.isdigit() or re.fullmatch(r"20\d{2}", token):
            continue
        hints.append(token)

    ordered = []
    seen = set()
    for hint in hints:
        key = hint.upper()
        if key not in seen:
            seen.add(key)
            ordered.append(hint)

    if not ordered:
        return {"item_number": None, "label": "all sold items"}

    con = duckdb.connect(db_path, read_only=True)
    try:
        lookup_sql = """
            SELECT item_number, COUNT(*) AS n
            FROM inventory_transaction
            WHERE reference = 'Sales order' AND issue = 'Sold' AND quantity < 0
              AND (
                    UPPER(CAST(item_number AS VARCHAR)) {op}
                    OR UPPER(CAST(product_number AS VARCHAR)) {op}
                  )
            GROUP BY 1
            ORDER BY n DESC
            LIMIT 6
        """
        for hint in ordered:
            exact = con.execute(lookup_sql.format(op="= UPPER(?)"), [hint, hint]).fetchall()
            if exact:
                return {
                    "item_number": exact[0][0],
                    "label": f"item {exact[0][0]}",
                    "matched": hint,
                }
        for hint in ordered:
            rows = con.execute(lookup_sql.format(op="LIKE UPPER(?)"), [f"%{hint}%", f"%{hint}%"]).fetchall()
            if len(rows) == 1:
                return {
                    "item_number": rows[0][0],
                    "label": f"item {rows[0][0]}",
                    "matched": hint,
                }
            if len(rows) > 1:
                names = ", ".join(str(row[0]) for row in rows[:5])
                return {
                    "error": f'"{hint}" matches more than one item. Name one of: {names}',
                }
        return {
            "error": f'No sold item matched "{ordered[0]}". Use an item number from the sales data.',
        }
    finally:
        con.close()


def _clip_non_negative(forecast_rows: list[dict]) -> list[dict]:
    clipped = []
    for row in forecast_rows:
        qty = max(0.0, float(row["quantity"]))
        lo = max(0.0, float(row.get("lower", qty)))
        hi = max(qty, float(row.get("upper", qty)))
        clipped.append({**row, "quantity": qty, "lower": min(lo, hi), "upper": max(lo, hi)})
    return clipped


def _wape(actual: pd.Series, predicted: pd.Series) -> float | None:
    denom = actual.abs().sum()
    if denom == 0:
        return None
    return float((actual - predicted).abs().sum() / denom)


def _holdout_size(n: int) -> int:
    if n < 21:
        return 0
    return min(30, max(7, n // 4))


def _seasonal_naive_forecast(y: pd.Series, days_ahead: int, season: int = 7) -> pd.Series:
    if len(y) < season:
        last = float(y.iloc[-1]) if len(y) else 0.0
        return pd.Series([last] * days_ahead)
    pattern = y.iloc[-season:].tolist()
    return pd.Series([pattern[i % season] for i in range(days_ahead)])


def _moving_average_forecast(y: pd.Series, days_ahead: int, window: int = 7) -> pd.Series:
    last = float(y.iloc[-window:].mean()) if len(y) else 0.0
    return pd.Series([last] * days_ahead)


def _backtest_seasonal_naive(y: pd.Series, holdout: int, season: int = 7) -> float | None:
    if len(y) <= holdout + season:
        return None
    actual = y.iloc[-holdout:]
    pred = y.shift(season).iloc[-holdout:]
    return _wape(actual, pred)


def _backtest_moving_average(y: pd.Series, holdout: int, window: int = 7) -> float | None:
    if len(y) <= holdout + window:
        return None
    actual = y.iloc[-holdout:]
    pred = y.shift(1).rolling(window=window, min_periods=window).mean().iloc[-holdout:]
    return _wape(actual, pred)


def forecast_with_prophet(historical_df: pd.DataFrame, days_ahead: int = 30) -> dict:
    """Generate forecast using Prophet.
    
    Returns:
        dict with 'historical', 'forecast', 'lower_bound', 'upper_bound' data
    """
    try:
        from prophet import Prophet
    except ImportError:
        return {
            "error": "Prophet not installed. Run: pip install prophet",
            "historical": historical_df.to_dict('records'),
        }
    
    # Prepare data for Prophet
    prophet_df = historical_df.copy()
    prophet_df.columns = ['ds', 'y']  # Prophet requires 'ds' and 'y' columns
    
    # Train model
    model = Prophet(
        daily_seasonality=False,
        weekly_seasonality=True,
        yearly_seasonality=True if len(prophet_df) >= 365 else False,
        changepoint_prior_scale=0.05,  # Flexibility for trend changes
    )
    
    model.fit(prophet_df)
    
    # Generate forecast
    future = model.make_future_dataframe(periods=days_ahead)
    forecast = model.predict(future)
    
    # Split historical and forecast
    historical_dates = set(prophet_df['ds'].dt.date)
    forecast['is_forecast'] = ~forecast['ds'].dt.date.isin(historical_dates)
    
    historical = forecast[~forecast['is_forecast']][['ds', 'yhat']].copy()
    historical.columns = ['date', 'quantity']
    
    future_forecast = forecast[forecast['is_forecast']][['ds', 'yhat', 'yhat_lower', 'yhat_upper']].copy()
    future_forecast.columns = ['date', 'quantity', 'lower', 'upper']
    future_forecast['quantity'] = future_forecast['quantity'].clip(lower=0)
    future_forecast['lower'] = future_forecast['lower'].clip(lower=0)
    future_forecast['upper'] = future_forecast['upper'].clip(lower=0)
    
    return {
        "historical": historical_df[["date", "quantity"]].to_dict("records"),
        "forecast": future_forecast.to_dict("records"),
        "model": "Prophet",
        "days_ahead": days_ahead,
    }


def forecast_simple_moving_average(historical_df: pd.DataFrame, days_ahead: int = 30, window: int = 7) -> dict:
    """Simple moving average forecast (fallback if Prophet fails)."""
    
    # Calculate moving average
    historical_df['ma'] = historical_df['quantity'].rolling(window=window, min_periods=1).mean()
    
    # Last MA value becomes the forecast
    last_ma = historical_df['ma'].iloc[-1]
    
    # Generate forecast dates
    last_date = pd.to_datetime(historical_df['date'].iloc[-1])
    forecast_dates = pd.date_range(start=last_date + timedelta(days=1), periods=days_ahead, freq='D')
    
    forecast_data = [{
        'date': date,
        'quantity': max(0.0, last_ma),
        'lower': max(0.0, last_ma * 0.8),
        'upper': max(0.0, last_ma * 1.2),
    } for date in forecast_dates]
    
    historical_data = historical_df[['date', 'quantity']].to_dict('records')
    
    return {
        "historical": historical_data,
        "forecast": forecast_data,
        "model": "Moving average (7-day)",
        "days_ahead": days_ahead,
    }


def _seasonal_naive_result(historical_df: pd.DataFrame, days_ahead: int) -> dict:
    y = historical_df["quantity"]
    last_date = pd.to_datetime(historical_df["date"].iloc[-1])
    preds = _seasonal_naive_forecast(y, days_ahead)
    dates = pd.date_range(start=last_date + timedelta(days=1), periods=days_ahead, freq="D")
    forecast_data = [
        {
            "date": date,
            "quantity": max(0.0, float(val)),
            "lower": max(0.0, float(val) * 0.8),
            "upper": max(0.0, float(val) * 1.2),
        }
        for date, val in zip(dates, preds)
    ]
    return {
        "historical": historical_df[["date", "quantity"]].to_dict("records"),
        "forecast": forecast_data,
        "model": "Seasonal naive (7-day)",
        "days_ahead": days_ahead,
    }


def _season_name(month: int) -> str:
    if month in (3, 4, 5):
        return "Spring"
    if month in (6, 7, 8):
        return "Summer"
    if month in (9, 10, 11):
        return "Fall"
    return "Winter"


def _rows_to_daily(rows: list, value_col: str = "quantity") -> pd.Series:
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    if "date" not in df.columns or value_col not in df.columns:
        return pd.Series(dtype=float)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["date"])
    df[value_col] = pd.to_numeric(df[value_col], errors="coerce").fillna(0.0)
    return df.groupby("date")[value_col].sum()


def _prior_calendar_day(ts: pd.Timestamp) -> pd.Timestamp:
    try:
        return ts.replace(year=ts.year - 1)
    except ValueError:
        return ts.replace(year=ts.year - 1, day=28)


def _fmt_yoy_label(pct: float | None) -> str | None:
    if pct is None:
        return None
    return f"{pct:+.0%}"


def _peak_callout(labels: list, forecast: list, actual: list, months: list[int]) -> dict | None:
    scored = [(i, v) for i, v in enumerate(forecast) if v is not None]
    if not scored:
        return None
    idx, value = max(scored, key=lambda item: item[1])
    baseline_src = [v for _, v in scored]
    baseline = sum(baseline_src) / len(baseline_src) if baseline_src else 0.0
    vs_baseline = ((value - baseline) / baseline) if baseline else 0.0
    month = months[idx] if idx < len(months) else idx + 1
    season = _season_name(month)
    if vs_baseline >= 0.12:
        label = f"{season} peak ({vs_baseline:+.0%} vs. baseline)"
    else:
        label = f"Peak · {labels[idx]}"
    return {
        "index": idx,
        "month": labels[idx],
        "label": label,
        "value": round(float(value), 1),
        "vs_baseline": round(float(vs_baseline), 4),
    }


def _yearly_naive_forecast(historical_df: pd.DataFrame, days_ahead: int) -> pd.Series:
    """Same calendar day last year — the usual seasonal baseline companies start from."""
    work = historical_df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.normalize()
    work = work.dropna(subset=["date"])
    hist = work.groupby("date")["quantity"].sum()
    last = hist.index.max()
    dates = pd.date_range(last + timedelta(days=1), periods=days_ahead, freq="D")
    values = []
    for ts in dates:
        prior = _prior_calendar_day(pd.Timestamp(ts))
        while prior > last:
            prior = _prior_calendar_day(prior)
        val = hist.get(prior)
        if val is None:
            same_doy = hist[hist.index.dayofyear == ts.dayofyear]
            val = float(same_doy.iloc[-1]) if len(same_doy) else float(hist.iloc[-1]) if len(hist) else 0.0
        values.append(max(0.0, float(val)))
    return pd.Series(values, index=dates)


def _yearly_naive_result(historical_df: pd.DataFrame, days_ahead: int) -> dict:
    preds = _yearly_naive_forecast(historical_df, days_ahead)
    # No invented confidence band — replay is exact history, so lower = upper = value.
    forecast_data = [
        {
            "date": date,
            "quantity": float(val),
            "lower": float(val),
            "upper": float(val),
        }
        for date, val in preds.items()
    ]
    return {
        "historical": historical_df[["date", "quantity"]].to_dict("records"),
        "forecast": forecast_data,
        "model": "Seasonal naive (yearly)",
        "days_ahead": days_ahead,
    }


def _backtest_yearly_naive(historical_df: pd.DataFrame, holdout: int, monthly: bool = False) -> float | None:
    if holdout <= 0 or len(historical_df) <= holdout + 300:
        return None
    work = historical_df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.normalize()
    y = work.groupby("date")["quantity"].sum().sort_index()
    actual = y.iloc[-holdout:]
    preds = []
    for ts in actual.index:
        prior = _prior_calendar_day(pd.Timestamp(ts))
        preds.append(float(y.get(prior, y.iloc[0])))
    pred_s = pd.Series(preds, index=actual.index)
    if monthly:
        return _monthly_wape(actual, pred_s)
    return _wape(actual.reset_index(drop=True), pred_s.reset_index(drop=True))


def _daily_quantity(historical_df: pd.DataFrame) -> pd.Series:
    work = historical_df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.normalize()
    return work.groupby("date")["quantity"].sum().sort_index()


def _ets_monthly_forecast(train_daily: pd.Series, periods: int) -> pd.Series | None:
    monthly = train_daily.resample("MS").sum().astype(float)
    if len(monthly) < 6 or periods < 1:
        return None
    try:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing
    except ImportError:
        return None
    # Need ≥2 full seasons for monthly seasonality; otherwise trend-only.
    use_seasonal = len(monthly) >= 24
    try:
        model = ExponentialSmoothing(
            monthly,
            trend="add",
            damped_trend=True,
            seasonal="add" if use_seasonal else None,
            seasonal_periods=12 if use_seasonal else None,
            initialization_method="estimated",
        )
        fitted = model.fit(optimized=True, use_brute=True)
        fc = fitted.forecast(periods)
    except Exception:
        return None
    fc.index = pd.date_range(
        monthly.index.max() + pd.offsets.MonthBegin(1),
        periods=len(fc),
        freq="MS",
    )
    return fc.clip(lower=0)


def _expand_monthly_to_daily(monthly_fc: pd.Series, start, days_ahead: int) -> pd.Series:
    dates = pd.date_range(pd.Timestamp(start).normalize(), periods=days_ahead, freq="D")
    lookup = {pd.Timestamp(idx).to_period("M"): float(val) for idx, val in monthly_fc.items()}
    values = []
    for ts in dates:
        month_qty = lookup.get(ts.to_period("M"))
        if month_qty is None:
            # Fall forward to nearest known forecast month if horizon exceeds fit.
            keys = sorted(lookup.keys())
            if not keys:
                values.append(0.0)
            else:
                month_qty = lookup[keys[-1]]
                values.append(max(0.0, month_qty / float(ts.days_in_month)))
        else:
            values.append(max(0.0, month_qty / float(ts.days_in_month)))
    return pd.Series(values, index=dates)


def _ets_result(historical_df: pd.DataFrame, days_ahead: int) -> dict:
    daily = _daily_quantity(historical_df)
    last = daily.index.max()
    start = last + timedelta(days=1)
    end = start + timedelta(days=days_ahead - 1)
    months = (end.to_period("M") - start.to_period("M")).n + 1
    months = max(2, int(months))
    monthly_fc = _ets_monthly_forecast(daily, months)
    if monthly_fc is None:
        return {"error": "ETS needs more monthly history", "forecast": []}
    preds = _expand_monthly_to_daily(monthly_fc, start, days_ahead)
    # Interval from residual scale on recent months.
    hist_m = daily.resample("MS").sum()
    resid = None
    if len(hist_m) >= 6:
        try:
            from statsmodels.tsa.holtwinters import ExponentialSmoothing
            use_seasonal = len(hist_m) >= 24
            fit = ExponentialSmoothing(
                hist_m.astype(float),
                trend="add",
                damped_trend=True,
                seasonal="add" if use_seasonal else None,
                seasonal_periods=12 if use_seasonal else None,
                initialization_method="estimated",
            ).fit(optimized=True, use_brute=True)
            resid = float((hist_m - fit.fittedvalues).std())
        except Exception:
            resid = None
    sigma = resid if resid and resid > 0 else None
    forecast_data = []
    for date, val in preds.items():
        v = float(val)
        if sigma is None:
            lo, hi = v * 0.85, v * 1.15
        else:
            # Daily share of monthly residual band.
            band = sigma / max(1, date.days_in_month)
            lo, hi = v - 1.28 * band, v + 1.28 * band
        forecast_data.append({
            "date": date,
            "quantity": max(0.0, v),
            "lower": max(0.0, lo),
            "upper": max(max(0.0, v), hi),
        })
    return {
        "historical": historical_df[["date", "quantity"]].to_dict("records"),
        "forecast": forecast_data,
        "model": "ETS (Holt-Winters)",
        "days_ahead": days_ahead,
    }


def _backtest_ets(historical_df: pd.DataFrame, holdout: int) -> float | None:
    """Score ETS on monthly totals inside the holdout window (production metric)."""
    if holdout <= 0 or len(historical_df) < holdout + 90:
        return None
    daily = _daily_quantity(historical_df)
    monthly = daily.resample("MS").sum()
    if len(monthly) < 8:
        return None
    holdout_m = max(2, len(daily.iloc[-holdout:].resample("MS").sum()))
    if len(monthly) <= holdout_m + 4:
        return None
    train_m = monthly.iloc[:-holdout_m]
    actual_m = monthly.iloc[-holdout_m:]
    # Fit on daily truncated to train end for shared helper.
    train_daily = daily[daily.index <= train_m.index.max() + pd.offsets.MonthEnd(0)]
    monthly_fc = _ets_monthly_forecast(train_daily, holdout_m)
    if monthly_fc is None or len(monthly_fc) < 1:
        return None
    pred = monthly_fc.iloc[: len(actual_m)]
    pred.index = actual_m.index[: len(pred)]
    return _wape(actual_m.iloc[: len(pred)], pred)


def _monthly_wape(actual_daily: pd.Series, pred_daily: pd.Series) -> float | None:
    actual_m = actual_daily.groupby(actual_daily.index.to_period("M")).sum()
    pred_m = pred_daily.groupby(pred_daily.index.to_period("M")).sum()
    idx = actual_m.index.intersection(pred_m.index)
    if len(idx) < 1:
        return None
    return _wape(actual_m.loc[idx], pred_m.loc[idx])


def _backtest_prophet(df: pd.DataFrame, holdout: int, monthly: bool = False) -> float | None:
    train = df.iloc[:-holdout].copy()
    if len(train) < 14:
        return None
    try:
        from prophet import Prophet
    except ImportError:
        return None
    prophet_df = train.rename(columns={"date": "ds", "quantity": "y"})
    prophet_df["ds"] = pd.to_datetime(prophet_df["ds"])
    model = Prophet(
        daily_seasonality=False,
        weekly_seasonality=True,
        yearly_seasonality=len(prophet_df) >= 365,
        changepoint_prior_scale=0.05,
    )
    model.fit(prophet_df)
    future = model.make_future_dataframe(periods=holdout)
    predicted = model.predict(future).tail(holdout)
    predicted["ds"] = pd.to_datetime(predicted["ds"]).dt.normalize()
    actual = df.iloc[-holdout:].copy()
    actual["date"] = pd.to_datetime(actual["date"]).dt.normalize()
    if monthly:
        pred_s = predicted.set_index("ds")["yhat"]
        act_s = actual.set_index("date")["quantity"]
        return _monthly_wape(act_s, pred_s)
    return _wape(actual["quantity"].reset_index(drop=True), predicted["yhat"].reset_index(drop=True))


def _prediction_model_label(model: str | None) -> str:
    names = {
        "Seasonal naive (7-day)": "Baseline (last week)",
        "Seasonal naive (yearly)": "Baseline (last year)",
        "Moving average (7-day)": "Moving average",
        "Prophet": "Prophet",
        "ETS (Holt-Winters)": "ETS",
    }
    if not model:
        return "Forecast"
    return names.get(model, model)


def _honest_method_note(model: str | None) -> str:
    if model and "Seasonal naive (yearly)" in model:
        return "Baseline only: copies the same calendar period from last year (no growth)."
    if model and "Seasonal naive (7-day)" in model:
        return "Baseline only: repeats the last 7 days."
    if model and model.startswith("Prophet"):
        return "Prophet fit on sold units (trend + seasonality). Gray line is last year for comparison."
    if model and model.startswith("ETS"):
        return "ETS (Holt-Winters) fit on monthly sold units. Gray line is last year for comparison."
    if model and "Moving average" in model:
        return "Flat recent average — short-horizon only."
    return "Built from sold history in DuckDB only."


def explain_forecast_basis(
    historical: list,
    labels: list,
    actual: list,
    forecast_vals: list,
    yoy: list,
    model: str | None,
    selection: str | None = None,
    wape: float | None = None,
) -> dict:
    """Data-only rationale: why the forecast is up/down vs last year.

    Uses sold history + this forecast's YoY table. No canned business story.
    """
    basis = []
    drivers = []

    model_label = _prediction_model_label(model)
    if model and "Seasonal naive (yearly)" in model:
        basis.append(
            f"Method: {model_label} — each forecast month equals the same calendar month in the sold history."
        )
    elif model and model.startswith("Prophet"):
        basis.append(
            f"Method: {model_label} — fitted trend + yearly/weekly seasonality on daily sold units."
        )
    elif model and model.startswith("ETS"):
        basis.append(
            f"Method: {model_label} — Holt-Winters level/trend/seasonality on monthly sold totals."
        )
    elif model and "Moving average" in model:
        basis.append(f"Method: {model_label} — recent average held flat.")
    else:
        basis.append(f"Method: {model_label}.")

    if selection:
        bit = selection
        if wape is not None:
            bit = f"{selection}; holdout WAPE {wape:.0%}"
        basis.append(f"Selection: {bit}.")

    hist = pd.DataFrame(historical or [])
    if not hist.empty and "date" in hist.columns and "quantity" in hist.columns:
        hist["date"] = pd.to_datetime(hist["date"], errors="coerce")
        hist = hist.dropna(subset=["date"])
        hist["quantity"] = pd.to_numeric(hist["quantity"], errors="coerce").fillna(0.0)
        monthly = hist.set_index("date")["quantity"].resample("MS").sum()
        if len(monthly) >= 3:
            peak_i = monthly.idxmax()
            trough_i = monthly.idxmin()
            basis.append(
                f"In history, strongest month was {peak_i.strftime('%b %Y')} "
                f"({float(monthly.max()):,.0f} units); weakest was "
                f"{trough_i.strftime('%b %Y')} ({float(monthly.min()):,.0f} units)."
            )

            # Same-calendar-month YoY where both years exist
            yoy_hist = []
            for ts, val in monthly.items():
                prior = ts - pd.DateOffset(years=1)
                if prior in monthly.index and monthly[prior] > 0:
                    yoy_hist.append((ts, (float(val) - float(monthly[prior])) / float(monthly[prior])))
            if yoy_hist:
                recent = yoy_hist[-6:] if len(yoy_hist) >= 2 else yoy_hist
                avg_hist_yoy = sum(p for _, p in recent) / len(recent)
                up = sum(1 for _, p in recent if p > 0.02)
                down = sum(1 for _, p in recent if p < -0.02)
                direction = (
                    "rose" if avg_hist_yoy > 0.02
                    else "fell" if avg_hist_yoy < -0.02
                    else "was roughly flat"
                )
                drivers.append(
                    f"Recent history {direction} vs the year before "
                    f"(avg YoY {avg_hist_yoy:+.0%} over the last {len(recent)} comparable months"
                    f"; {up} up, {down} down)."
                )

            if len(monthly) >= 8:
                half = max(3, len(monthly) // 3)
                early = float(monthly.iloc[:half].mean())
                late = float(monthly.iloc[-half:].mean())
                if early > 0:
                    slope = (late - early) / early
                    if abs(slope) >= 0.03:
                        drivers.append(
                            f"Level shift in the sold series: last {half} months average "
                            f"{late:,.0f} vs first {half} months {early:,.0f} ({slope:+.0%})."
                        )

    pairs = [
        (labels[i], actual[i], forecast_vals[i], yoy[i])
        for i in range(min(len(labels), len(forecast_vals)))
        if i < len(actual) and forecast_vals[i] is not None
    ]
    yoy_pairs = [(lab, a, f, y) for lab, a, f, y in pairs if y is not None and a not in (None, 0)]
    if yoy_pairs:
        avg_yoy = sum(y for _, _, _, y in yoy_pairs) / len(yoy_pairs)
        if avg_yoy > 0.02:
            drivers.append(
                f"Overall the forecast is above last year (avg YoY {avg_yoy:+.0%} across {len(yoy_pairs)} periods)."
            )
        elif avg_yoy < -0.02:
            drivers.append(
                f"Overall the forecast is below last year (avg YoY {avg_yoy:+.0%} across {len(yoy_pairs)} periods)."
            )
        else:
            drivers.append(
                f"Overall the forecast is close to last year (avg YoY {avg_yoy:+.0%} across {len(yoy_pairs)} periods)."
            )

        ranked = sorted(yoy_pairs, key=lambda row: row[3])
        low_lab, low_a, low_f, low_y = ranked[0]
        high_lab, high_a, high_f, high_y = ranked[-1]
        if low_y <= -0.03:
            drivers.append(
                f"Largest drop vs last year: {low_lab} "
                f"({low_a:,.0f} to {low_f:,.0f}, {low_y:+.0%})."
            )
        if high_y >= 0.03:
            drivers.append(
                f"Largest rise vs last year: {high_lab} "
                f"({high_a:,.0f} to {high_f:,.0f}, {high_y:+.0%})."
            )

        # Within-forecast shape
        fc_only = [(lab, f) for lab, _, f, _ in pairs if f is not None]
        if len(fc_only) >= 3:
            peak_lab, peak_v = max(fc_only, key=lambda x: x[1])
            trough_lab, trough_v = min(fc_only, key=lambda x: x[1])
            drivers.append(
                f"Inside the forecast window, high is {peak_lab} ({peak_v:,.0f} units) "
                f"and low is {trough_lab} ({trough_v:,.0f} units) — shape comes from the fitted seasonality on history."
            )

    if not drivers:
        drivers.append("Not enough overlapping history to explain YoY moves beyond the model fit.")

    return {
        "basis": basis,
        "drivers": drivers,
        "insights": basis + drivers,
    }


def build_demand_planning_view(
    historical: list,
    forecast: list,
    window_start,
    window_end,
    grain: str = "month",
    model: str | None = None,
    selection: str | None = None,
    wape: float | None = None,
) -> dict:
    """Year-over-year planning chart: prior-year actual vs forecast on the same calendar months.

    Matches the Dynamics 365 Demand Planning sample: one forecast line, a confidence
    band, prior-year actuals, a peak callout, and a monthly Actual / Forecast / YoY table.
    """
    start = pd.Timestamp(window_start).normalize()
    end = pd.Timestamp(window_end).normalize()
    if end < start:
        start, end = end, start

    hist_d = _rows_to_daily(historical, "quantity")
    fc_d = _rows_to_daily(forecast, "quantity")
    lo_d = _rows_to_daily(forecast, "lower")
    hi_d = _rows_to_daily(forecast, "upper")

    use_month = grain == "month" or (end - start).days > 90

    if use_month:
        raw_periods = list(pd.period_range(start.to_period("M"), end.to_period("M"), freq="M"))

        def _month_window(period):
            month_start = period.to_timestamp().normalize()
            month_end = (period + 1).to_timestamp().normalize() - pd.Timedelta(days=1)
            a = max(start, month_start)
            b = min(end, month_end)
            if b < a:
                return None
            return a, b

        covered = [(p, w) for p in raw_periods if (w := _month_window(p))]
        if len(covered) >= 3:
            covered = [(p, w) for p, w in covered if (w[1] - w[0]).days + 1 >= 20]
        if not covered:
            covered = [(p, w) for p in raw_periods if (w := _month_window(p))]

        periods = [p for p, _ in covered]
        actual, forecast_vals, lower, upper, yoy, months = [], [], [], [], [], []
        for period, (win_a, win_b) in covered:
            prior_a = _prior_calendar_day(win_a)
            prior_b = _prior_calendar_day(win_b)
            hist_slice = hist_d[(hist_d.index >= prior_a) & (hist_d.index <= prior_b)] if len(hist_d) else pd.Series(dtype=float)
            fc_slice = fc_d[(fc_d.index >= win_a) & (fc_d.index <= win_b)] if len(fc_d) else pd.Series(dtype=float)
            lo_slice = lo_d[(lo_d.index >= win_a) & (lo_d.index <= win_b)] if len(lo_d) else pd.Series(dtype=float)
            hi_slice = hi_d[(hi_d.index >= win_a) & (hi_d.index <= win_b)] if len(hi_d) else pd.Series(dtype=float)

            act = float(hist_slice.sum()) if len(hist_slice) else None
            qty = float(fc_slice.sum()) if len(fc_slice) else None
            lo = float(lo_slice.sum()) if len(lo_slice) else (qty * 0.8 if qty is not None else None)
            hi = float(hi_slice.sum()) if len(hi_slice) else (qty * 1.2 if qty is not None else None)
            if qty is not None:
                lo = max(0.0, lo if lo is not None else qty * 0.8)
                hi = max(qty, hi if hi is not None else qty * 1.2)

            pct = None
            if act not in (None, 0) and qty is not None:
                pct = (qty - act) / act

            actual.append(None if act is None else round(act, 1))
            forecast_vals.append(None if qty is None else round(qty, 1))
            lower.append(None if lo is None else round(lo, 1))
            upper.append(None if hi is None else round(hi, 1))
            yoy.append(None if pct is None else round(pct, 4))
            months.append(int(period.month))

        same_year = len({p.year for p in periods}) == 1 if periods else False
        labels = [p.strftime("%b") if same_year else p.strftime("%b %Y") for p in periods]
        actual_years = {(p - 12).year for p in periods}
        forecast_years = {p.year for p in periods}
        grain_out = "month"
        y_title = "Units per month"
    else:
        days = pd.date_range(start, end, freq="D")
        actual, forecast_vals, lower, upper, yoy, months = [], [], [], [], [], []
        for ts in days:
            prior = _prior_calendar_day(ts)
            act = float(hist_d.get(prior)) if prior in hist_d.index else None
            qty = float(fc_d.get(ts)) if ts in fc_d.index else None
            lo = float(lo_d.get(ts)) if ts in lo_d.index else (qty * 0.8 if qty is not None else None)
            hi = float(hi_d.get(ts)) if ts in hi_d.index else (qty * 1.2 if qty is not None else None)
            if qty is not None:
                lo = max(0.0, lo if lo is not None else qty * 0.8)
                hi = max(qty, hi if hi is not None else qty * 1.2)
            pct = None
            if act not in (None, 0) and qty is not None:
                pct = (qty - act) / act
            actual.append(None if act is None else round(act, 1))
            forecast_vals.append(None if qty is None else round(qty, 1))
            lower.append(None if lo is None else round(lo, 1))
            upper.append(None if hi is None else round(hi, 1))
            yoy.append(None if pct is None else round(pct, 4))
            months.append(int(ts.month))
        labels = [ts.strftime("%b %d") for ts in days]
        actual_years = {ts.year - 1 for ts in days}
        forecast_years = {ts.year for ts in days}
        grain_out = "day"
        y_title = "Units per day"
        periods = None

    actual_year = next(iter(actual_years)) if len(actual_years) == 1 else None
    forecast_year = next(iter(forecast_years)) if len(forecast_years) == 1 else None
    actual_label = f"Actual demand — {actual_year}" if actual_year else "Actual demand — prior year"
    model_label = _prediction_model_label(model)
    forecast_label = f"{model_label} — {forecast_year}" if forecast_year else model_label
    table_actual = f"Actual {actual_year}" if actual_year else "Actual (prior year)"
    table_forecast = f"Forecast {forecast_year}" if forecast_year else "Forecast"

    peak = _peak_callout(labels, forecast_vals, actual, months)
    yoy_labels = [_fmt_yoy_label(v) for v in yoy]
    honesty = _honest_method_note(model)
    is_naive_year = bool(model and "Seasonal naive (yearly)" in model)

    rationale = explain_forecast_basis(
        historical=historical,
        labels=labels,
        actual=actual,
        forecast_vals=forecast_vals,
        yoy=yoy,
        model=model,
        selection=selection,
        wape=wape,
    )
    insights = rationale["insights"]
    if is_naive_year:
        insights.append(
            "Gray and blue match with +0% YoY when the method is last-year copy — that is the model, not an error."
        )

    return {
        "style": "demand_planning",
        "labels": labels,
        "historical": actual,
        "actual": actual,
        "forecast": forecast_vals,
        "lower": lower,
        "upper": upper,
        "yoy": yoy,
        "yoy_labels": yoy_labels,
        "grain": grain_out,
        "y_title": y_title,
        "actual_year": actual_year,
        "forecast_year": forecast_year,
        "actual_label": actual_label,
        "forecast_label": forecast_label,
        "peak": None if is_naive_year else peak,
        "insights": insights,
        "basis": rationale.get("basis") or [],
        "drivers": rationale.get("drivers") or [],
        "honesty_note": honesty,
        "honest_replay": is_naive_year,
        "planning_table": {
            "columns": labels,
            "actual_label": table_actual,
            "forecast_label": (
                f"Same period last year → {forecast_year}"
                if is_naive_year and forecast_year
                else table_forecast
            ),
            "actual": actual,
            "forecast": forecast_vals,
            "yoy": yoy_labels,
        },
    }


def build_monthly_chart(
    result: dict,
    history_months: int | None = None,
    forecast_months: int | None = None,
) -> dict:
    """Roll daily series up to months. Length follows the data, not a fixed 12-month window."""
    hist = pd.DataFrame(result.get("historical") or [])
    fc = pd.DataFrame(result.get("forecast") or [])

    def monthly_sum(df: pd.DataFrame, value_col: str) -> pd.Series:
        if df is None or df.empty or value_col not in df.columns:
            return pd.Series(dtype=float)
        work = df.copy()
        work["date"] = pd.to_datetime(work["date"], errors="coerce")
        work = work.dropna(subset=["date"])
        work[value_col] = pd.to_numeric(work[value_col], errors="coerce").fillna(0.0)
        work["month"] = work["date"].dt.to_period("M")
        return work.groupby("month")[value_col].sum()

    hist_m = monthly_sum(hist, "quantity")
    fc_m = monthly_sum(fc, "quantity")
    lo_m = monthly_sum(fc, "lower") if not fc.empty and "lower" in fc.columns else pd.Series(dtype=float)
    hi_m = monthly_sum(fc, "upper") if not fc.empty and "upper" in fc.columns else pd.Series(dtype=float)

    hist_count = history_months if history_months is not None else (len(hist_m) if len(hist_m) else 0)
    hist_index = (
        pd.period_range(end=hist_m.index.max(), periods=hist_count, freq="M")
        if hist_count
        else pd.PeriodIndex([], freq="M")
    )
    if len(fc_m):
        start = hist_index.max() + 1 if len(hist_index) else fc_m.index.min()
        fc_count = forecast_months if forecast_months is not None else max(1, len(fc_m))
        fc_index = pd.period_range(start=start, periods=fc_count, freq="M")
    else:
        fc_index = pd.PeriodIndex([], freq="M")

    labels = [str(p) for p in list(hist_index) + list(fc_index)]
    historical = [float(hist_m.get(p, 0.0)) for p in hist_index] + [None] * len(fc_index)
    forecast = [None] * len(hist_index) + [float(fc_m.get(p, 0.0)) for p in fc_index]
    lower = [None] * len(hist_index)
    upper = [None] * len(hist_index)
    for p in fc_index:
        qty = float(fc_m.get(p, 0.0))
        lo = float(lo_m.get(p, max(0.0, qty * 0.8))) if p in lo_m.index else max(0.0, qty * 0.8)
        hi = float(hi_m.get(p, qty * 1.2)) if p in hi_m.index else qty * 1.2
        lower.append(max(0.0, lo))
        upper.append(max(qty, hi))

    return {
        "labels": labels,
        "historical": historical,
        "forecast": forecast,
        "lower": lower,
        "upper": upper,
        "grain": "month",
        "history_months": int(len(hist_index)),
        "forecast_months": int(len(fc_index)),
    }


FORECAST_MODEL_IDS = {
    "Seasonal naive (7-day)": "seasonal_naive",
    "Seasonal naive (yearly)": "seasonal_naive_yearly",
    "Moving average (7-day)": "moving_average",
    "Prophet": "prophet",
    "ETS (Holt-Winters)": "ets",
}


def generate_forecast(
    days_back: int | None = None,
    days_ahead: int = 365,
    use_prophet: bool = True,
    horizon: str = "day",
    item_number: str | None = None,
) -> dict:
    """Production forecast: D365-style best fit on sold history.

    With ≥12 months of history, the auto-selected forecast is the best of
    Prophet / ETS by monthly WAPE. Seasonal naive is kept as a baseline card
    only (Microsoft treats naive as a low-data fallback, not the main model).
    Short horizons still compare weekly naive / moving average / Prophet.
    """
    historical_df = get_historical_sales(days_back=days_back, item_number=item_number)

    if len(historical_df) == 0 or float(historical_df["quantity"].sum()) <= 0:
        scope = f"item {item_number}" if item_number else "the sales history"
        return {
            "error": f"No sold units found for {scope}",
            "historical": [],
            "forecast": [],
            "item_number": item_number,
        }

    y = historical_df["quantity"]
    long_range = horizon == "month" or days_ahead > 90
    n = len(historical_df)
    month_count = int(
        pd.to_datetime(historical_df["date"]).dt.to_period("M").nunique()
    )
    # D365-style: <12 months ≈ low-data → naive fallback; otherwise best-fit models.
    low_data = month_count < 12
    holdout = min(180, max(60, n // 5)) if long_range else _holdout_size(n)
    if holdout >= n - 14:
        holdout = 0

    candidates = []
    eligible_names: set[str] = set()

    if long_range:
        yearly_wape = _backtest_yearly_naive(historical_df, holdout, monthly=True) if holdout else None
        candidates.append({
            "id": "seasonal_naive_yearly",
            "name": "Seasonal naive (yearly)",
            "wape": yearly_wape,
            "baseline": True,
        })
        if low_data:
            eligible_names.add("Seasonal naive (yearly)")

        ets_wape = None
        if holdout and not low_data:
            try:
                ets_wape = _backtest_ets(historical_df, holdout)
            except Exception as e:
                print(f"ETS backtest failed: {e}")
        candidates.append({
            "id": "ets",
            "name": "ETS (Holt-Winters)",
            "wape": ets_wape,
            "baseline": False,
        })
        if not low_data:
            eligible_names.add("ETS (Holt-Winters)")

        prophet_wape = None
        if use_prophet and holdout and not low_data:
            try:
                prophet_wape = _backtest_prophet(historical_df, holdout, monthly=True)
            except Exception as e:
                print(f"Prophet backtest failed: {e}")
        candidates.append({
            "id": "prophet",
            "name": "Prophet",
            "wape": prophet_wape,
            "baseline": False,
        })
        if use_prophet and not low_data:
            eligible_names.add("Prophet")

        builders = {
            "Prophet": lambda: forecast_with_prophet(historical_df, days_ahead=days_ahead),
            "Seasonal naive (yearly)": lambda: _yearly_naive_result(historical_df, days_ahead),
            "ETS (Holt-Winters)": lambda: _ets_result(historical_df, days_ahead),
        }
        default_winner = (
            "Seasonal naive (yearly)" if low_data
            else ("Prophet" if use_prophet else "ETS (Holt-Winters)")
        )
        default_reason = (
            f"low data ({month_count} months < 12); baseline last-year copy"
            if low_data
            else "not enough holdout to score; defaulted to Prophet"
        )
        grain_note = f"best fit (monthly WAPE, last {holdout} days)"
    else:
        naive_wape = _backtest_seasonal_naive(y, holdout) if holdout else None
        candidates.append({
            "id": "seasonal_naive",
            "name": "Seasonal naive (7-day)",
            "wape": naive_wape,
            "baseline": True,
        })
        ma_wape = _backtest_moving_average(y, holdout) if holdout else None
        candidates.append({
            "id": "moving_average",
            "name": "Moving average (7-day)",
            "wape": ma_wape,
            "baseline": False,
        })
        eligible_names.add("Moving average (7-day)")
        prophet_wape = None
        if use_prophet and holdout:
            try:
                prophet_wape = _backtest_prophet(historical_df, holdout, monthly=False)
            except Exception as e:
                print(f"Prophet backtest failed: {e}")
        candidates.append({
            "id": "prophet",
            "name": "Prophet",
            "wape": prophet_wape,
            "baseline": False,
        })
        if use_prophet:
            eligible_names.add("Prophet")
        builders = {
            "Prophet": lambda: forecast_with_prophet(historical_df, days_ahead=days_ahead),
            "Moving average (7-day)": lambda: forecast_simple_moving_average(historical_df, days_ahead=days_ahead),
            "Seasonal naive (7-day)": lambda: _seasonal_naive_result(historical_df, days_ahead=days_ahead),
        }
        default_winner = "Prophet" if use_prophet else "Moving average (7-day)"
        default_reason = "not enough history to backtest; defaulted to short-horizon model"
        grain_note = f"best fit (WAPE, last {holdout} days)"

    scored = [
        c for c in candidates
        if c["wape"] is not None and c["name"] in eligible_names
    ]
    if scored:
        winner = min(scored, key=lambda c: c["wape"])["name"]
        selection = grain_note
    elif long_range and low_data:
        winner = "Seasonal naive (yearly)"
        selection = default_reason
    elif long_range and use_prophet and n >= 14:
        winner = "Prophet"
        selection = default_reason
    elif long_range:
        winner = "ETS (Holt-Winters)"
        selection = default_reason
    elif use_prophet and n >= 14:
        winner = "Prophet"
        selection = "not enough history to backtest; defaulted to Prophet"
    else:
        winner = default_winner
        selection = default_reason

    model_forecasts = {}
    for name, builder in builders.items():
        if name == "Prophet" and not use_prophet:
            continue
        try:
            built = builder()
            if built.get("error"):
                print(f"{name} forecast skipped: {built['error']}")
                continue
            model_forecasts[name] = _clip_non_negative(built.get("forecast") or [])
        except Exception as e:
            print(f"{name} forecast failed: {e}")

    fallbacks = (
        ["Prophet", "ETS (Holt-Winters)", "Seasonal naive (yearly)"]
        if long_range and not low_data
        else (
            ["Seasonal naive (yearly)", "Prophet", "ETS (Holt-Winters)"]
            if long_range
            else ["Prophet", "Moving average (7-day)", "Seasonal naive (7-day)"]
        )
    )
    if winner not in model_forecasts:
        for name in fallbacks:
            if name in model_forecasts:
                winner = name
                selection = f"selected model failed; fell back to {winner}"
                break
        else:
            return {
                "error": "All forecast models failed",
                "historical": historical_df[["date", "quantity"]].to_dict("records"),
                "forecast": [],
            }

    historical = historical_df[["date", "quantity"]].to_dict("records")
    hist_start = pd.to_datetime(historical_df["date"].min()).date()
    hist_end = pd.to_datetime(historical_df["date"].max()).date()
    return {
        "historical": historical,
        "forecast": model_forecasts[winner],
        "model": winner,
        "model_forecasts": model_forecasts,
        "candidates": candidates,
        "selection": selection,
        "holdout_days": holdout,
        "horizon": "month" if long_range else "day",
        "low_data": low_data,
        "history_months": month_count,
        "history_start": hist_start.isoformat(),
        "history_end": hist_end.isoformat(),
        "history_days": int(n),
        "item_number": item_number,
    }


if __name__ == "__main__":
    # Test forecast
    print("Testing forecast generation...")
    result = generate_forecast(days_back=180, days_ahead=365)
    
    if "error" in result:
        print(f"Error: {result['error']}")
    else:
        print(f"Model: {result['model']}")
        print(f"Historical data points: {len(result['historical'])}")
        print(f"Forecast data points: {len(result['forecast'])}")
        print(f"\nFirst 3 forecast days:")
        for row in result['forecast'][:3]:
            print(f"  {row['date']}: {row['quantity']:.1f} (±{row.get('lower', 0):.1f} to {row.get('upper', 0):.1f})")
