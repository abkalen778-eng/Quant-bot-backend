from typing import Any

from fastapi import HTTPException

from main import app, _get_json, _sma, _rsi, PUBLIC_BASE


def _historical_candles(product: str, granularity: int = 3600) -> list[list[Any]]:
    candles = _get_json(
        f"{PUBLIC_BASE}/products/{product}/candles",
        params={"granularity": granularity},
    )
    if not isinstance(candles, list) or len(candles) < 70:
        raise HTTPException(status_code=502, detail=f"Not enough historical candles for {product}")
    return sorted(candles, key=lambda x: x[0])


def _signal_at(closes: list[float]) -> tuple[str, int]:
    s20 = _sma(closes, 20)
    s50 = _sma(closes, 50)
    r14 = _rsi(closes, 14)
    score = 0
    if s20 is not None and s50 is not None:
        score += 1 if s20 > s50 else -1
    if r14 is not None:
        if r14 < 35:
            score += 1
        elif r14 > 70:
            score -= 1
    if s20 is not None:
        score += 1 if closes[-1] > s20 else -1
    signal = "BULLISH" if score >= 2 else "BEARISH" if score <= -2 else "NEUTRAL"
    return signal, score


def run_backtest(product: str, fee_bps: float = 40.0) -> dict[str, Any]:
    product = product.upper()
    candles = _historical_candles(product)
    closes = [float(x[4]) for x in candles]
    times = [int(x[0]) for x in candles]

    in_position = False
    entry_price = 0.0
    trades: list[dict[str, Any]] = []
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    fee = fee_bps / 10000.0

    for i in range(50, len(closes)):
        signal, score = _signal_at(closes[: i + 1])
        price = closes[i]

        if not in_position and signal == "BULLISH":
            entry_price = price
            entry_time = times[i]
            in_position = True
        elif in_position and signal == "BEARISH":
            gross_return = (price / entry_price) - 1.0
            net_return = gross_return - (2 * fee)
            equity *= (1.0 + net_return)
            peak = max(peak, equity)
            drawdown = (peak - equity) / peak if peak else 0.0
            max_drawdown = max(max_drawdown, drawdown)
            trades.append({
                "entry_time": entry_time,
                "exit_time": times[i],
                "entry_price": round(entry_price, 8),
                "exit_price": round(price, 8),
                "gross_return_pct": round(gross_return * 100, 3),
                "net_return_pct": round(net_return * 100, 3),
                "exit_score": score,
            })
            in_position = False

    if in_position:
        price = closes[-1]
        gross_return = (price / entry_price) - 1.0
        net_return = gross_return - (2 * fee)
        equity *= (1.0 + net_return)
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak if peak else 0.0
        max_drawdown = max(max_drawdown, drawdown)
        trades.append({
            "entry_time": entry_time,
            "exit_time": times[-1],
            "entry_price": round(entry_price, 8),
            "exit_price": round(price, 8),
            "gross_return_pct": round(gross_return * 100, 3),
            "net_return_pct": round(net_return * 100, 3),
            "exit_score": None,
            "forced_close_at_end": True,
        })

    wins = sum(1 for t in trades if t["net_return_pct"] > 0)
    losses = sum(1 for t in trades if t["net_return_pct"] <= 0)
    win_rate = (wins / len(trades) * 100) if trades else 0.0
    total_return = (equity - 1.0) * 100
    buy_hold = ((closes[-1] / closes[50]) - 1.0) * 100

    return {
        "product": product,
        "timeframe": "1h",
        "candles_tested": len(closes),
        "fee_assumption_bps_per_side": fee_bps,
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 2),
        "strategy_return_pct": round(total_return, 2),
        "buy_hold_return_pct": round(buy_hold, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "trade_log": trades,
        "note": "Historical backtest only. Past performance does not guarantee future results.",
    }


@app.get("/backtest/{product_id}")
def backtest_product(product_id: str, fee_bps: float = 40.0) -> dict[str, Any]:
    if fee_bps < 0 or fee_bps > 500:
        raise HTTPException(status_code=400, detail="fee_bps must be between 0 and 500")
    return run_backtest(product_id, fee_bps)


@app.get("/backtest")
def backtest_all(products: str = "BTC-USD,ETH-USD,SOL-USD", fee_bps: float = 40.0) -> dict[str, Any]:
    product_list = [p.strip().upper() for p in products.split(",") if p.strip()][:10]
    results = []
    for product in product_list:
        try:
            results.append(run_backtest(product, fee_bps))
        except HTTPException as exc:
            results.append({"product": product, "error": exc.detail})
    return {"count": len(results), "results": results}
