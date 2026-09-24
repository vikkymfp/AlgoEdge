from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AlgoEdge"
    display_name: str = "My Account"
    environment: str = "development"
    symbol: str = "AAPL"
    period: str = "6mo"
    interval: str = "1d"
    short_window: int = 20
    long_window: int = 50
    initial_cash: float = 100_000.0
    live_trading: bool = False
    broker: str = "paper"
    groww_access_token: str | None = None
    groww_api_key: str | None = None
    groww_api_secret: str | None = None
    groww_exchange: str = "NSE"
    groww_quantity: int = 1
    groww_product: str = "CNC"
    groww_order_type: str = "MARKET"

    # Local SQL Server persistence (strategy signals, orders, risk state).
    # Empty db_server means "no database configured" - persistence is
    # skipped entirely rather than erroring, since it's an optional
    # observability layer, not a requirement for trading to function.
    db_server: str = ""
    db_name: str = "AlgoEdge"
    db_trusted_connection: bool = True
    db_odbc_driver: str = "ODBC Driver 18 for SQL Server"

    # Fernet key (base64, from Fernet.generate_key()) used to encrypt broker
    # credentials/tokens at rest in the database. Blank disables DB-backed
    # credential storage - UI-submitted credentials still work for that
    # process's lifetime, just aren't persisted across a restart. Never
    # commit a real value; generate one locally and keep it in .env only.
    credential_encryption_key: str = ""

    # Net P&L cost model (spec §27). All default to 0.0 - "not yet
    # configured" rather than a guessed brokerage/tax rate. Set these once
    # you have your account's real, current published rates; see
    # cost_model.py for what each one means.
    cost_brokerage_per_order: float = 0.0
    cost_stt_percent_on_sell: float = 0.0
    cost_exchange_charges_percent: float = 0.0
    cost_gst_percent: float = 0.0
    cost_stamp_duty_percent_on_buy: float = 0.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ALGOEDGE_",
        case_sensitive=False,
    )


def get_settings() -> Settings:
    return Settings()
