import asyncio

import pytest

from algoedge import web_server
from algoedge.manual_trading import ManualOrderRequest, validate_order_request
from algoedge.request_guard import parse_allowed_hosts, rejection_reason

LOCAL = "127.0.0.1:5173"
HEADER = {"X-AlgoEdge-Request": "1"}


@pytest.mark.parametrize("host", ["localhost", "localhost:5173", "127.0.0.1:5173", "[::1]:5173"])
def test_loopback_hosts_are_allowed(host):
    assert rejection_reason("GET", host, None, None) is None


@pytest.mark.parametrize("host", [None, "", "evil.example", "evil.example:5173", "127.0.0.1.evil.example"])
def test_other_hosts_are_refused_even_for_reads(host):
    # DNS rebinding: the attacker's name resolves to 127.0.0.1 but keeps its own Host.
    assert rejection_reason("GET", host, None, None) == "Host not allowed"


def test_operator_allow_listed_host_is_accepted():
    extra = parse_allowed_hosts(" vps.internal , ")
    assert rejection_reason("GET", "vps.internal:5181", None, None, extra) is None
    assert rejection_reason("GET", "other.internal", None, None, extra) == "Host not allowed"


def test_post_requires_the_custom_header():
    assert rejection_reason("POST", LOCAL, None, None) is not None
    assert rejection_reason("POST", LOCAL, None, "1") is None


@pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1:9999", "null"])
def test_post_from_another_origin_is_refused(origin):
    assert rejection_reason("POST", LOCAL, origin, "1") == "Cross-origin request refused"


def test_post_from_same_origin_is_accepted():
    assert rejection_reason("POST", LOCAL, "http://127.0.0.1:5173", "1") is None


def _status(method, path, headers):
    """Drives the real ASGI app without a network or an HTTP test client."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "path": path, "raw_path": path.encode(), "query_string": b"", "scheme": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "server": ("127.0.0.1", 5173), "client": ("127.0.0.1", 50000),
    }
    asyncio.run(web_server.app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_server_blocks_a_forged_post_before_any_handler_runs():
    reset = "/api/auto-trading/kill-switch/reset"
    assert _status("POST", reset, {"Host": LOCAL, "Origin": "https://evil.example"}) == 403
    assert _status("POST", reset, {"Host": LOCAL}) == 403
    assert _status("GET", "/api/alerts", {"Host": "evil.example"}) == 403
    assert _status("GET", "/api/alerts", {"Host": LOCAL}) != 403


def test_order_lots_are_capped():
    request = ManualOrderRequest(
        right="CE", side="BUY", order_type="MARKET", lots=11, product="NRML", price=None, trigger_price=None,
    )
    with pytest.raises(ValueError, match="exceeds the per-order limit of 10"):
        validate_order_request(request, max_lots=10)
    validate_order_request(request, max_lots=11)
    validate_order_request(request)
