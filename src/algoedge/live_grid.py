from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from growwapi.groww.exceptions import GrowwAPIException

from algoedge.config import Settings
from algoedge.groww_broker import GrowwBroker
from fno_signals.broker import check_instrument_master


class LiveGridService:
    def __init__(self, broker: GrowwBroker, settings: Settings) -> None:
        self.broker = broker
        self.settings = settings
        self.order_ledger_path = Path("data/grid_orders.json")

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        positions = self._payload(self.broker.client.get_positions_for_user(segment="CASH"))
        orders = self._payload(
            self.broker.client.get_order_list(segment="CASH", page=0, page_size=25)
        )
        symbols = {
            str(item.get("trading_symbol", "")).upper()
            for item in [*positions, *orders]
            if item.get("trading_symbol")
        }
        if not symbols:
            symbols.add(self.settings.symbol.upper())
        return {
            "displayName": self.settings.display_name,
            "grids": [self._grid(symbol, positions, orders) for symbol in sorted(symbols)],
        }

    def positions_snapshot(self) -> dict[str, Any]:
        positions = self._payload(self.broker.client.get_positions_for_user(segment="CASH"))
        return {
            "source": "LIVE BROKER DATA",
            "positions": [self._position(item) for item in positions],
        }

    def orders_snapshot(self) -> dict[str, Any]:
        cash_orders = self._payload(
            self.broker.client.get_order_list(segment="CASH", page=0, page_size=50)
        )
        fno_orders = self._payload(
            self.broker.client.get_order_list(segment="FNO", page=0, page_size=50)
        )
        ledger = self._load_ledger()
        orders = [self._normalize_order(item, ledger) for item in [*cash_orders, *fno_orders]]
        return {"source": "LIVE BROKER DATA", "orders": orders}

    def account_snapshot(self) -> dict[str, Any]:
        tasks = {
            "profile": self.broker.client.get_user_profile,
            "holdings": self.broker.client.get_holdings_for_user,
            "positions": self.broker.client.get_positions_for_user,
            "margin": self.broker.client.get_available_margin_details,
            "cash_orders": lambda: self.broker.client.get_order_list(
                segment="CASH", page=0, page_size=25
            ),
            "fno_orders": lambda: self.broker.client.get_order_list(
                segment="FNO", page=0, page_size=25
            ),
        }
        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = {name: executor.submit(function) for name, function in tasks.items()}
            # future.result() is safe to call twice on the same future (it
            # replays the cached result/exception, no re-execution) - one
            # pass gets the payload, the other gets the real success/error
            # this capability's own diagnostic status is built from. Never
            # inferred from whether the broker session merely connected.
            responses = {name: self._safe_read(future) for name, future in futures.items()}
            statuses = {name: self._capability_status(future) for name, future in futures.items()}

        profile = responses["profile"]
        holdings = self._payload(responses["holdings"])
        positions = self._payload(responses["positions"])
        margin = self._payload(responses["margin"])
        cash_orders = self._payload(responses["cash_orders"])
        fno_orders = self._payload(responses["fno_orders"])

        cash_orders_ok, cash_orders_error = statuses["cash_orders"]
        fno_orders_ok, fno_orders_error = statuses["fno_orders"]
        orders_ok = cash_orders_ok and fno_orders_ok
        orders_error = cash_orders_error or fno_orders_error

        profile_ok, profile_error = statuses["profile"]
        margin_ok, margin_error = statuses["margin"]
        holdings_ok, holdings_error = statuses["holdings"]
        positions_ok, positions_error = statuses["positions"]
        instrument_master = check_instrument_master(self.broker.client)
        # "LIVE BROKER DATA" must mean at least one real account endpoint
        # actually responded - never a fixed label implying success
        # regardless of whether every single one of the five parallel
        # calls above actually failed (e.g. an expired token).
        any_capability_ok = profile_ok or margin_ok or holdings_ok or positions_ok or orders_ok

        return {
            "source": "LIVE BROKER DATA" if any_capability_ok else "GROWW DATA UNAVAILABLE",
            "profile": {
                "connected": profile_ok,
                "error": profile_error,
                "nseEnabled": profile.get("nse_enabled"),
                "bseEnabled": profile.get("bse_enabled"),
                "ddpiEnabled": profile.get("ddpi_enabled"),
                "activeSegments": profile.get("active_segments", []),
            },
            "margin": margin,
            "marginStatus": {"available": margin_ok, "error": margin_error},
            "holdings": holdings,
            "holdingsStatus": {"available": holdings_ok, "error": holdings_error},
            "positions": positions,
            "positionsStatus": {"available": positions_ok, "error": positions_error},
            "orders": [*cash_orders, *fno_orders],
            "ordersStatus": {"available": orders_ok, "error": orders_error},
            "instrumentMaster": {
                "available": instrument_master["available"],
                "count": instrument_master["count"],
                "error": instrument_master["error"],
                "fields": [
                    "exchange", "exchange_token", "trading_symbol", "groww_symbol", "name",
                    "instrument_type", "segment", "series", "isin", "underlying_symbol",
                    "underlying_exchange_token", "expiry_date", "strike_price", "lot_size",
                    "tick_size", "freeze_quantity", "buy_allowed", "sell_allowed",
                ],
            },
            "marketData": self._market_data_status(),
        }

    def _market_data_status(self) -> dict[str, Any]:
        """Market data (get_ltp/get_quote/get_ohlc) needs Groww's separate
        Live Data permission. Reported from what TokenService actually
        observed on those endpoints - AVAILABLE only after a real market
        data call succeeded; otherwise (denied, or not yet exercised) it
        stays PERMISSION_DENIED_OR_UNAVAILABLE. A denial here is endpoint-
        level and never affects the broker connection itself."""
        token_service = getattr(self.broker, "token_service", None)
        observed = token_service.capability_status("market_data") if token_service is not None else None
        available = observed is not None and observed["status"] == "AVAILABLE"
        return {
            "status": "AVAILABLE" if available else "PERMISSION_DENIED_OR_UNAVAILABLE",
            "error": observed["error"] if observed is not None else None,
            "availableMethods": ["get_ltp", "get_quote", "get_ohlc"],
        }

    @staticmethod
    def _safe_read(future: Any) -> dict[str, Any]:
        try:
            value = future.result(timeout=8)
            return value if isinstance(value, dict) else {}
        except (GrowwAPIException, KeyError, OSError, TimeoutError, TypeError, ValueError):
            return {}

    @staticmethod
    def _capability_status(future: Any) -> tuple[bool, str | None]:
        """Whether this specific Groww call actually succeeded - the real
        signal account_snapshot()'s per-capability status fields are built
        from. Never leaks anything beyond the broker's own error message
        (business-level, e.g. "Access forbidden" - the same class of
        message token_service.status().lastError already surfaces
        elsewhere in this app); never a credential or connection string."""
        try:
            value = future.result(timeout=8)
            if isinstance(value, dict):
                return True, None
            return False, "Unexpected response shape from Groww"
        except GrowwAPIException as error:
            return False, str(error)
        except (KeyError, OSError, TimeoutError, TypeError, ValueError) as error:
            return False, f"{type(error).__name__}: {error}"

    def _grid(
        self,
        symbol: str,
        positions: list[dict[str, Any]],
        orders: list[dict[str, Any]],
    ) -> dict[str, Any]:
        position = next(
            (item for item in positions if str(item.get("trading_symbol", "")).upper() == symbol),
            {},
        )
        quote = self._quote(symbol)
        size = self._number(position.get("quantity"), 0)
        average_entry = self._number(position.get("net_price"), None)
        mark_price = self._number(quote.get("ltp"), None) if quote else None
        unrealized_pnl = (
            (mark_price - average_entry) * size
            if mark_price is not None and average_entry is not None
            else None
        )
        open_orders = [
            item for item in orders
            if str(item.get("order_status", "")).upper() in {"OPEN", "PENDING", "TRIGGER_PENDING"}
            and str(item.get("trading_symbol", "")).upper() == symbol
        ]
        ledger = self._load_ledger()
        normalized_orders = [self._normalize_order(item, ledger) for item in open_orders]
        return {
            "id": f"live-{symbol.lower()}",
            "name": f"Live {symbol}",
            "symbol": symbol,
            "description": f"{symbol} · {self.settings.groww_exchange} · Cash delivery",
            "status": "RUNNING" if size else "PAUSED",
            "source": "LIVE BROKER DATA",
            "range": "Not configured",
            "spacing": "Not configured",
            "realizedPnl": self._number(position.get("realised_pnl"), 0),
            "size": size,
            "side": "LONG" if size > 0 else "SHORT" if size < 0 else "FLAT",
            "averageEntry": average_entry,
            "markPrice": mark_price,
            "unrealizedPnl": unrealized_pnl,
            "liquidationPrice": None,
            "utilization": None,
            "health": None,
            "nextTrigger": None,
            "orders": normalized_orders,
        }

    def _position(self, position: dict[str, Any]) -> dict[str, Any]:
        symbol = str(position.get("trading_symbol", ""))
        quantity = self._number(position.get("quantity"), 0)
        average_price = self._number(position.get("net_price"), None)
        quote = self._quote(symbol.upper()) if symbol else None
        ltp = self._number(quote.get("ltp"), None) if quote else None
        unrealized_pnl = (
            (ltp - average_price) * quantity
            if ltp is not None and average_price is not None
            else None
        )
        return {
            "symbol": symbol,
            "quantity": quantity,
            "side": "LONG" if quantity > 0 else "SHORT" if quantity < 0 else "FLAT",
            "averagePrice": average_price,
            "ltp": ltp,
            "unrealizedPnl": unrealized_pnl,
            "realizedPnl": self._number(position.get("realised_pnl"), 0),
        }

    def _quote(self, symbol: str) -> dict[str, Any] | None:
        try:
            response = self.broker.client.get_quote(
                trading_symbol=symbol,
                exchange=self.settings.groww_exchange,
                segment="CASH",
            )
            return self._payload(response)
        except (GrowwAPIException, KeyError, TypeError, ValueError):
            return None

    def _normalize_order(self, order: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
        reference = str(order.get("order_reference_id", ""))
        actual_price = self._number(
            order.get("price") or order.get("average_fill_price"),
            0,
        )
        return {
            "symbol": str(order.get("trading_symbol", "")),
            "side": str(order.get("transaction_type", "")).upper(),
            "quantity": self._number(order.get("quantity"), 0),
            "actualPrice": actual_price,
            "gridLevel": ledger.get(reference),
            "status": str(order.get("order_status", "")).upper(),
        }

    def _load_ledger(self) -> dict[str, Any]:
        if not self.order_ledger_path.exists():
            return {}
        try:
            value = json.loads(self.order_ledger_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _payload(response: dict[str, Any]) -> list[dict[str, Any]]:
        payload = response.get("payload", response)
        if isinstance(payload, dict):
            # "holdings" confirmed live against the real account -
            # get_holdings_for_user() wraps its list under that key too,
            # same shape as positions/order_list/quote below.
            for key in ("positions", "order_list", "quote", "holdings"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
            return payload
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _number(value: Any, default: float | None) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
