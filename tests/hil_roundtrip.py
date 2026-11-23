#!/usr/bin/env python3
"""Hardware-in-the-loop test against a real fridge.

Round trip (each step must be confirmed by a status line from the fridge,
either the direct reply or later telemetry):

  power on (from off) -> toggle eco/max and back -> units C -> units F
  -> deep 35 F -> shelf 0 F -> power off

If the fridge is on at the start, it is switched off first (setup step).

--reconnect additionally forces the BLE link down mid-test (via BlueZ,
Linux only) and checks the library reconnects and keeps working.

The phone app must be disconnected. Exit code 0 = all steps passed.

  python tests/hil_roundtrip.py [--address MAC] [--reconnect] [-v]
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from travoca_rigel import protocol as P  # noqa: E402
from travoca_rigel.fridge import Fridge, discover  # noqa: E402

DEEP_TARGET_F = 35
SHELF_TARGET_F = 0


def color(ok: bool) -> str:
    return "\033[1;32mPASS\033[0m" if ok else "\033[1;31mFAIL\033[0m"


async def run(a) -> bool:
    addr = a.address or os.environ.get("TRAVOCA_ADDRESS")
    if not addr:
        found = await discover(a.timeout)
        if len(found) != 1:
            raise SystemExit(f"found {found or 'no fridges'}; pass --address "
                             "(is the phone app connected to it?)")
        addr = found[0]

    log_path = ROOT / "captures" / f"hil_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    log_path.parent.mkdir(exist_ok=True)
    logf = log_path.open("w", buffering=1)

    def rec(**kw):
        logf.write(json.dumps({"ts": round(time.time(), 6), **kw}) + "\n")

    link_events: list[bool] = []
    fridge = Fridge(addr,
                    on_tx=lambda p: rec(dir="pc->fridge", hex=p.hex()),
                    on_rx=lambda d, st: rec(dir="fridge->pc", hex=d.hex()),
                    on_link=lambda up: (link_events.append(up), rec(link=up)),
                    backoff_initial=1.0, scan_timeout=a.timeout)
    results: list[tuple[str, bool]] = []

    async def step(name: str, cmd: int, value: int) -> P.Status:
        t0 = time.monotonic()
        rec(step=name)
        try:
            st = await fridge.send(cmd, value)
            assert P.applied(st, cmd, value), st.raw
        except Exception as e:
            results.append((name, False))
            print(f"{color(False)} {name}: {type(e).__name__}: {e}")
            raise
        results.append((name, True))
        print(f"{color(True)} {name:24} {time.monotonic() - t0:5.2f}s  {st.summary()}")
        return st

    async def force_drop():
        rec(step="force link drop")
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "disconnect", addr,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        t0 = time.monotonic()
        while fridge.is_connected and time.monotonic() - t0 < 10:
            await asyncio.sleep(0.1)
        dropped = not fridge.is_connected
        results.append(("link drop detected", dropped))
        print(f"{color(dropped)} link drop detected")
        assert dropped, "link never went down"

    expected = 0
    try:
        await fridge.connect()
        st = await fridge.status()
        print(f"initial: {st.summary()}")

        if st.power:
            await step("setup: power off", P.POWER, 0)
            expected += 1
        st = await step("power on", P.POWER, 1)

        original_mode = 1 if st.mode == "eco" else 2
        other_mode = 3 - original_mode
        await step(f"mode -> {P.MODE_NAMES[other_mode]}", P.MODE, other_mode)

        if a.reconnect:
            await force_drop()
            expected += 1
            # this call has to wait for the background reconnect
        await step(f"mode -> {P.MODE_NAMES[original_mode]}", P.MODE, original_mode)

        await step("units -> C", P.UNITS, 1)
        await step("units -> F", P.UNITS, 2)
        await step(f"deep -> {DEEP_TARGET_F} F", P.DEEP_SET, DEEP_TARGET_F)
        await step(f"shelf -> {SHELF_TARGET_F} F", P.SHELF_SET, SHELF_TARGET_F)
        await step("power off", P.POWER, 0)
    except Exception as e:
        if not results or results[-1][1]:
            results.append(("(outside step)", False))
            print(f"\033[1;31mERROR\033[0m {type(e).__name__}: {e}")
    finally:
        await fridge.close()
        logf.close()

    expected += 8
    if a.reconnect:
        ok_rc = link_events[:3] == [True, False, True]
        results.append(("reconnected", ok_rc))
        print(f"{color(ok_rc)} reconnected (link events: {link_events})")
        expected += 1
    passed = sum(ok for _, ok in results)
    ok = passed == len(results) == expected
    print(("\033[1;32mALL PASSED" if ok else "\033[1;31mFAILED") +
          f"\033[0m ({passed}/{expected} checks)  log: {log_path}")
    return ok


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--address", help="fridge BLE address (default: $TRAVOCA_ADDRESS or discovery)")
    p.add_argument("--timeout", type=float, default=20)
    p.add_argument("--reconnect", action="store_true", help="also force a link drop and verify reconnect")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logs (incl. unknown status fields)")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("bleak").setLevel(logging.INFO if a.verbose else logging.WARNING)
    sys.exit(0 if asyncio.run(run(a)) else 1)


if __name__ == "__main__":
    main()
