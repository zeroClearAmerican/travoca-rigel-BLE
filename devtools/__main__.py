#!/usr/bin/env python3
"""Dev tools for reverse engineering the Travoca Rigel BLE protocol.
Not part of the control library (see travoca_rigel/). Linux + BlueZ only.

  python -m devtools scan      list nearby BLE devices + advertisement data
  python -m devtools profile   dump a device's advertisement + GATT table to JSON
  python -m devtools relay     MITM: clone the fridge for the phone app,
                               forward + log all traffic
  python -m devtools client    interactive raw GATT client
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

from devtools.capture import Capture, console, hexdump, short_uuid

ROOT = Path(__file__).resolve().parent.parent
PROFILES = ROOT / "profiles"
CAPTURES = ROOT / "captures"
log = logging.getLogger("devtools")


# ----------------------------------------------------------------- helpers

def adv_to_dict(adv) -> dict:
    return {
        "local_name": adv.local_name,
        "service_uuids": list(adv.service_uuids),
        "manufacturer_data": {str(k): v.hex() for k, v in adv.manufacturer_data.items()},
        "service_data": {k: v.hex() for k, v in adv.service_data.items()},
        "tx_power": adv.tx_power,
        "rssi": adv.rssi,
    }


async def find(target: str, timeout: float):
    """Find a device by MAC address or (case-insensitive) name substring."""
    t = target.lower()
    is_addr = len(t) == 17 and t.count(":") == 5

    def match(d, adv):
        if is_addr:
            return d.address.lower() == t
        return t in (adv.local_name or d.name or "").lower()

    found = asyncio.get_running_loop().create_future()

    def cb(d, adv):
        if match(d, adv) and not found.done():
            found.set_result((d, adv))

    async with BleakScanner(cb):
        try:
            return await asyncio.wait_for(found, timeout)
        except asyncio.TimeoutError:
            raise SystemExit(f"device {target!r} not found in {timeout}s "
                             "(is the phone app still connected to it?)")


def latest_profile() -> Path | None:
    ps = sorted(PROFILES.glob("*.json"), key=lambda p: p.stat().st_mtime)
    return ps[-1] if ps else None


def load_profile(arg: str | None) -> dict:
    p = Path(arg) if arg else latest_profile()
    if not p or not p.exists():
        raise SystemExit("no profile; run `python -m devtools profile <name-or-mac>` first")
    console.print(f"[dim]profile <- {p}[/]")
    return json.loads(p.read_text())


def resolve_char(client: BleakClient, spec: str) -> BleakGATTCharacteristic:
    """spec is a decimal handle, 0x-handle, short uuid (fff1) or full uuid."""
    try:
        h = int(spec, 0)
        c = client.services.get_characteristic(h)
        if c:
            return c
    except ValueError:
        pass
    s = spec.lower()
    for svc in client.services:
        for c in svc.characteristics:
            if c.uuid.lower() == s or short_uuid(c.uuid) == s:
                return c
    raise ValueError(f"no characteristic {spec!r}")


# ----------------------------------------------------------------- scan

async def cmd_scan(a):
    console.print(f"scanning {a.time}s ...")
    res = await BleakScanner.discover(timeout=a.time, return_adv=True)
    rows = sorted(res.values(), key=lambda x: -x[1].rssi)
    for d, adv in rows:
        name = adv.local_name or d.name
        if not name and not a.all:
            continue
        console.print(f"[bold]{d.address}[/] {adv.rssi:4} dBm  [yellow]{name}[/]")
        for u in adv.service_uuids:
            console.print(f"    uuid  {u}")
        for k, v in adv.manufacturer_data.items():
            console.print(f"    mfr   0x{k:04x}: {hexdump(v)}")
        for k, v in adv.service_data.items():
            console.print(f"    sdata {k}: {hexdump(v)}")


# ----------------------------------------------------------------- profile

async def cmd_profile(a):
    dev, adv = await find(a.target, a.timeout)
    console.print(f"found {dev.address} {adv.local_name!r}, connecting ...")
    async with BleakClient(dev) as client:
        prof = {
            "name": adv.local_name or dev.name,
            "address": dev.address,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "mtu": client.mtu_size,
            "advertisement": adv_to_dict(adv),
            "services": [],
        }
        for svc in client.services:
            s = {"uuid": svc.uuid, "handle": svc.handle,
                 "description": svc.description, "characteristics": []}
            console.print(f"[bold]service {svc.uuid}[/] ({svc.description}) h={svc.handle}")
            for ch in svc.characteristics:
                c = {"uuid": ch.uuid, "handle": ch.handle,
                     "properties": list(ch.properties),
                     "description": ch.description, "value": None, "descriptors": []}
                if "read" in ch.properties:
                    try:
                        c["value"] = (await client.read_gatt_char(ch)).hex()
                    except Exception as e:
                        c["read_error"] = str(e)
                console.print(f"  char {ch.uuid} h={ch.handle} {','.join(ch.properties)}"
                              f"  [dim]{ch.description}[/]"
                              + (f"\n      value: {c['value']}  |{bytes.fromhex(c['value']).decode('ascii', 'replace')}|"
                                 if c["value"] else ""))
                for d in ch.descriptors:
                    dd = {"uuid": d.uuid, "handle": d.handle, "value": None}
                    try:
                        dd["value"] = (await client.read_gatt_descriptor(d.handle)).hex()
                    except Exception as e:
                        dd["read_error"] = str(e)
                    console.print(f"      desc {d.uuid} h={d.handle} = {dd['value']}")
                    c["descriptors"].append(dd)
                s["characteristics"].append(c)
            prof["services"].append(s)

    PROFILES.mkdir(exist_ok=True)
    safe = (prof["name"] or dev.address).replace(" ", "_").replace("/", "_")
    out = PROFILES / f"{safe}_{dev.address.replace(':', '')}.json"
    out.write_text(json.dumps(prof, indent=2))
    console.print(f"[green]saved {out}[/]")


# ----------------------------------------------------------------- relay

class Relay:
    def __init__(self, profile: dict, cap: Capture, eager_notify: bool):
        self.profile = profile
        self.cap = cap
        self.eager_notify = eager_notify
        self.client: BleakClient | None = None
        self.periph = None
        self.subscribed: set[int] = set()   # handles subscribed on fridge
        self.phone_wants: set[int] = set()  # handles the phone subscribed
        self.connected = asyncio.Event()
        self.closing = False
        self._connect_lock = asyncio.Lock()

    # --- fridge side
    async def connect_fridge(self):
        if self._connect_lock.locked():
            return  # another reconnect loop is already running
        async with self._connect_lock:
            await self._connect_loop()

    async def _connect_loop(self):
        target = self.profile["address"]
        while not self.closing:
            try:
                if self.client:  # clear any half-open BlueZ state
                    try:
                        self.client._disconnected_callback = None
                        await self.client.disconnect()
                    except Exception:
                        pass
                dev, _ = await find(target, 30)
                self.client = BleakClient(dev, disconnected_callback=self._on_fridge_dc)
                await self.client.connect()
                self.cap.event("info", "fridge-up", mtu=self.client.mtu_size)
                self.subscribed.clear()
                wanted = set(self.phone_wants)
                if self.eager_notify:
                    wanted |= {h for h, c in self.periph.chars.items()
                               if {"notify", "indicate"} & set(c._flags)}
                for h in wanted:
                    await self._fridge_subscribe(h)
                self.connected.set()
                return
            except (Exception, SystemExit) as e:
                self.cap.event("info", "fridge-retry", err=str(e))
                await asyncio.sleep(2)

    def _on_fridge_dc(self, _client):
        was_up = self.connected.is_set()
        self.connected.clear()
        self.cap.event("info", "fridge-down")
        # Only reconnect from here after an established link drops; failures
        # during connect() are retried by the loop itself.
        if was_up and not self.closing:
            asyncio.get_running_loop().create_task(self.connect_fridge())

    async def _fridge_subscribe(self, handle: int):
        if handle in self.subscribed:
            return

        def cb(ch: BleakGATTCharacteristic, data: bytearray, h=handle):
            data = bytes(data)
            to_phone = h in self.phone_wants
            self.cap.event("fridge->phone" if to_phone else "fridge->pc",
                           "notify", h, data)
            if to_phone:
                self.periph.push(h, data)

        await self.client.start_notify(handle, cb)
        self.subscribed.add(handle)

    async def _ready(self):
        await asyncio.wait_for(self.connected.wait(), 10)

    # --- callbacks from the phone (via peripheral)
    async def on_write(self, handle, data, opts):
        kind = opts.get("type", "request")
        self.cap.event("phone->fridge", "write" if kind != "command" else "write-cmd",
                       handle, data)
        await self._ready()
        await self.client.write_gatt_char(handle, data, response=(kind != "command"))

    async def on_read(self, handle, opts):
        await self._ready()
        data = bytes(await self.client.read_gatt_char(handle))
        self.cap.event("fridge->phone", "read", handle, data, offset=opts.get("offset", 0))
        return data[opts.get("offset", 0):]

    async def on_subscribe(self, handle, enable):
        self.cap.event("phone->fridge", "subscribe" if enable else "unsubscribe", handle)
        if enable:
            self.phone_wants.add(handle)
            await self._ready()
            await self._fridge_subscribe(handle)
        else:
            self.phone_wants.discard(handle)
            # keep the fridge-side subscription so we still capture traffic

    async def on_read_descriptor(self, handle):
        await self._ready()
        data = bytes(await self.client.read_gatt_descriptor(handle))
        self.cap.event("fridge->phone", "read-desc", handle, data)
        return data

    async def on_write_descriptor(self, handle, data):
        self.cap.event("phone->fridge", "write-desc", handle, data)
        await self._ready()
        await self.client.write_gatt_descriptor(handle, data)


async def watch_connections(bus, fridge_addr: str, cap: Capture):
    """Log phone connect/disconnect by watching BlueZ Device1.Connected."""
    from dbus_fast import Message, MessageType

    await bus.call(Message(
        destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus", member="AddMatch", signature="s",
        body=["type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',"
              "member='PropertiesChanged',arg0='org.bluez.Device1'"]))
    fridge_suffix = fridge_addr.replace(":", "_").upper()

    def handler(msg):
        if msg.message_type != MessageType.SIGNAL or msg.member != "PropertiesChanged":
            return
        if not msg.body or msg.body[0] != "org.bluez.Device1":
            return
        changed = msg.body[1]
        if "Connected" in changed and not msg.path.upper().endswith(fridge_suffix):
            addr = msg.path.rsplit("dev_", 1)[-1].replace("_", ":")
            cap.event("info", "phone-up" if changed["Connected"].value else "phone-down",
                      addr=addr)

    bus.add_message_handler(handler)


async def cmd_relay(a):
    from devtools.peripheral import Peripheral

    profile = load_profile(a.profile)
    out = CAPTURES / f"relay_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    cap = Capture(out, profile)
    relay = Relay(profile, cap, a.eager_notify)
    periph = Peripheral(profile, relay, a.adapter)
    relay.periph = periph

    if a.adv_only:
        console.print("[yellow]adv-only: NOT connecting to the fridge[/]")
    else:
        # Connect to the fridge FIRST: while we hold the connection it stops
        # advertising, so the phone can only find our clone.
        console.print(f"connecting to fridge {profile['address']} ...")
        await relay.connect_fridge()
    await periph.start(spoof_name=not a.no_spoof_name)
    await watch_connections(periph.bus, profile["address"], cap)
    console.print("[bold green]relay up[/] - open the app on the phone. Ctrl-C to stop.")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, stop.set)
    await stop.wait()

    console.print("shutting down ...")
    relay.closing = True
    await periph.stop()
    if relay.client and relay.client.is_connected:
        relay.client._disconnected_callback = None  # no reconnect on exit
        await relay.client.disconnect()
    cap.close()


# ----------------------------------------------------------------- client

CLIENT_HELP = """commands:
  w  <char> <hex>    write with response     (char = handle, short or full uuid)
                     FFF1 writes must pass protocol.build(); others need --unsafe
  wn <char> <hex>    write without response
  r  <char>          read
  sub <char>         subscribe     unsub <char>
  ls                 list characteristics
  # text             annotate the capture log
  q                  quit"""


async def cmd_client(a):
    profile = load_profile(a.profile) if not a.target else None
    target = a.target or profile["address"]
    dev, _ = await find(target, a.timeout)
    out = CAPTURES / f"client_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    from travoca_rigel import protocol as P

    async with BleakClient(dev) as client:
        cap = Capture(out, profile)
        for svc in client.services:
            for ch in svc.characteristics:
                cap.labels[ch.handle] = short_uuid(ch.uuid)
        statuses: asyncio.Queue = asyncio.Queue()
        subscribed: set[int] = set()

        def cb(ch, data):
            cap.event("fridge->pc", "notify", ch.handle, bytes(data))
            st = P.decode(bytes(data))
            if st:
                statuses.put_nowait(st)

        async def sub(ch):
            await client.start_notify(ch, cb)
            subscribed.add(ch.handle)
            cap.event("pc->fridge", "subscribe", ch.handle)

        async def guard(ch, data: bytes):
            """Writes to FFF1 must be commands the app would send (see
            protocol.build); validated against a fresh status line. Writes
            to any other characteristic (e.g. AE01, likely firmware update)
            need --unsafe."""
            if ch.uuid.lower() != P.CMD_CHAR:
                if not a.unsafe:
                    raise P.CommandError(f"writes to {ch.uuid} are blocked; only FFF1 is allowed "
                                         "without --unsafe")
                cap.event("info", "unsafe-write", ch.handle)
                return
            parsed = P.parse_command(data)
            if not parsed:
                raise P.CommandError("not a /Scc/1/vvv command; refusing raw write to FFF1")
            if parsed[0] == P.QUERY:
                return
            status_ch = client.services.get_characteristic(P.STATUS_CHAR)
            if status_ch.handle not in subscribed:
                await sub(status_ch)
            while not statuses.empty():
                statuses.get_nowait()
            q = P.query()
            cap.event("pc->fridge", "write", ch.handle, q)
            await client.write_gatt_char(ch, q, response=True)
            P.build(parsed[0], parsed[1], await asyncio.wait_for(statuses.get(), 5))

        if not a.no_sub:
            for svc in client.services:
                for ch in svc.characteristics:
                    if {"notify", "indicate"} & set(ch.properties):
                        await sub(ch)
        console.print(CLIENT_HELP)
        loop = asyncio.get_running_loop()
        while True:
            line = (await loop.run_in_executor(None, sys.stdin.readline))
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                cap.event("info", "note", text=line[1:].strip())
                continue
            cmd, *args = line.split(maxsplit=2)
            try:
                if cmd == "q":
                    break
                elif cmd == "ls":
                    for svc in client.services:
                        console.print(f"[bold]{svc.uuid}[/]")
                        for ch in svc.characteristics:
                            console.print(f"  #{ch.handle:<4} {short_uuid(ch.uuid):10} "
                                          f"{ch.uuid}  {','.join(ch.properties)}")
                elif cmd in ("w", "wn"):
                    ch = resolve_char(client, args[0])
                    data = bytes.fromhex(args[1].replace(" ", ""))
                    await guard(ch, data)
                    cap.event("pc->fridge", "write" if cmd == "w" else "write-cmd", ch.handle, data)
                    await client.write_gatt_char(ch, data, response=(cmd == "w"))
                elif cmd == "r":
                    ch = resolve_char(client, args[0])
                    cap.event("fridge->pc", "read", ch.handle, bytes(await client.read_gatt_char(ch)))
                elif cmd == "sub":
                    await sub(resolve_char(client, args[0]))
                elif cmd == "unsub":
                    ch = resolve_char(client, args[0])
                    await client.stop_notify(ch)
                    cap.event("pc->fridge", "unsubscribe", ch.handle)
                else:
                    console.print(CLIENT_HELP)
            except Exception as e:
                console.print(f"[red]{type(e).__name__}: {e}[/]")
        cap.close()


# ----------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("scan")
    s.add_argument("-t", "--time", type=float, default=8)
    s.add_argument("-a", "--all", action="store_true", help="include unnamed devices")

    s = sp.add_parser("profile")
    s.add_argument("target", help="MAC address or name substring")
    s.add_argument("--timeout", type=float, default=20)

    s = sp.add_parser("relay")
    s.add_argument("--profile", help="profile JSON (default: newest in profiles/)")
    s.add_argument("--adapter", default="hci0")
    s.add_argument("--eager-notify", action="store_true",
                   help="subscribe to all fridge notifications immediately, before the phone does")
    s.add_argument("--adv-only", action="store_true",
                   help="advertise the clone without connecting to the fridge (discovery test)")
    s.add_argument("--no-spoof-name", action="store_true",
                   help="don't change the adapter alias to the fridge's name")

    s = sp.add_parser("client")
    s.add_argument("target", nargs="?", help="MAC/name (default: address from newest profile)")
    s.add_argument("--profile")
    s.add_argument("--timeout", type=float, default=20)
    s.add_argument("--no-sub", action="store_true", help="don't auto-subscribe to notifications")
    s.add_argument("--unsafe", action="store_true",
                   help="allow writes to characteristics other than FFF1 (FFF1 is always validated)")

    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if not a.verbose:
        logging.getLogger("bleak").setLevel(logging.WARNING)
    try:
        asyncio.run(globals()[f"cmd_{a.cmd}"](a))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
