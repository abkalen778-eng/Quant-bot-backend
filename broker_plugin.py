from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from typing import Any, Literal

from coinbase.rest import RESTClient

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class OrderIntent:
    """Broker-neutral order request produced by the Quant Bot engine."""

    client_order_id: str
    product_id: str
    side: Side
    size_usd: float
    reference_price: float
    estimated_base_size: float
    signal: str
    signal_key: str | None
    strategy: str | None
    stop_loss: Any = None
    exit_plan: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_mcp_payload(self) -> dict[str, Any]:
        """Stable payload an MCP adapter can translate into its Coinbase order tool."""
        return {
            "provider": "coinbase",
            "action": "place_market_order",
            "order": self.to_dict(),
        }


class CoinbaseBrokerPlugin:
    """Coinbase execution adapter. Strategy logic deliberately lives elsewhere."""

    name = "coinbase"

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        trading_enabled: bool = False,
        dry_run: bool = True,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.trading_enabled = trading_enabled
        self.dry_run = dry_run

    @property
    def credentials_present(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _client(self) -> RESTClient:
        if not self.credentials_present:
            raise RuntimeError("Coinbase credentials are not configured")
        return RESTClient(api_key=self.api_key, api_secret=self.api_secret)

    def status(self) -> dict[str, Any]:
        return {
            "plugin": self.name,
            "credentials_present": self.credentials_present,
            "trading_enabled": self.trading_enabled,
            "dry_run": self.dry_run,
            "mcp_ready": True,
        }

    def auth_check(self) -> dict[str, Any]:
        try:
            self._client().get_accounts(limit=1)
            return {"connected": True, **self.status()}
        except Exception as exc:
            return {"connected": False, "error_type": type(exc).__name__, **self.status()}

    def build_intent(
        self,
        *,
        product_id: str,
        side: Side,
        size_usd: float,
        signal: dict[str, Any],
    ) -> OrderIntent:
        price = float(signal["price"])
        return OrderIntent(
            client_order_id=str(uuid.uuid4()),
            product_id=product_id,
            side=side,
            size_usd=round(float(size_usd), 2),
            reference_price=price,
            estimated_base_size=round(float(size_usd) / price, 12),
            signal=str(signal["signal"]),
            signal_key=signal.get("signal_key"),
            strategy=signal.get("strategy"),
            stop_loss=signal.get("stop_loss"),
            exit_plan=signal.get("exit_plan"),
        )

    def execute(self, intent: OrderIntent) -> dict[str, Any]:
        preview = intent.to_dict()
        if self.dry_run or not self.trading_enabled:
            return {
                "accepted": True,
                "executed": False,
                "mode": "DRY_RUN",
                "plugin": self.name,
                "preview": preview,
                "mcp_payload": intent.to_mcp_payload(),
            }

        client = self._client()
        try:
            if intent.side == "BUY":
                result = client.market_order_buy(
                    client_order_id=intent.client_order_id,
                    product_id=intent.product_id,
                    quote_size=f"{intent.size_usd:.2f}",
                )
            else:
                result = client.market_order_sell(
                    client_order_id=intent.client_order_id,
                    product_id=intent.product_id,
                    base_size=f"{intent.estimated_base_size:.12f}",
                )
            return {
                "accepted": True,
                "executed": True,
                "mode": "LIVE",
                "plugin": self.name,
                "order": result.to_dict() if hasattr(result, "to_dict") else str(result),
            }
        except Exception as exc:
            raise RuntimeError(f"Coinbase order failed: {type(exc).__name__}") from exc
