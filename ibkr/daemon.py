"""IBKR connection daemon.

Owns one ib_async connection (clientId=1) and exposes its surface to the rest
of the desk over HTTP at 127.0.0.1:7790. Replaces the prior pattern where every
scheduled skill opened its own connection with a unique clientId — TWS now
shows a single client tab, and reconnect/rate-limit logic lives in one place.

Run with:
    python -m ibkr.daemon

Auth: every request must carry `Authorization: Bearer <IBKR_DAEMON_TOKEN>`,
loaded from .env at startup. localhost-bound, but the token still gates a
stray local script from accidentally placing an order.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# ── bootstrap ───────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

import db.store as store  # noqa: E402

log = logging.getLogger("ibkr_daemon")

_LOCK_PATH = PROJECT_ROOT / "data" / "ibkr_daemon.lock"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7790
_DAEMON_CLIENT_ID = 1  # the daemon owns clientId=1

# state shared by handlers (single-process, single asyncio loop)
_state: dict[str, Any] = {
    "ib": None,           # ib_async.IB or None
    "mode": "paper",
    "started_at": time.time(),
    "config": {},
    "ibkr_host": "127.0.0.1",
    "ibkr_port": 4002,
    "active_trades": {},  # db_order_id -> Trade (keeps fillEvent closures alive)
    "contract_cache": {}, # "SYM:CCY:EXCH" -> qualified Contract
    "ib_lock": None,      # asyncio.Lock initialized in lifespan
}


# ── lockfile (single-instance) ──────────────────────────────────────────────


class LockHeld(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock() -> None:
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _LOCK_PATH.exists():
        try:
            existing = int(_LOCK_PATH.read_text(encoding="utf-8").strip())
        except Exception:
            existing = 0
        if existing and _pid_alive(existing):
            raise LockHeld(
                f"ibkr-daemon already running as PID {existing}. "
                f"If stale, delete {_LOCK_PATH}."
            )
    _LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")


def release_lock() -> None:
    try:
        if _LOCK_PATH.exists():
            existing = int(_LOCK_PATH.read_text(encoding="utf-8").strip() or "0")
            if existing == os.getpid():
                _LOCK_PATH.unlink()
    except Exception:
        pass


# ── IB connection ───────────────────────────────────────────────────────────


async def _connect_ib() -> Any:
    """Connect to IB Gateway. Reused for initial connect and reconnect loop."""
    from ib_async import IB

    ib = IB()
    host = _state["ibkr_host"]
    port = _state["ibkr_port"]

    candidate_ids = [_DAEMON_CLIENT_ID]
    candidate_ids.extend(random.sample(range(50, 1000), k=4))

    last_exc: Optional[Exception] = None
    for cid in candidate_ids:
        try:
            await asyncio.wait_for(
                ib.connectAsync(host, port, clientId=cid),
                timeout=8.0,
            )
            if cid != _DAEMON_CLIENT_ID:
                log.warning(
                    "clientId %s busy — fell back to %s. Investigate stray client.",
                    _DAEMON_CLIENT_ID, cid,
                )
            log.info("Connected to IBKR at %s:%s (clientId=%s)", host, port, cid)
            break
        except Exception as exc:
            last_exc = exc
            log.warning("connect attempt clientId=%s failed: %s", cid, exc)
            try:
                if ib.isConnected():
                    await ib.disconnectAsync()
            except Exception:
                pass
    else:
        raise TimeoutError(f"could not connect to IBKR at {host}:{port}: {last_exc}")

    # validate paper/live mode matches config
    actual_paper = port in (4002, 7497)
    expected_paper = _state["mode"] == "paper"
    if actual_paper != expected_paper:
        await ib.disconnectAsync()
        raise RuntimeError(
            f"Mode mismatch: config says '{_state['mode']}' but port {port} "
            f"is {'paper' if actual_paper else 'live'}."
        )

    ib.disconnectedEvent += _on_disconnect
    return ib


def _on_disconnect() -> None:
    log.warning("IBKR disconnected — daemon will retry")
    _state["ib"] = None
    # schedule a reconnect; the lifespan supervisor task picks it up
    loop = asyncio.get_event_loop()
    loop.create_task(_reconnect_loop())


async def _reconnect_loop() -> None:
    delay = 1.0
    while _state["ib"] is None:
        try:
            log.info("reconnecting to IBKR in %.1fs", delay)
            await asyncio.sleep(delay)
            ib = await _connect_ib()
            _state["ib"] = ib
            log.info("reconnected successfully")
            return
        except Exception as exc:
            log.warning("reconnect failed: %s", exc)
            delay = min(delay * 2.0, 30.0)


async def _ensure_connected() -> Any:
    """Return the live IB or raise if not connected (HTTP 503)."""
    ib = _state["ib"]
    if ib is None or not ib.isConnected():
        raise ConnectionError("ibkr_disconnected")
    return ib


# ── auth middleware ─────────────────────────────────────────────────────────


class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # /healthz is unauthenticated for systemd healthchecks
        if request.url.path == "/healthz":
            return await call_next(request)
        token = os.environ.get("IBKR_DAEMON_TOKEN", "")
        if not token:
            return JSONResponse(
                {"error": "daemon misconfigured: no token set"}, status_code=500
            )
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


# ── helpers ─────────────────────────────────────────────────────────────────


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


def _disconnected_response() -> JSONResponse:
    return JSONResponse(
        {"error": "ibkr_disconnected", "retrying": True},
        status_code=503,
    )


async def _resolve_contract(symbol: str, currency: str = "USD",
                            exchange: str = "SMART") -> Any:
    cache_key = f"{symbol}:{currency}:{exchange}"
    cache = _state["contract_cache"]
    if cache_key in cache:
        return cache[cache_key]

    from ib_async import Stock

    ib = await _ensure_connected()
    contract = Stock(symbol, exchange, currency)
    details = await ib.reqContractDetailsAsync(contract)
    if not details:
        raise ValueError(f"no contract found for symbol '{symbol}'")
    qualified = details[0].contract
    cache[cache_key] = qualified
    return qualified


# ── handlers ────────────────────────────────────────────────────────────────


async def healthz(request: Request) -> JSONResponse:
    ib = _state["ib"]
    connected = bool(ib and ib.isConnected())
    return JSONResponse({
        "ok": True,
        "connected": connected,
        "mode": _state["mode"],
        "uptime_s": round(time.time() - _state["started_at"], 1),
        "client_id": _DAEMON_CLIENT_ID,
    })


async def mode(request: Request) -> JSONResponse:
    return JSONResponse({"mode": _state["mode"]})


async def positions(request: Request) -> JSONResponse:
    try:
        ib = await _ensure_connected()
    except ConnectionError:
        return _disconnected_response()
    async with _state["ib_lock"]:
        # ib.portfolio() returns PortfolioItem with live marketPrice/marketValue/
        # unrealizedPNL (populated by account updates). ib.positions() only has
        # qty + avgCost. Filter qty=0 since IBKR keeps closed-out rows around.
        await ib.reqPositionsAsync()  # ensures positions are pulled at least once
        items = ib.portfolio()
        out = []
        for p in items:
            if p.position == 0:
                continue
            out.append({
                "symbol": p.contract.symbol,
                "quantity": p.position,
                "avg_cost": p.averageCost,
                "market_price": float(getattr(p, "marketPrice", 0.0) or 0.0),
                "market_value": float(getattr(p, "marketValue", 0.0) or 0.0),
                "unrealized_pnl": float(getattr(p, "unrealizedPNL", 0.0) or 0.0),
            })
    return JSONResponse(out)


async def balances(request: Request) -> JSONResponse:
    try:
        ib = await _ensure_connected()
    except ConnectionError:
        return _disconnected_response()
    async with _state["ib_lock"]:
        await ib.reqAccountSummaryAsync()
        values = ib.accountValues()
    result: dict = {"mode": _state["mode"]}
    wanted = {"NetLiquidation", "TotalCashValue", "BuyingPower",
              "RealizedPnL", "UnrealizedPnL"}
    for item in values:
        if item.tag not in wanted or item.currency != "USD":
            continue
        if item.tag == "NetLiquidation":
            result["nav"] = float(item.value)
        elif item.tag == "TotalCashValue":
            result["cash"] = float(item.value)
        elif item.tag == "BuyingPower":
            result["buying_power"] = float(item.value)
        elif item.tag == "RealizedPnL":
            result["realized_pnl_today"] = float(item.value)
        elif item.tag == "UnrealizedPnL":
            result["unrealized_pnl"] = float(item.value)
    return JSONResponse(result)


async def open_orders(request: Request) -> JSONResponse:
    try:
        ib = await _ensure_connected()
    except ConnectionError:
        return _disconnected_response()
    async with _state["ib_lock"]:
        trades = await ib.reqOpenOrdersAsync()
    out = []
    for trade in trades:
        o = trade.order
        s = trade.orderStatus
        out.append({
            "order_id": o.orderId,
            "symbol": trade.contract.symbol,
            "action": o.action,
            "order_type": o.orderType,
            "quantity": o.totalQuantity,
            "filled": s.filled,
            "remaining": s.remaining,
            "limit_price": o.lmtPrice if o.lmtPrice and o.lmtPrice > 0 else None,
            "stop_price": o.auxPrice if o.auxPrice and o.auxPrice > 0 else None,
            "status": s.status,
            "avg_fill_price": s.avgFillPrice if s.avgFillPrice > 0 else None,
        })
    return JSONResponse(out)


async def place_order(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")
    try:
        ib = await _ensure_connected()
    except ConnectionError:
        return _disconnected_response()

    from ib_async import LimitOrder, MarketOrder, StopOrder

    symbol = body.get("symbol")
    action = body.get("action")
    quantity = body.get("quantity")
    order_type = body.get("order_type")
    limit_price = body.get("limit_price")
    stop_price = body.get("stop_price")
    agent_name = body.get("agent_name", "") or ""
    session_id = body.get("session_id")
    reasoning = body.get("reasoning")

    if not all([symbol, action, quantity is not None, order_type]):
        return _err("missing one of symbol/action/quantity/order_type")

    if order_type == "MKT":
        ibkr_order = MarketOrder(action, quantity)
    elif order_type == "LMT":
        if limit_price is None:
            return _err("limit_price required for LMT")
        ibkr_order = LimitOrder(action, quantity, limit_price)
    elif order_type == "STP":
        if stop_price is None:
            return _err("stop_price required for STP")
        ibkr_order = StopOrder(action, quantity, stop_price)
    else:
        return _err(f"unknown order_type: {order_type}")

    async with _state["ib_lock"]:
        contract = await _resolve_contract(symbol)
        trade = ib.placeOrder(contract, ibkr_order)
        await asyncio.sleep(0.5)  # let IB assign orderId

    ibkr_order_id = ibkr_order.orderId
    mode_str = _state["mode"]

    db_order_id = await store.write_order(
        session_id=session_id,
        agent_name=agent_name,
        symbol=symbol,
        action=action,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit_price,
        stop_price=stop_price,
        status="submitted",
        risk_approved=True,
        human_approved=None,
        rejection_reason=None,
        reasoning=reasoning,
        mode=mode_str,
        ibkr_order_id=ibkr_order_id,
    )

    _state["active_trades"][db_order_id] = trade
    trade.fillEvent += lambda trade, fill, report: asyncio.create_task(
        _on_fill(db_order_id, agent_name, mode_str, fill, report)
    )
    # replay fills that landed during the sleep + DB-write window
    for existing_fill in list(trade.fills):
        report = getattr(existing_fill, "commissionReport", None)
        await _on_fill(db_order_id, agent_name, mode_str, existing_fill, report)

    log.info("Placed %s %s %s x%s → ibkr_id=%s", action, order_type,
             symbol, quantity, ibkr_order_id)
    return JSONResponse({
        "status": "submitted",
        "order_id": db_order_id,
        "ibkr_order_id": ibkr_order_id,
        "symbol": symbol,
        "action": action,
        "quantity": quantity,
        "order_type": order_type,
        "limit_price": limit_price,
        "stop_price": stop_price,
        "mode": mode_str,
    })


async def _on_fill(order_id: int, agent_name: str, mode_str: str,
                   fill, report) -> None:
    try:
        realized_pnl: Optional[float] = None
        if report is not None:
            rp = getattr(report, "realizedPNL", None)
            if rp is not None and abs(float(rp)) < 1e100:
                realized_pnl = float(rp)

        symbol = fill.contract.symbol
        fill_id = await store.write_fill(
            ibkr_exec_id=fill.execution.execId,
            order_id=order_id,
            agent_name=agent_name,
            filled_at=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            action=fill.execution.side,
            quantity=fill.execution.shares,
            fill_price=fill.execution.price,
            commission=report.commission if report else None,
            exchange=fill.execution.exchange,
            mode=mode_str,
            realized_pnl=realized_pnl,
        )
        await store.update_order_status(order_id, "filled")

        # Per-agent double-entry ledger: BUYs become LEND rows weighted by
        # the originating decision's conviction shares; SELLs become RETURN
        # rows distributed pro-rata across current holders. Replaces the
        # old FIFO `reconcile_symbol` reconciler. See DESK_POLICY §0/§7.
        if fill_id is not None:
            try:
                from meta_agent.ledger_writer import book_fill_to_ledger
                res = await book_fill_to_ledger(
                    fill_id=fill_id,
                    order_id=order_id,
                    symbol=symbol,
                    action=fill.execution.side,
                    quantity=float(fill.execution.shares),
                    fill_price=float(fill.execution.price),
                )
                log.info(
                    "ledger %s on %s: %d rows (decision=%s, agents=%s)",
                    res.get("event"), symbol, res.get("rows_written", 0),
                    res.get("decision_id"),
                    ",".join(res.get("agents") or []),
                )
            except Exception as exc:
                log.warning("book_fill_to_ledger(%s) failed: %s", symbol, exc)

        log.info("Fill recorded: order_id=%s exec_id=%s",
                 order_id, fill.execution.execId)
    except Exception as exc:
        log.error("Fill callback error: %s", exc)


async def cancel_order(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")
    ibkr_order_id = body.get("ibkr_order_id")
    if ibkr_order_id is None:
        return _err("ibkr_order_id required")
    try:
        ib = await _ensure_connected()
    except ConnectionError:
        return _disconnected_response()
    async with _state["ib_lock"]:
        trades = await ib.reqOpenOrdersAsync()
        for trade in trades:
            if trade.order.orderId == ibkr_order_id:
                ib.cancelOrder(trade.order)
                log.info("Cancelled order ibkr_id=%s", ibkr_order_id)
                return JSONResponse({"status": "cancelled",
                                     "ibkr_order_id": ibkr_order_id})
    return JSONResponse({"status": "not_found",
                         "ibkr_order_id": ibkr_order_id})


async def modify_order(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")
    ibkr_order_id = body.get("ibkr_order_id")
    new_limit_price = body.get("new_limit_price")
    new_quantity = body.get("new_quantity")
    if ibkr_order_id is None:
        return _err("ibkr_order_id required")
    try:
        ib = await _ensure_connected()
    except ConnectionError:
        return _disconnected_response()
    async with _state["ib_lock"]:
        trades = await ib.reqOpenOrdersAsync()
        for trade in trades:
            if trade.order.orderId == ibkr_order_id:
                order = trade.order
                if new_limit_price is not None:
                    order.lmtPrice = new_limit_price
                if new_quantity is not None:
                    order.totalQuantity = new_quantity
                ib.placeOrder(trade.contract, order)
                return JSONResponse({"status": "modified",
                                     "ibkr_order_id": ibkr_order_id})
    return JSONResponse({"status": "not_found",
                         "ibkr_order_id": ibkr_order_id})


async def resolve_endpoint(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")
    symbol = body.get("symbol")
    currency = body.get("currency", "USD")
    exchange = body.get("exchange", "SMART")
    if not symbol:
        return _err("symbol required")
    try:
        await _ensure_connected()
    except ConnectionError:
        return _disconnected_response()
    try:
        async with _state["ib_lock"]:
            qualified = await _resolve_contract(symbol, currency, exchange)
    except ValueError as exc:
        return _err(str(exc), status=404)
    return JSONResponse({
        "symbol": qualified.symbol,
        "conId": qualified.conId,
        "exchange": qualified.exchange,
        "primary_exchange": getattr(qualified, "primaryExchange", None),
        "currency": qualified.currency,
    })


async def quote(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")
    symbol = body.get("symbol")
    if not symbol:
        return _err("symbol required")
    try:
        ib = await _ensure_connected()
    except ConnectionError:
        return _disconnected_response()

    async with _state["ib_lock"]:
        contract = await _resolve_contract(symbol)
        ticker = ib.reqMktData(contract, "", False, False)
        await asyncio.sleep(2)
        ib.cancelMktData(contract)

    def _val(v):
        if v is None:
            return None
        try:
            if v != v:  # NaN
                return None
            if v == 1.7976931348623157e+308:
                return None
            return v
        except Exception:
            return None

    return JSONResponse({
        "symbol": symbol,
        "bid": _val(ticker.bid),
        "ask": _val(ticker.ask),
        "last": _val(ticker.last),
        "close": _val(ticker.close),
        "volume": _val(ticker.volume),
        "day_high": _val(ticker.high),
        "day_low": _val(ticker.low),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def bars(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")
    symbol = body.get("symbol")
    bar_size = body.get("bar_size", "5 mins")
    duration = body.get("duration", "1 D")
    what_to_show = body.get("what_to_show", "TRADES")
    if not symbol:
        return _err("symbol required")
    try:
        ib = await _ensure_connected()
    except ConnectionError:
        return _disconnected_response()

    async with _state["ib_lock"]:
        contract = await _resolve_contract(symbol)
        result = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=True,
            formatDate=1,
        )
    return JSONResponse({
        "symbol": symbol,
        "bar_size": bar_size,
        "duration": duration,
        "bars": [
            {
                "t": b.date.isoformat() if hasattr(b.date, "isoformat") else str(b.date),
                "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume,
            } for b in result
        ],
    })


async def scanner(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")
    scan_type = body.get("scan_type")
    num_rows = body.get("num_rows", 20)
    above_price = body.get("above_price")
    below_price = body.get("below_price")
    above_volume = body.get("above_volume")
    if not scan_type:
        return _err("scan_type required")
    try:
        ib = await _ensure_connected()
    except ConnectionError:
        return _disconnected_response()

    from ib_async import ScannerSubscription
    sub = ScannerSubscription(
        instrument="STK",
        locationCode="STK.US.MAJOR",
        scanCode=scan_type,
        numberOfRows=num_rows,
    )
    if above_price is not None:
        sub.abovePrice = above_price
    if below_price is not None:
        sub.belowPrice = below_price
    if above_volume is not None:
        sub.aboveVolume = above_volume

    async with _state["ib_lock"]:
        scan_data = await ib.reqScannerDataAsync(sub)
    results = []
    for i, item in enumerate(scan_data):
        c = item.contractDetails.contract
        results.append({
            "rank": i + 1,
            "symbol": c.symbol,
            "exchange": getattr(c, "primaryExchange", None) or c.exchange,
        })
    return JSONResponse({"scan_type": scan_type, "results": results})


async def news(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")
    symbol = body.get("symbol")
    max_items = body.get("max_items", 10)
    try:
        ib = await _ensure_connected()
    except ConnectionError:
        return _disconnected_response()
    headlines = []
    if symbol:
        async with _state["ib_lock"]:
            contract = await _resolve_contract(symbol)
            news_list = await ib.reqHistoricalNewsAsync(
                contract.conId,
                providerCodes="BRFG+DJNL",
                startDateTime="",
                endDateTime="",
                totalResults=max_items,
            )
        for n in news_list:
            headlines.append({
                "time": str(n.time),
                "symbol": symbol,
                "headline": n.headline,
                "provider": n.providerCode,
                "article_id": n.articleId,
            })
    return JSONResponse({"symbol": symbol, "headlines": headlines})


# ── lifespan & app ──────────────────────────────────────────────────────────


def _load_config() -> dict:
    cfg_path = PROJECT_ROOT / "config.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)


@asynccontextmanager
async def _lifespan(app: Starlette):
    cfg = _load_config()
    _state["config"] = cfg
    _state["mode"] = cfg.get("trading", {}).get("mode", "paper")
    ibcfg = cfg.get("ibkr", {})
    _state["ibkr_host"] = ibcfg.get("host", "127.0.0.1")
    _state["ibkr_port"] = ibcfg.get("port", 4002)
    _state["ib_lock"] = asyncio.Lock()
    _state["started_at"] = time.time()

    log.info("daemon starting (mode=%s, ibkr=%s:%s)",
             _state["mode"], _state["ibkr_host"], _state["ibkr_port"])

    try:
        ib = await _connect_ib()
        _state["ib"] = ib
    except Exception as exc:
        log.error("initial connect failed: %s — entering reconnect loop", exc)
        asyncio.create_task(_reconnect_loop())

    try:
        yield
    finally:
        log.info("daemon shutting down")
        ib = _state["ib"]
        if ib and ib.isConnected():
            try:
                await ib.disconnectAsync()
            except Exception:
                pass
        release_lock()


def _build_app() -> Starlette:
    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/mode", mode, methods=["GET"]),
        Route("/positions", positions, methods=["GET"]),
        Route("/balances", balances, methods=["GET"]),
        Route("/open_orders", open_orders, methods=["GET"]),
        Route("/place_order", place_order, methods=["POST"]),
        Route("/cancel_order", cancel_order, methods=["POST"]),
        Route("/modify_order", modify_order, methods=["POST"]),
        Route("/resolve", resolve_endpoint, methods=["POST"]),
        Route("/quote", quote, methods=["POST"]),
        Route("/bars", bars, methods=["POST"]),
        Route("/scanner", scanner, methods=["POST"]),
        Route("/news", news, methods=["POST"]),
    ]
    return Starlette(
        routes=routes,
        middleware=[Middleware(BearerAuth)],
        lifespan=_lifespan,
    )


# Module-level app for uvicorn import.
app = _build_app()


def _setup_logging() -> None:
    """Log to stdout — systemd's StandardOutput=append in the unit file
    captures it to logs/ibkr-daemon.log. Avoids dup writes that would happen
    if we also opened the file directly from inside the process."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(stream)


def main() -> None:
    _setup_logging()
    try:
        acquire_lock()
    except LockHeld as exc:
        log.error(str(exc))
        sys.exit(1)

    if not os.environ.get("IBKR_DAEMON_TOKEN"):
        log.error("IBKR_DAEMON_TOKEN not set in env / .env — refusing to start")
        release_lock()
        sys.exit(1)

    import uvicorn
    config = uvicorn.Config(
        app,
        host=_DEFAULT_HOST,
        port=_DEFAULT_PORT,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    def _stop(*_):
        log.info("signal received — shutting down")
        server.should_exit = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        server.run()
    finally:
        release_lock()


if __name__ == "__main__":
    main()
