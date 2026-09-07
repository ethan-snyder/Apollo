"""
Apollo — paper trading engine.

Simulated trading only. No broker is contacted, no orders are placed, no real
money moves at any point. Every "fill" is an arithmetic operation against a
live price quote.

Each session starts with $10,000 (configurable), evaluates the current signal
on a fixed interval, and sizes positions from signal strength and confidence.
Runs persist to `paper_runs` / `paper_trades` / `paper_equity` so a session can
be analyzed long after it ends.

Duration is arbitrary: `--duration 1h` for a quick trial, `7d` for a longer
soak, or `0` to run until interrupted.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal as signal_module
from datetime import datetime, timedelta, timezone

from config import ASSETS, CRYPTO_ASSETS, HTTP_CONCURRENCY
from collectors.base import HttpClient
from scoring import aggregate_signal
from store import (articles_for_signal, create_paper_run, finish_paper_run,
                   init_db, record_equity, record_trade, utcnow)

logger = logging.getLogger("PAPER")

COINBASE_TICKER = "https://api.exchange.coinbase.com/products/{}/ticker"

STARTING_CASH = 10_000.0
FEE_BPS = 10.0                 # 0.10% per side
DEFAULT_INTERVAL = 60          # seconds between evaluations
DEFAULT_WINDOW_MIN = 60        # trailing news window for the signal

# Sizing / entry rules
MIN_CONFIDENCE = 0.20
ENTRY_THRESHOLD = 0.30         # |net_impact| needed to take a position
MAX_ASSET_ALLOCATION = 0.40    # cap per asset, as a fraction of equity
REBALANCE_TOLERANCE = 0.05     # ignore target changes smaller than 5% of equity


def parse_duration(s: str | None) -> int:
    """
    '1h' -> 3600, '30m' -> 1800, '7d' -> 604800, '0'/None -> 0 (unbounded).
    Bare numbers are read as seconds.
    """
    if not s:
        return 0
    s = str(s).strip().lower()
    if s in ("0", "inf", "forever", "none"):
        return 0
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if s[-1] in units:
        try:
            return int(float(s[:-1]) * units[s[-1]])
        except ValueError:
            return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


class Portfolio:
    """Cash + per-asset positions with average-cost basis for realized P&L."""

    def __init__(self, cash: float = STARTING_CASH):
        self.starting_cash = cash
        self.cash = cash
        self.positions: dict[str, float] = {}     # asset -> units
        self.cost_basis: dict[str, float] = {}    # asset -> avg entry price
        self.realized_pnl = 0.0
        self.fees_paid = 0.0

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(
            q * prices.get(a, 0.0) for a, q in self.positions.items())

    def position_value(self, asset: str, price: float) -> float:
        return self.positions.get(asset, 0.0) * price

    def apply_fill(self, asset: str, delta_units: float, price: float,
                   fee_rate: float) -> tuple[float, float]:
        """
        Apply a fill. Returns (fee, realized_pnl_from_this_fill).

        Realized P&L is booked only on the closing portion of a trade, using
        average cost. Adding to a position rolls the average instead.
        """
        notional = abs(delta_units * price)
        fee = notional * fee_rate
        realized = 0.0

        current = self.positions.get(asset, 0.0)
        basis = self.cost_basis.get(asset, price)

        # Closing or reducing (delta opposes current position)
        if current and (current > 0) != (delta_units > 0):
            closing = min(abs(delta_units), abs(current))
            direction = 1 if current > 0 else -1
            realized = closing * (price - basis) * direction
            self.realized_pnl += realized

        new_qty = current + delta_units
        if current == 0 or (current > 0) == (delta_units > 0):
            # Opening or adding — roll the average cost.
            total = abs(current) + abs(delta_units)
            if total:
                self.cost_basis[asset] = (
                    (abs(current) * basis + abs(delta_units) * price) / total)
        elif abs(new_qty) < 1e-12:
            self.cost_basis.pop(asset, None)

        self.cash -= delta_units * price
        self.cash -= fee
        self.fees_paid += fee

        if abs(new_qty) < 1e-12:
            self.positions.pop(asset, None)
            self.cost_basis.pop(asset, None)
        else:
            self.positions[asset] = new_qty

        return fee, realized

    def snapshot(self, prices: dict[str, float]) -> dict:
        return {
            a: {"qty": round(q, 8), "price": prices.get(a, 0.0),
                "value": round(q * prices.get(a, 0.0), 2)}
            for a, q in self.positions.items()
        }


class PaperTrader:
    def __init__(self, *, assets: list[str] | None = None,
                 starting_cash: float = STARTING_CASH,
                 duration: str | int = "1h",
                 interval: int = DEFAULT_INTERVAL,
                 window_min: int = DEFAULT_WINDOW_MIN,
                 allow_short: bool = False,
                 min_confidence: float = MIN_CONFIDENCE,
                 threshold: float = ENTRY_THRESHOLD,
                 max_allocation: float = MAX_ASSET_ALLOCATION,
                 fee_bps: float = FEE_BPS,
                 label: str = ""):
        self.assets = [a.upper() for a in (assets or CRYPTO_ASSETS)]
        self.portfolio = Portfolio(starting_cash)
        self.duration = (duration if isinstance(duration, int)
                         else parse_duration(duration))
        self.interval = max(5, int(interval))
        self.window_min = window_min
        self.allow_short = allow_short
        self.min_confidence = min_confidence
        self.threshold = threshold
        self.max_allocation = max_allocation
        self.fee_rate = fee_bps / 10_000.0
        self.label = label or f"paper-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
        self.run_id: int | None = None
        self.stop = asyncio.Event()
        self.ticks = 0

    # -- pricing ----------------------------------------------------------
    async def fetch_prices(self, http: HttpClient) -> dict[str, float]:
        """Live spot quotes from Coinbase (keyless)."""
        out: dict[str, float] = {}

        async def one(asset: str) -> None:
            pair = ASSETS.get(asset, {}).get("pair")
            if not pair:
                return
            try:
                data = await http.get_json(COINBASE_TICKER.format(pair))
                price = float(data.get("price") or 0)
                if price > 0:
                    out[asset] = price
            except Exception as exc:
                logger.warning("price fetch failed for %s: %s", asset, exc)

        await asyncio.gather(*(one(a) for a in self.assets))
        return out

    # -- signals ----------------------------------------------------------
    async def current_signals(self) -> dict[str, dict]:
        sigs: dict[str, dict] = {}
        for asset in self.assets:
            rows = await asyncio.to_thread(
                articles_for_signal, asset, self.window_min)
            sigs[asset] = aggregate_signal(rows, self.window_min)
        return sigs

    # -- trading ----------------------------------------------------------
    def target_fraction(self, sig: dict) -> float:
        """
        Map a signal to a target allocation as a fraction of equity.

        Scales with both signal strength and confidence, capped at
        max_allocation, so a weak-but-confident read takes a smaller position
        than a strong one.
        """
        if sig["confidence"] < self.min_confidence:
            return 0.0
        net = sig["net_impact"]
        if abs(net) < self.threshold:
            return 0.0
        strength = min(1.0, abs(net) / 2.0)
        frac = self.max_allocation * strength * min(1.0, sig["confidence"] / 0.6)
        if net < 0:
            return -frac if self.allow_short else 0.0
        return frac

    async def evaluate(self, prices: dict[str, float],
                       signals: dict[str, dict]) -> None:
        ts = utcnow()
        equity = self.portfolio.equity(prices)

        for asset in self.assets:
            price = prices.get(asset)
            sig = signals.get(asset)
            if not price or not sig:
                continue

            target_frac = self.target_fraction(sig)
            target_units = (target_frac * equity) / price
            current_units = self.portfolio.positions.get(asset, 0.0)
            delta = target_units - current_units

            if abs(delta * price) < equity * REBALANCE_TOLERANCE:
                continue

            # Never let a buy exceed available cash (no simulated margin).
            if delta > 0:
                affordable = self.portfolio.cash / (price * (1 + self.fee_rate))
                delta = min(delta, max(0.0, affordable))
                if delta * price < equity * REBALANCE_TOLERANCE:
                    continue

            fee, realized = self.portfolio.apply_fill(
                asset, delta, price, self.fee_rate)

            side = "BUY" if delta > 0 else "SELL"
            reason = (f"{sig['direction']} net={sig['net_impact']:+.3f} "
                      f"conf={sig['confidence']:.2f} n={sig['article_count']}")
            await asyncio.to_thread(
                record_trade, self.run_id, ts, asset, side, abs(delta), price,
                fee, reason, sig["direction"], sig["net_impact"],
                sig["confidence"], self.portfolio.cash,
                self.portfolio.positions.get(asset, 0.0), realized)

            logger.info("%s %s %.6f @ $%,.2f | %s", side, asset, abs(delta),
                        price, reason)

        equity = self.portfolio.equity(prices)
        await asyncio.to_thread(
            record_equity, self.run_id, ts, equity, self.portfolio.cash,
            self.portfolio.snapshot(prices))

    # -- lifecycle --------------------------------------------------------
    def _summary(self, prices: dict[str, float]) -> dict:
        equity = self.portfolio.equity(prices)
        start = self.portfolio.starting_cash
        return {
            "starting_cash": start,
            "ending_equity": round(equity, 2),
            "return_pct": round(100 * (equity / start - 1), 3),
            "realized_pnl": round(self.portfolio.realized_pnl, 2),
            "unrealized_pnl": round(
                equity - self.portfolio.cash
                - sum(self.portfolio.cost_basis.get(a, 0) * q
                      for a, q in self.portfolio.positions.items()), 2),
            "fees_paid": round(self.portfolio.fees_paid, 2),
            "ticks": self.ticks,
            "final_positions": self.portfolio.snapshot(prices),
            "final_cash": round(self.portfolio.cash, 2),
        }

    async def run(self) -> dict:
        await asyncio.to_thread(init_db)

        params = {
            "assets": self.assets, "duration_seconds": self.duration,
            "interval_seconds": self.interval, "window_min": self.window_min,
            "allow_short": self.allow_short, "min_confidence": self.min_confidence,
            "threshold": self.threshold, "max_allocation": self.max_allocation,
            "fee_bps": self.fee_rate * 10_000,
        }
        self.run_id = await asyncio.to_thread(
            create_paper_run, self.label, self.portfolio.starting_cash, params)

        deadline = (datetime.now(timezone.utc) + timedelta(seconds=self.duration)
                    if self.duration else None)

        logger.info("PAPER TRADING (simulated, no real orders) — run #%d '%s'",
                    self.run_id, self.label)
        logger.info("  cash=$%,.2f assets=%s duration=%s interval=%ds",
                    self.portfolio.starting_cash, ",".join(self.assets),
                    f"{self.duration}s" if self.duration else "unbounded",
                    self.interval)

        self._install_signal_handlers()
        status = "completed"
        prices: dict[str, float] = {}

        try:
            async with HttpClient(concurrency=HTTP_CONCURRENCY) as http:
                while not self.stop.is_set():
                    if deadline and datetime.now(timezone.utc) >= deadline:
                        break
                    try:
                        prices = await self.fetch_prices(http) or prices
                        if prices:
                            signals = await self.current_signals()
                            await self.evaluate(prices, signals)
                            self.ticks += 1
                            if self.ticks % 10 == 1:
                                eq = self.portfolio.equity(prices)
                                logger.info(
                                    "tick %d | equity $%,.2f (%+.2f%%) cash $%,.2f",
                                    self.ticks, eq,
                                    100 * (eq / self.portfolio.starting_cash - 1),
                                    self.portfolio.cash)
                        else:
                            logger.warning("no prices this tick — skipping")
                    except Exception as exc:
                        logger.error("tick failed: %s", exc)

                    try:
                        await asyncio.wait_for(self.stop.wait(),
                                               timeout=self.interval)
                    except asyncio.TimeoutError:
                        pass
        except KeyboardInterrupt:
            status = "stopped"
        except Exception as exc:
            logger.error("run failed: %s", exc)
            status = "error"

        summary = self._summary(prices)
        await asyncio.to_thread(
            finish_paper_run, self.run_id, status,
            summary["ending_equity"], summary)

        logger.info("run #%d %s — equity $%,.2f (%+.2f%%), %d ticks, "
                    "$%,.2f fees", self.run_id, status,
                    summary["ending_equity"], summary["return_pct"],
                    summary["ticks"], summary["fees_paid"])
        summary["run_id"] = self.run_id
        summary["status"] = status
        return summary

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal_module.SIGINT, signal_module.SIGTERM):
                try:
                    loop.add_signal_handler(sig, self.stop.set)
                except (NotImplementedError, AttributeError):
                    pass          # Windows — KeyboardInterrupt handles it
        except RuntimeError:
            pass


def main(**kwargs) -> dict:
    trader = PaperTrader(**kwargs)
    try:
        return asyncio.run(trader.run())
    except KeyboardInterrupt:
        logger.info("interrupted")
        return {"status": "stopped"}
