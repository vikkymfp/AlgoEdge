# AlgoEdge Grid Monitor

This static dashboard renders each grid page with position size, average entry, mark price, unrealized P&L, liquidation price, and open orders. Each order displays the broker's actual price beside the original grid level.

The UI requests `GET /api/grids` and expects:

```json
{
  "grids": [
    {
      "id": "delta-01",
      "name": "Delta 01",
      "symbol": "RELIANCE",
      "description": "RELIANCE · NSE · Cash delivery",
      "status": "RUNNING",
      "source": "LIVE BROKER DATA",
      "range": "₹2,740 — ₹3,020",
      "spacing": "₹20.00",
      "realizedPnl": 8460,
      "size": 320,
      "side": "LONG",
      "averageEntry": 2864.5,
      "markPrice": 2918.25,
      "unrealizedPnl": 17200,
      "liquidationPrice": 2486,
      "utilization": 64,
      "health": 82,
      "nextTrigger": 2940,
      "orders": [
        { "side": "BUY", "quantity": 80, "actualPrice": 2842.1, "gridLevel": 2840, "status": "OPEN" }
      ]
    }
  ]
}
```

When the API is unavailable, the UI clearly labels itself as a demo feed and uses local snapshots. It never reads or displays environment credentials.
