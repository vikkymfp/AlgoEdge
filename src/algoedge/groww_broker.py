from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from growwapi import GrowwAPI

from algoedge.config import Settings
from algoedge.token_service import BrokerNotConnectedError, TokenService


@dataclass
class GrowwBroker:
    settings: Settings
    token_service: TokenService

    @classmethod
    def from_settings(cls, settings: Settings) -> GrowwBroker:
        """Builds a broker with its own fresh TokenService - fine for a
        standalone one-shot script (connect_groww.py), but web_server.py
        should construct one shared TokenService and pass it in directly so
        the whole app shares a single connection/status instead of each
        broker instance re-authenticating independently."""
        return cls(settings=settings, token_service=TokenService(settings))

    @property
    def client(self) -> GrowwAPI:
        """Raises BrokerNotConnectedError instead of ever returning a stale
        or unauthenticated client - every call site already has to handle
        Groww errors, so this just gives them one more clear, catchable
        reason a call couldn't be made."""
        return self.token_service.effective_client()

    def check_connection(self) -> dict:
        return self.client.get_user_profile()

    def verify_connection(self) -> None:
        response = self.check_connection()
        if not isinstance(response, dict):
            raise TypeError("Groww connection returned an invalid response")

    def execute(self, action: str, price: float, quantity: int | None = None) -> dict:
        if not self.settings.live_trading:
            raise RuntimeError(
                "Live trading is disabled. Set ALGOEDGE_LIVE_TRADING=true only after validation."
            )
        if action not in {"buy", "sell"}:
            raise ValueError(f"Unsupported action: {action}")
        if not self.token_service.is_connected():
            raise BrokerNotConnectedError(
                "Groww is not connected. Configure it in API Management before trading."
            )

        result = self.client.place_order(
            trading_symbol=self.settings.symbol,
            quantity=quantity or self.settings.groww_quantity,
            validity="DAY",
            exchange=self.settings.groww_exchange,
            segment="CASH",
            product=self.settings.groww_product,
            order_type=self.settings.groww_order_type,
            transaction_type="BUY" if action == "buy" else "SELL",
            price=price if self.settings.groww_order_type == "LIMIT" else 0,
            order_reference_id=f"AE-{uuid4().hex[:12]}",
        )
        return result
