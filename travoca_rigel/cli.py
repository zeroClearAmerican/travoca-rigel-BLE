"""travoca-rigel: control a Travoca Rigel fridge over BLE.

  travoca-rigel discover
  travoca-rigel status
  travoca-rigel watch                    live telemetry; survives link drops
  travoca-rigel power on|off
  travoca-rigel mode eco|max
  travoca-rigel units c|f
  travoca-rigel batt low|medium|high
  travoca-rigel deep <temp>              setpoints use the fridge's current units
  travoca-rigel shelf <temp>

The address comes from --address, $TRAVOCA_ADDRESS, or discovery (when
exactly one fridge is in range). Only one BLE client can be connected to the
fridge at a time, so close the phone app first.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time

from travoca_rigel import protocol as P
from travoca_rigel.fridge import Fridge, NotAcknowledged, NotConnected, discover

SETTERS = {
    "power": (P.POWER, {"on": 1, "off": 0}),
    "mode": (P.MODE, {"eco": 1, "max": 2}),
    "units": (P.UNITS, {"c": 1, "f": 2}),
    "batt": (P.BATT_PROT, {"low": 1, "medium": 2, "med": 2, "high": 3}),
    "deep": (P.DEEP_SET, None),
    "shelf": (P.SHELF_SET, None),
}


def ts() -> str:
    return time.strftime("%H:%M:%S")


async def resolve_address(a) -> str:
    addr = a.address or os.environ.get("TRAVOCA_ADDRESS")
    if addr:
        return addr
    found = await discover(a.timeout)
    if len(found) != 1:
        raise SystemExit(f"found {len(found)} fridges {found}; pass --address" if found else
                         "no fridge found (is the phone app connected to it?)")
    return found[0]


async def run(a):
    if a.action == "discover":
        for addr in await discover(a.timeout):
            print(addr)
        return

    if a.action in SETTERS:
        if a.value is None:
            raise SystemExit(f"usage: travoca-rigel {a.action} <value>")
        cmd, names = SETTERS[a.action]
        try:
            value = names[a.value.lower()] if names else int(a.value)
        except (KeyError, ValueError):
            raise SystemExit(f"bad value {a.value!r} for {a.action}")

    addr = await resolve_address(a)
    last_raw = None

    def on_rx(data, st):
        nonlocal last_raw
        if st and a.action == "watch" and st.raw != last_raw:
            print(f"{ts()} {st.summary()}", flush=True)
        if st:
            last_raw = st.raw

    def on_link(up):
        if a.action == "watch":
            print(f"{ts()} link {'up' if up else 'DOWN, reconnecting'}", flush=True)

    fridge = Fridge(addr, on_rx=on_rx, on_link=on_link, reconnect=a.action == "watch",
                    scan_timeout=a.timeout,
                    on_tx=(lambda p: logging.debug("tx %r", p)) if a.verbose else None)
    try:
        await fridge.connect()
    except NotConnected as e:
        raise SystemExit(str(e))

    try:
        if a.action == "status":
            print((await fridge.status()).summary())
        elif a.action == "watch":
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for s in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(s, stop.set)
            while not stop.is_set():  # poll like the app does, every 10 s
                try:
                    await fridge.status()
                except (NotConnected, asyncio.TimeoutError) as e:
                    logging.info("poll skipped: %s", e or "timeout")
                try:
                    await asyncio.wait_for(stop.wait(), a.interval)
                except asyncio.TimeoutError:
                    pass
        else:
            try:
                st = await fridge.send(cmd, value)
            except P.CommandError as e:
                raise SystemExit(f"refused: {e}")
            except NotAcknowledged as e:
                raise SystemExit(f"not acknowledged: {e}")
            print(st.summary())
    finally:
        await fridge.close()


def main():
    p = argparse.ArgumentParser(prog="travoca-rigel", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["discover", "status", "watch", *SETTERS])
    p.add_argument("value", nargs="?")
    p.add_argument("--address", help="fridge BLE address (default: $TRAVOCA_ADDRESS or discovery)")
    p.add_argument("--timeout", type=float, default=20, help="scan/connect timeout, seconds")
    p.add_argument("--interval", type=float, default=10, help="watch poll interval, seconds")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging (incl. unknown status fields)")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("bleak").setLevel(logging.INFO if a.verbose else logging.WARNING)
    try:
        asyncio.run(run(a))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
