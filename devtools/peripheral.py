"""BlueZ GATT server + LE advertisement over D-Bus (dbus_fast).

Builds a clone of a real device's GATT table from a saved profile and
routes every read / write / subscribe from the connected phone to async
callbacks supplied by the relay.
"""

import logging
from typing import Awaitable, Callable

from dbus_fast import BusType, DBusError, PropertyAccess, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method

log = logging.getLogger("devtools.peripheral")

BLUEZ = "org.bluez"
APP_PATH = "/org/travoca_rigel/relay"
ADV_PATH = APP_PATH + "/adv0"

# Services BlueZ provides itself; cloning them causes conflicts.
SKIP_SERVICES = {
    "00001800-0000-1000-8000-00805f9b34fb",  # Generic Access
    "00001801-0000-1000-8000-00805f9b34fb",  # Generic Attribute
}
# BlueZ creates the CCCD itself for notify/indicate characteristics.
SKIP_DESCRIPTORS = {"00002902-0000-1000-8000-00805f9b34fb"}

ALLOWED_CHAR_FLAGS = {
    "broadcast", "read", "write-without-response", "write", "notify",
    "indicate", "authenticated-signed-writes", "reliable-write",
    "writable-auxiliaries",
}

ReadCb = Callable[[int, dict], Awaitable[bytes]]
WriteCb = Callable[[int, bytes, dict], Awaitable[None]]
NotifyCb = Callable[[int, bool], Awaitable[None]]


def _plain(options: dict) -> dict:
    return {k: (v.value if isinstance(v, Variant) else v) for k, v in options.items()}


class Service(ServiceInterface):
    def __init__(self, path: str, uuid: str, primary: bool = True):
        super().__init__("org.bluez.GattService1")
        self.path, self._uuid, self._primary = path, uuid, primary

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":
        return self._primary


class Characteristic(ServiceInterface):
    def __init__(self, path, service_path, uuid, flags, handle, relay):
        super().__init__("org.bluez.GattCharacteristic1")
        self.path, self._service, self._uuid = path, service_path, uuid
        self._flags = flags
        self.handle = handle  # handle on the REAL device
        self.relay = relay
        self._value = b""
        self._notifying = False

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return self._service

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return self._flags

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        return self._value

    @dbus_property(access=PropertyAccess.READ)
    def Notifying(self) -> "b":
        return self._notifying

    @method()
    async def ReadValue(self, options: "a{sv}") -> "ay":
        try:
            self._value = bytes(await self.relay.on_read(self.handle, _plain(options)))
        except Exception as e:  # surface failures to the phone
            log.warning("read %s failed: %s", self._uuid, e)
            raise DBusError("org.bluez.Error.Failed", str(e))
        return self._value

    @method()
    async def WriteValue(self, value: "ay", options: "a{sv}"):
        try:
            await self.relay.on_write(self.handle, bytes(value), _plain(options))
        except Exception as e:
            log.warning("write %s failed: %s", self._uuid, e)
            raise DBusError("org.bluez.Error.Failed", str(e))

    @method()
    async def StartNotify(self):
        if self._notifying:
            return
        self._notifying = True
        self.emit_properties_changed({"Notifying": True})
        await self.relay.on_subscribe(self.handle, True)

    @method()
    async def StopNotify(self):
        if not self._notifying:
            return
        self._notifying = False
        self.emit_properties_changed({"Notifying": False})
        await self.relay.on_subscribe(self.handle, False)

    def push(self, data: bytes):
        """Send a notification/indication to the phone."""
        self._value = bytes(data)
        if self._notifying:
            self.emit_properties_changed({"Value": self._value})


class Descriptor(ServiceInterface):
    def __init__(self, path, char_path, uuid, handle, relay):
        super().__init__("org.bluez.GattDescriptor1")
        self.path, self._char, self._uuid = path, char_path, uuid
        self.handle, self.relay = handle, relay

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Characteristic(self) -> "o":
        return self._char

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return ["read", "write"]

    @method()
    async def ReadValue(self, options: "a{sv}") -> "ay":
        try:
            return bytes(await self.relay.on_read_descriptor(self.handle))
        except Exception as e:
            raise DBusError("org.bluez.Error.Failed", str(e))

    @method()
    async def WriteValue(self, value: "ay", options: "a{sv}"):
        try:
            await self.relay.on_write_descriptor(self.handle, bytes(value))
        except Exception as e:
            raise DBusError("org.bluez.Error.Failed", str(e))


class Advertisement(ServiceInterface):
    def __init__(self, local_name, service_uuids, manufacturer_data, service_data):
        super().__init__("org.bluez.LEAdvertisement1")
        self.local_name = local_name
        self.service_uuids = service_uuids
        self.manufacturer_data = manufacturer_data  # {int: bytes}
        self.service_data = service_data  # {uuid: bytes}

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def Discoverable(self) -> "b":
        # Sets the LE General Discoverable AD flag. Without it BlueZ omits the
        # flags entirely (adapter isn't discoverable) and iOS ignores us.
        return True

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        return self.local_name or ""

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":
        return self.service_uuids

    @dbus_property(access=PropertyAccess.READ)
    def ManufacturerData(self) -> "a{qv}":
        return {k: Variant("ay", v) for k, v in self.manufacturer_data.items()}

    @dbus_property(access=PropertyAccess.READ)
    def ServiceData(self) -> "a{sv}":
        return {k: Variant("ay", v) for k, v in self.service_data.items()}

    @method()
    def Release(self):
        log.info("advertisement released by BlueZ")


class Peripheral:
    """Owns the D-Bus connection, GATT app and advertisement."""

    def __init__(self, profile: dict, relay, adapter: str = "hci0"):
        self.profile = profile
        self.relay = relay
        self.adapter_path = f"/org/bluez/{adapter}"
        self.bus: MessageBus | None = None
        self.chars: dict[int, Characteristic] = {}  # real handle -> clone
        self._exported: list[str] = []
        self._orig_alias: str | None = None

    async def start(self, spoof_name: bool = True):
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._build()
        intro = await self.bus.introspect(BLUEZ, self.adapter_path)
        obj = self.bus.get_proxy_object(BLUEZ, self.adapter_path, intro)
        self._adapter = obj.get_interface("org.bluez.Adapter1")
        self._gatt_mgr = obj.get_interface("org.bluez.GattManager1")
        self._adv_mgr = obj.get_interface("org.bluez.LEAdvertisingManager1")

        name = self.profile["advertisement"].get("local_name") or self.profile.get("name")
        if spoof_name and name:
            # GAP Device Name (0x2A00) served by BlueZ comes from the adapter alias.
            self._orig_alias = await self._adapter.get_alias()
            await self._adapter.set_alias(name)

        await self._gatt_mgr.call_register_application(APP_PATH, {})
        log.info("GATT application registered (%d characteristics)", len(self.chars))
        await self._register_adv()

    def _export(self, path, iface):
        self.bus.export(path, iface)
        self._exported.append(path)

    def _build(self):
        si = 0
        for svc in self.profile["services"]:
            if svc["uuid"].lower() in SKIP_SERVICES:
                continue
            spath = f"{APP_PATH}/service{si}"
            si += 1
            self._export(spath, Service(spath, svc["uuid"]))
            for ci, ch in enumerate(svc["characteristics"]):
                cpath = f"{spath}/char{ci}"
                flags = [f for f in ch["properties"] if f in ALLOWED_CHAR_FLAGS]
                c = Characteristic(cpath, spath, ch["uuid"], flags, ch["handle"], self.relay)
                self._export(cpath, c)
                self.chars[ch["handle"]] = c
                for di, d in enumerate(ch.get("descriptors", [])):
                    if d["uuid"].lower() in SKIP_DESCRIPTORS:
                        continue
                    dpath = f"{cpath}/desc{di}"
                    self._export(dpath, Descriptor(dpath, cpath, d["uuid"], d["handle"], self.relay))

    async def _register_adv(self):
        adv = self.profile["advertisement"]
        mfr = {int(k): bytes.fromhex(v) for k, v in adv.get("manufacturer_data", {}).items()}
        sdata = {k: bytes.fromhex(v) for k, v in adv.get("service_data", {}).items()}
        uuids = list(adv.get("service_uuids", []))
        name = adv.get("local_name") or self.profile.get("name") or ""

        # Legacy advertising is 31 bytes; if the full clone doesn't fit, shed
        # fields in order of least likely to be used by the app for filtering.
        attempts = [
            (name, uuids, mfr, sdata),
            (name, uuids, mfr, {}),
            (name, uuids, {}, {}),
            (name, [], mfr, {}),
            (name, [], {}, {}),
        ]
        last_err = None
        for n, u, m, s in attempts:
            a = Advertisement(n, u, m, s)
            try:
                self.bus.export(ADV_PATH, a)
                await self._adv_mgr.call_register_advertisement(ADV_PATH, {})
                log.info("advertising as %r uuids=%s mfr=%s sdata=%s", n, u,
                         {k: v.hex() for k, v in m.items()}, list(s))
                if (u, m, s) != (uuids, mfr, sdata):
                    log.warning("advertisement was trimmed to fit; original had uuids=%s mfr=%s sdata=%s",
                                uuids, list(mfr), list(sdata))
                self._adv_registered = True
                return
            except DBusError as e:
                last_err = e
                self.bus.unexport(ADV_PATH)
                log.debug("adv attempt failed: %s", e)
        raise RuntimeError(f"could not register advertisement: {last_err}")

    def push(self, handle: int, data: bytes):
        c = self.chars.get(handle)
        if c:
            c.push(data)

    async def stop(self):
        if not self.bus:
            return
        for call in (
            lambda: self._adv_mgr.call_unregister_advertisement(ADV_PATH),
            lambda: self._gatt_mgr.call_unregister_application(APP_PATH),
        ):
            try:
                await call()
            except Exception as e:
                log.debug("cleanup: %s", e)
        if self._orig_alias is not None:
            try:
                await self._adapter.set_alias(self._orig_alias)
            except Exception:
                pass
        self.bus.disconnect()
