"""Constant-arrival-rate load generator for the Sentinel shop (FR-TS-02).

- Default 10 rps (clamped to [5, 20] rps)
- 70% POST /orders (create), 30% GET /orders/{id} or GET /orders (read)
- Fixed PRNG seed for repeatable evaluation baselines
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import time
from collections import deque

import httpx

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("loadgen")

SKUS = ("SKU-WIDGET-01", "SKU-GADGET-02", "SKU-CABLE-03", "SKU-MOUNT-04", "SKU-CASE-05")


def build_order_payload(rng: random.Random) -> dict[str, object]:
    # ~2/3 of orders carry 2+ line items so release/orders-itemised-tax triggers ~66% 500s
    item_count = 1 if rng.random() < 0.33 else rng.randint(2, 4)
    items = [
        {
            "sku": rng.choice(SKUS),
            "qty": rng.randint(1, 3),
            "unit_price_cents": rng.choice([1299, 2499, 4999, 7500, 11900]),
        }
        for _ in range(item_count)
    ]
    card_number = "4000000000000002" if rng.random() < 0.02 else "4242424242424242"
    return {
        "customer_id": f"cust-{rng.randint(1, 500):04d}",
        "items": items,
        "currency": "USD",
        "card_number": card_number,
    }


def clamp_rps(rps: float) -> float:
    return max(5.0, min(20.0, float(rps)))


async def run_loadgen(
    *,
    orders_url: str,
    rps: float = 10.0,
    seed: int = 42,
    stop_event: asyncio.Event | None = None,
) -> None:
    clamped_rps = clamp_rps(rps)
    interval = 1.0 / clamped_rps
    rng = random.Random(seed)
    recent_ids: deque[str] = deque(maxlen=200)
    stop = stop_event or asyncio.Event()

    async with httpx.AsyncClient(base_url=orders_url.rstrip("/"), timeout=4.0) as client:
        logger.info("loadgen_started url=%s rps=%.1f seed=%d", orders_url, clamped_rps, seed)
        next_tick = time.perf_counter()
        while not stop.is_set():
            now = time.perf_counter()
            if now < next_tick:
                await asyncio.sleep(next_tick - now)
            next_tick += interval

            async def _one_request() -> None:
                try:
                    if rng.random() < 0.70 or not recent_ids:
                        resp = await client.post("/orders", json=build_order_payload(rng))
                        if resp.status_code == 201:
                            oid = resp.json().get("id")
                            if oid:
                                recent_ids.append(str(oid))
                    else:
                        if rng.random() < 0.7:
                            oid = rng.choice(list(recent_ids))
                            await client.get(f"/orders/{oid}")
                        else:
                            await client.get("/orders", params={"limit": 10})
                except Exception:
                    pass

            asyncio.create_task(_one_request())


def main() -> None:
    orders_url = os.getenv("LOADGEN_ORDERS_URL", "http://orders:8001")
    rps = float(os.getenv("LOADGEN_RPS", "10"))
    seed = int(os.getenv("LOADGEN_SEED", "42"))
    stop = asyncio.Event()

    async def _runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await run_loadgen(orders_url=orders_url, rps=rps, seed=seed, stop_event=stop)

    asyncio.run(_runner())


if __name__ == "__main__":
    main()
