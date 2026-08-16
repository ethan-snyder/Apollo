"""
Apollo — async orchestrator.

Runs every enabled collector concurrently on its own cadence, plus a periodic
signal-computation task. One misbehaving source can't stall the others: each
collector owns its loop and swallows its own exceptions.
"""
from __future__ import annotations

import asyncio
import logging
import signal as signal_module
from datetime import datetime, timezone

from config import ASSETS, HTTP_CONCURRENCY
from collectors.base import REGISTRY, HttpClient
from scoring import aggregate_signal
from store import articles_for_signal, init_db, record_signal, stats

logger = logging.getLogger("DAEMON")

# Signals are computed over several horizons: a fast window for reaction, a
# slow one for regime.
SIGNAL_WINDOWS = [15, 60, 240, 1440]
SIGNAL_INTERVAL = 60


class Apollo:
    def __init__(self, only: list[str] | None = None,
                 exclude: list[str] | None = None):
        self.only = set(only) if only else None
        self.exclude = set(exclude or [])
        self.stop = asyncio.Event()
        self.collectors: list = []

    def _build(self, http: HttpClient) -> list:
        built = []
        for name, cls in REGISTRY.items():
            if self.only and name not in self.only:
                continue
            if name in self.exclude:
                continue
            try:
                c = cls(http)
            except Exception as exc:
                logger.error("could not construct collector %s: %s", name, exc)
                continue
            if not c.enabled:
                logger.info("collector %s disabled (missing credentials)", name)
                continue
            built.append(c)
        return built

    async def compute_signals(self) -> dict:
        """Roll scored articles into per-asset directional signals."""
        out: dict[str, dict] = {}
        for asset in ASSETS:
            for window in SIGNAL_WINDOWS:
                rows = await asyncio.to_thread(articles_for_signal, asset, window)
                agg = aggregate_signal(rows, window)
                await asyncio.to_thread(
                    record_signal, asset, window, agg["article_count"],
                    agg["net_impact"], agg["mean_sentiment"], agg["direction"],
                    agg["confidence"], agg["detail"])
                if window == 60:
                    out[asset] = agg
        return out

    async def _signal_loop(self) -> None:
        while not self.stop.is_set():
            try:
                sigs = await self.compute_signals()
                parts = [
                    f"{a}:{s['direction']}({s['net_impact']:+.2f},"
                    f"c={s['confidence']:.2f},n={s['article_count']})"
                    for a, s in sigs.items() if s["article_count"]
                ]
                if parts:
                    logger.info("1h signals — %s", "  ".join(parts))
            except Exception as exc:
                logger.error("signal computation failed: %s", exc)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=SIGNAL_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def _heartbeat(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=300)
                return
            except asyncio.TimeoutError:
                pass
            try:
                s = await asyncio.to_thread(stats)
                logger.info("heartbeat — %d articles total, %d in last hour",
                            s["total_articles"], s["last_hour"])
            except Exception as exc:
                logger.error("heartbeat failed: %s", exc)

    async def run(self) -> None:
        await asyncio.to_thread(init_db)

        async with HttpClient(concurrency=HTTP_CONCURRENCY) as http:
            self.collectors = self._build(http)
            if not self.collectors:
                logger.error("no collectors enabled — nothing to do")
                return

            logger.info("starting %d collectors: %s", len(self.collectors),
                        ", ".join(c.name for c in self.collectors))

            tasks = [asyncio.create_task(c.run_forever(self.stop), name=c.name)
                     for c in self.collectors]
            tasks.append(asyncio.create_task(self._signal_loop(), name="signals"))
            tasks.append(asyncio.create_task(self._heartbeat(), name="heartbeat"))

            self._install_signal_handlers()

            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                logger.info("shutting down")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal_module.SIGINT, signal_module.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except NotImplementedError:
                # Windows: KeyboardInterrupt is handled by the caller instead.
                pass


async def run_once(only: list[str] | None = None) -> dict:
    """Single pass over every collector — used by the CLI's 'refresh now'."""
    await asyncio.to_thread(init_db)
    results: dict[str, tuple[int, int]] = {}
    async with HttpClient(concurrency=HTTP_CONCURRENCY) as http:
        app = Apollo(only=only)
        collectors = app._build(http)
        # RSS needs its first tick to consider every feed due, which it is.
        outs = await asyncio.gather(
            *(c.run_once() for c in collectors), return_exceptions=True)
        for c, r in zip(collectors, outs):
            results[c.name] = (0, 0) if isinstance(r, Exception) else r
            if isinstance(r, Exception):
                logger.error("%s failed: %s", c.name, r)
        await app.compute_signals()
    return results


def main(only: list[str] | None = None, exclude: list[str] | None = None) -> None:
    app = Apollo(only=only, exclude=exclude)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logger.info("interrupted by user")
