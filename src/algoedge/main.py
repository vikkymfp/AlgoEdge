from algoedge.config import get_settings
from algoedge.market_data import fetch_closing_prices
from algoedge.paper_broker import PaperBroker
from algoedge.strategy import moving_average_signal


def main() -> None:
    settings = get_settings()
    prices = fetch_closing_prices(settings.symbol, settings.period, settings.interval)
    signal = moving_average_signal(prices, settings.short_window, settings.long_window)

    broker = PaperBroker(settings.initial_cash)
    broker.execute(signal.action, signal.price)

    print(f"{settings.app_name} | {settings.symbol}")
    print(f"Signal: {signal.action} at {signal.price:.2f}")
    print(f"Paper equity: {broker.equity(signal.price):.2f}")
    if settings.live_trading:
        print("Live trading is enabled in settings, but no live broker is configured.")


if __name__ == "__main__":
    main()
