from __future__ import annotations

import os
from typing import Any, Literal

from fastapi import HTTPException, Query

from backtest_app import app
from broker_plugin import CoinbaseBrokerPlugin
from main import (
    COINBASE_API_KEY,
    COINBASE_API_SECRET,
    DRY_RUN,
    MAX_ORDER_USD,
    STRATEGY_NAME,
    TRADING_ENABLED,
    build_signal,
)

BROKER = CoinbaseBrokerPlugin(
    api_key=COINBASE_API_KEY,
    api_secret=COINBASE_API_SECRET,
    trading_enabled=TRADING_ENABLED,
    dry_run=DRY_RUN,
)

MCP_BRIDGE_ENABLED = os.getenv("MCP_BRIDGE_ENABLED", "true").lower() == "true"


def _validated_intent(
    product_id: str,
    side: Literal["BUY", "SELL"],
    size_usd: float,
    require_signal: bool,
) -> dict[str, Any]:
    if not MCP_BRIDGE_ENABLED:
        raise HTTPException(status_code=503, detail="MCP bridge is disabled")
    if size_usd <= 0:
        raise HTTPException(status_code=400, detail="size_usd must be greater than 0")
    if size_usd > MAX_ORDER_USD:
        raise HTTPException(status_code=400, detail="Order intent exceeds MAX_ORDER_USD")

    product = product_id.strip().upper()
    signal = build_signal(product)
    required = "BULLISH" if side == "BUY" else "BEARISH"
    if require_signal and signal["signal"] != required:
        raise HTTPException(
            status_code=409,
            detail=f"Intent blocked: {side} requires {required}; current signal {signal['signal']}",
        )

    intent = BROKER.build_intent(
        product_id=product,
        side=side,
        size_usd=size_usd,
        signal=signal,
    )
    return {
        "accepted": True,
        "executed": False,
        "mode": "MCP_INTENT_ONLY",
        "strategy": STRATEGY_NAME,
        "broker": "coinbase",
        "intent": intent.to_dict(),
        "mcp_payload": intent.to_mcp_payload(),
        "note": "This endpoint does not place an order. It returns a validated Coinbase order intent for an authorized MCP/execution layer.",
    }


@app.get("/mcp/status")
def mcp_status() -> dict[str, Any]:
    return {
        "name": "Quant Bot Coinbase MCP Bridge",
        "status": "online" if MCP_BRIDGE_ENABLED else "disabled",
        "strategy": STRATEGY_NAME,
        "broker": "coinbase",
        "execution_exposed": False,
        "max_order_usd": MAX_ORDER_USD,
        "broker_status": BROKER.status(),
    }


@app.get("/mcp/order-intent/{product_id}")
def mcp_order_intent(
    product_id: str,
    side: Literal["BUY", "SELL"] = Query(default="BUY"),
    size_usd: float = Query(default=5.0, gt=0),
    require_signal: bool = Query(default=True),
) -> dict[str, Any]:
    return _validated_intent(product_id, side, size_usd, require_signal)


@app.get("/mcp/openapi.json")
def mcp_openapi() -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Quant Bot Coinbase MCP Bridge",
            "version": "1.0.0",
            "description": "Validated Quant Bot signals and Coinbase order intents. This surface does not execute orders.",
        },
        "servers": [{"url": "https://quant-bot-backend-production.up.railway.app"}],
        "paths": {
            "/mcp/status": {
                "get": {
                    "operationId": "getQuantCoinbaseBridgeStatus",
                    "summary": "Get Quant Bot Coinbase bridge status",
                    "responses": {"200": {"description": "Bridge status"}},
                }
            },
            "/tools/signal/{product_id}": {
                "get": {
                    "operationId": "getQuantSignalForCoinbase",
                    "summary": "Get the unchanged Quant Bot strategy signal",
                    "parameters": [
                        {"name": "product_id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "Quant Bot signal"}},
                }
            },
            "/mcp/order-intent/{product_id}": {
                "get": {
                    "operationId": "createCoinbaseOrderIntent",
                    "summary": "Create a validated Coinbase order intent without executing it",
                    "parameters": [
                        {"name": "product_id", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "side", "in": "query", "required": False, "schema": {"type": "string", "enum": ["BUY", "SELL"], "default": "BUY"}},
                        {"name": "size_usd", "in": "query", "required": False, "schema": {"type": "number", "default": 5.0}},
                        {"name": "require_signal", "in": "query", "required": False, "schema": {"type": "boolean", "default": True}},
                    ],
                    "responses": {
                        "200": {"description": "Validated MCP order intent"},
                        "409": {"description": "Strategy signal does not authorize the requested side"},
                    },
                }
            },
        },
    }
