import pandas as pd
import yfinance as yf


def fetch_closing_prices(symbol: str, period: str, interval: str) -> pd.Series:
    history = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
    if history.empty or "Close" not in history:
        raise RuntimeError(f"No closing-price data returned for {symbol}")
    return history["Close"].dropna()
