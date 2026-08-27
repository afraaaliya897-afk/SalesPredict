"""
Forecasting service integrated into chat.

Uses Prophet for time series forecasting with automatic seasonality detection.
"""

import pandas as pd
import duckdb
from datetime import timedelta
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent / "sales_inventory.duckdb")

def get_historical_sales(days_back: int = 365, db_path: str = DB_PATH) -> pd.DataFrame:
    """Daily sold units, using the data's latest physical date (not wall-clock today)."""
    con = duckdb.connect(db_path, read_only=True)
    try:
        (max_date,) = con.execute(
            "SELECT MAX(physical_date) FROM inventory_transaction WHERE reference = 'Sales order'"
        ).fetchone()
        if max_date is None:
            return pd.DataFrame(columns=["date", "quantity"])
        cutoff_date = pd.Timestamp(max_date) - timedelta(days=days_back)
        sql = """
            SELECT
                CAST(it.physical_date AS DATE) as date,
                SUM(-it.quantity) as quantity
            FROM inventory_transaction it
            LEFT JOIN sales_order so ON it.number = so.sales_order_number
            WHERE it.reference = 'Sales order'
              AND it.issue = 'Sold'
              AND it.quantity < 0
              AND (
                    so.sales_order_number IS NULL
                    OR (
                        so.status NOT IN ('Canceled', 'Cancelled')
                        AND so.do_not_process != 'Yes'
                    )
                  )
              AND it.physical_date >= ?
            GROUP BY CAST(it.physical_date AS DATE)
            ORDER BY date ASC
        """
        df = con.execute(sql, [cutoff_date]).df()

        if len(df) > 0:
            date_range = pd.date_range(start=df["date"].min(), end=df["date"].max(), freq="D")
            df = df.set_index("date").reindex(date_range, fill_value=0).reset_index()
            df.columns = ["date", "quantity"]

        return df
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


def _backtest_prophet(df: pd.DataFrame, holdout: int) -> float | None:
    train = df.iloc[:-holdout].copy()
    if len(train) < 14:
        return None
    try:
        from prophet import Prophet
    except ImportError:
        return None
    prophet_df = train.rename(columns={"date": "ds", "quantity": "y"})
    model = Prophet(
        daily_seasonality=False,
        weekly_seasonality=True,
        yearly_seasonality=len(prophet_df) >= 365,
        changepoint_prior_scale=0.05,
    )
    model.fit(prophet_df)
    future = model.make_future_dataframe(periods=holdout)
    predicted = model.predict(future).tail(holdout)["yhat"]
    actual = df.iloc[-holdout:]["quantity"].reset_index(drop=True)
    return _wape(actual, predicted.reset_index(drop=True))


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


def build_monthly_chart(
    result: dict,
    history_months: int = 12,
    forecast_months: int = 12,
) -> dict:
    """Last N months of actuals + next N months of forecast. Does not mix grains."""
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

    hist_index = (
        pd.period_range(end=hist_m.index.max(), periods=history_months, freq="M")
        if len(hist_m)
        else pd.PeriodIndex([], freq="M")
    )
    if len(fc_m):
        start = hist_index.max() + 1 if len(hist_index) else fc_m.index.min()
        fc_index = pd.period_range(start=start, periods=forecast_months, freq="M")
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
    "Moving average (7-day)": "moving_average",
    "Prophet": "prophet",
}


def generate_forecast(days_back: int = 365, days_ahead: int = 365, use_prophet: bool = True) -> dict:
    """Backtest all candidate models, forecast each one, and mark the lowest-WAPE winner."""
    historical_df = get_historical_sales(days_back=days_back)

    if len(historical_df) == 0:
        return {
            "error": "No historical data available",
            "historical": [],
            "forecast": [],
        }

    y = historical_df["quantity"]
    holdout = _holdout_size(len(historical_df))
    candidates = []

    naive_wape = _backtest_seasonal_naive(y, holdout) if holdout else None
    candidates.append({
        "id": "seasonal_naive",
        "name": "Seasonal naive (7-day)",
        "wape": naive_wape,
    })

    ma_wape = _backtest_moving_average(y, holdout) if holdout else None
    candidates.append({
        "id": "moving_average",
        "name": "Moving average (7-day)",
        "wape": ma_wape,
    })

    prophet_wape = None
    if use_prophet and holdout:
        try:
            prophet_wape = _backtest_prophet(historical_df, holdout)
        except Exception as e:
            print(f"Prophet backtest failed: {e}")
    candidates.append({"id": "prophet", "name": "Prophet", "wape": prophet_wape})

    scored = [c for c in candidates if c["wape"] is not None]
    if scored:
        winner = min(scored, key=lambda c: c["wape"])["name"]
        selection = f"lowest WAPE on last {holdout} days"
    elif use_prophet and len(historical_df) >= 14:
        winner = "Prophet"
        selection = "not enough history to backtest; defaulted to Prophet"
    else:
        winner = "Moving average (7-day)"
        selection = "not enough history to backtest; defaulted to moving average"

    builders = {
        "Prophet": lambda: forecast_with_prophet(historical_df, days_ahead=days_ahead),
        "Moving average (7-day)": lambda: forecast_simple_moving_average(historical_df, days_ahead=days_ahead),
        "Seasonal naive (7-day)": lambda: _seasonal_naive_result(historical_df, days_ahead=days_ahead),
    }

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

    if winner not in model_forecasts:
        if "Moving average (7-day)" in model_forecasts:
            winner = "Moving average (7-day)"
            selection = "selected model failed; fell back to moving average"
        elif model_forecasts:
            winner = next(iter(model_forecasts))
            selection = f"selected model failed; fell back to {winner}"
        else:
            return {
                "error": "All forecast models failed",
                "historical": historical_df[["date", "quantity"]].to_dict("records"),
                "forecast": [],
            }

    historical = historical_df[["date", "quantity"]].to_dict("records")
    result = {
        "historical": historical,
        "forecast": model_forecasts[winner],
        "model": winner,
        "model_forecasts": model_forecasts,
        "candidates": candidates,
        "selection": selection,
        "holdout_days": holdout,
    }
    alt = []
    for extra in (14, 60):
        if extra == holdout or extra >= len(historical_df) // 2:
            continue
        alt.append({
            "holdout_days": extra,
            "seasonal_naive_wape": _backtest_seasonal_naive(y, extra),
            "moving_average_wape": _backtest_moving_average(y, extra),
        })
    result["alt_holdouts"] = alt
    return result


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
