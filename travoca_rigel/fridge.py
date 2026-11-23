"""Async BLE client for the fridge (CPython + bleak).

- Every command is validated by protocol.build() against a fresh status.
- send() waits until a status line shows the change applied.
- If the link drops, it reconnects in the background with exponential
  backoff; calls made meanwhile wait for the link (up to `link_timeout`).
"""

import asyncio
import logging
import random
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from travoca_rigel import protocol as P

log = logging.getLogger("travoca_rigel.fridge")


class NotAcknowledged(RuntimeError):
    """The fridge never reported the requested change."""


class NotConnected(RuntimeError):
    """No link to the fridge within the allowed time."""


async def discover(timeout: float = 10.0) -> list[str]:
    """Addresses of nearby fridges (advertised name 'travoca' + FFF0)."""
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return [d.address for d, adv in found.values()
            if (adv.local_name or "") == P.ADV_NAME and P.SERVICE_UUID in adv.service_uuids]


class Fridge:
    def __init__(self, address: str, *,
                 on_tx: Callable[[bytes], None] | None = None,
                 on_rx: Callable[[bytes, P.Status | None], None] | None = None,
                 on_link: Callable[[bool], None] | None = None,
                 reconnect: bool = True,
                 backoff_initial: float = 1.0,
                 backoff_max: float = 60.0,
                 scan_timeout: float = 20.0,
                 link_timeout: float = 30.0):
        self.address = address
        self.on_tx, self.on_rx, self.on_link = on_tx, on_rx, on_link
        self.reconnect = reconnect
        self.backoff_initial, self.backoff_max = backoff_initial, backoff_max
        self.scan_timeout = scan_timeout
        self.link_timeout = link_timeout
        self.latest: P.Status | None = None
        self._client: BleakClient | None = None
        self._up = asyncio.Event()
        self._closing = False
        self._reconnect_task: asyncio.Task | None = None
        self._waiters: list[tuple[Callable[[P.Status], bool], asyncio.Future]] = []
        self._lock = asyncio.Lock()  # one command exchange at a time

    # --- connection management ----------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._up.is_set()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def connect(self, timeout: float | None = None):
        """Initial connection, retried with backoff. Raises NotConnected if
        not up within `timeout` (default: one scan_timeout + a retry)."""
        timeout = timeout if timeout is not None else self.scan_timeout * 2
        try:
            await asyncio.wait_for(self._connect_with_backoff(), timeout)
        except asyncio.TimeoutError:
            raise NotConnected(f"could not connect to {self.address} within {timeout:.0f}s "
                               "(is another device, e.g. the phone app, connected to it?)")

    async def close(self):
        self._closing = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._set_link(False)

    async def _connect_once(self):
        client = BleakClient(self.address, disconnected_callback=self._on_disconnect,
                             timeout=self.scan_timeout)
        await client.connect()
        try:
            await client.start_notify(P.STATUS_CHAR, self._on_notify)
        except Exception:
            await client.disconnect()
            raise
        self._client = client
        self._set_link(True)

    async def _connect_with_backoff(self):
        delay = self.backoff_initial
        while not self._closing:
            try:
                await self._connect_once()
                return
            except (BleakError, asyncio.TimeoutError, OSError) as e:
                # Jitter so several clients don't retry in lockstep.
                wait = delay * random.uniform(0.8, 1.2)
                log.info("connect to %s failed (%s); retrying in %.1fs", self.address, e or type(e).__name__, wait)
                await asyncio.sleep(wait)
                delay = min(delay * 2, self.backoff_max)

    def _on_disconnect(self, client: BleakClient):
        if client is not self._client:
            return
        self._set_link(False)
        if self._closing or not self.reconnect:
            return
        log.warning("link to %s lost; reconnecting", self.address)
        if not self._reconnect_task or self._reconnect_task.done():
            self._reconnect_task = asyncio.get_running_loop().create_task(self._connect_with_backoff())

    def _set_link(self, up: bool):
        if up == self._up.is_set():
            return
        (self._up.set if up else self._up.clear)()
        if not up:
            for _, fut in self._waiters:
                if not fut.done():
                    fut.set_exception(NotConnected("link lost"))
        if self.on_link:
            self.on_link(up)

    async def _await_link(self):
        if self._closing:
            raise NotConnected("closed")
        try:
            await asyncio.wait_for(self._up.wait(), self.link_timeout)
        except asyncio.TimeoutError:
            raise NotConnected(f"no link to {self.address} for {self.link_timeout:.0f}s")

    # --- traffic ---------------------------------------------------------------

    def _on_notify(self, _ch, data: bytearray):
        data = bytes(data)
        st = P.decode(data)
        if self.on_rx:
            self.on_rx(data, st)
        if not st:
            return
        self.latest = st
        for pred, fut in list(self._waiters):
            if not fut.done() and pred(st):
                fut.set_result(st)

    async def _write(self, payload: bytes):
        await self._await_link()
        if self.on_tx:
            self.on_tx(payload)
        await self._client.write_gatt_char(P.CMD_CHAR, payload, response=True)

    async def _exchange(self, payload: bytes, pred: Callable[[P.Status], bool], timeout: float) -> P.Status:
        """Write payload and wait for a status line matching pred."""
        fut = asyncio.get_running_loop().create_future()
        entry = (pred, fut)
        self._waiters.append(entry)
        try:
            await self._write(payload)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._waiters.remove(entry)

    async def status(self, timeout: float = 5.0) -> P.Status:
        """Request and return a fresh status line."""
        async with self._lock:
            return await self._exchange(P.query(), lambda st: True, timeout)

    async def send(self, cmd: int, value: int, timeout: float = 5.0) -> P.Status:
        """Validate against fresh state, send, and wait until a status line
        (the direct reply or later telemetry) shows the change applied.
        If nothing confirms it within `timeout`, query once more before
        raising NotAcknowledged."""
        async with self._lock:
            current = await self._exchange(P.query(), lambda st: True, timeout)
            payload = P.build(cmd, value, current)  # raises CommandError
            pred = lambda st: P.applied(st, cmd, value)
            try:
                return await self._exchange(payload, pred, timeout)
            except asyncio.TimeoutError:
                pass
            st = await self._exchange(P.query(), lambda st: True, timeout)
            if pred(st):
                return st
            raise NotAcknowledged(f"S{cmd:02d}={value} not reflected in status: {st.raw}")

    # convenience setters
    async def set_power(self, on: bool):
        return await self.send(P.POWER, int(on))

    async def set_mode(self, mode: str):
        return await self.send(P.MODE, {"eco": 1, "max": 2}[mode])

    async def set_units(self, units: str):
        return await self.send(P.UNITS, {"C": 1, "F": 2}[units.upper()])

    async def set_battery_protection(self, level: str):
        return await self.send(P.BATT_PROT, {"low": 1, "medium": 2, "high": 3}[level])

    async def set_deep(self, temp: int):
        return await self.send(P.DEEP_SET, temp)

    async def set_shelf(self, temp: int):
        return await self.send(P.SHELF_SET, temp)
