"""Travoca Rigel (ICECO OEM) BLE protocol: framing, status decoding and the
command rules the official app enforces.

Pure Python with no imports beyond an optional `logging`, so it can be
copied as-is to CircuitPython / MicroPython (no dataclasses, no re).

Transport: service FFF0. Write ASCII commands to FFF1 (write with
response), receive ASCII status lines as notifications on FFF4. Every line
ends in '\\n'.

  Command : /S<cc>/1/<vvv>\\n       cc = 2-digit command,
                                    vvv = "001", "039" or "-01"
  Status  : /S00/1D/<12 comma-separated fields>\\n

See PROTOCOL.md for details.
"""

try:
    import logging
    log = logging.getLogger("travoca_rigel.protocol")
except ImportError:  # CircuitPython / MicroPython
    log = None

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
CMD_CHAR = "0000fff1-0000-1000-8000-00805f9b34fb"
STATUS_CHAR = "0000fff4-0000-1000-8000-00805f9b34fb"
ADV_NAME = "travoca"

QUERY = 0       # value -01: request a status line
POWER = 1       # 0 off, 1 on
DEEP_SET = 3    # deep-side setpoint, in current display units
BATT_PROT = 4   # 1 low, 2 medium, 3 high
SHELF_SET = 5   # shelf-side setpoint, in current display units
MODE = 6        # 1 eco, 2 max
UNITS = 7       # 1 C, 2 F
# S02 and S08+ are unmapped: the app never sends them. Not exposed.

BATT_NAMES = {1: "low", 2: "medium", 3: "high"}
MODE_NAMES = {1: "eco", 2: "max"}
UNIT_NAMES = {1: "C", 2: "F"}

# --- rules enforced by the official app ------------------------------------

# Setpoint range for both zones: -4..68 F (exactly -20..20 C).
SETPOINT_RANGE = {"F": (-4, 68), "C": (-20, 20)}
# The two setpoints may be at most 36 F apart (20 C, the same span),
# presumably because the compressor can't hold a wider separation.
MAX_SETPOINT_SPREAD = {"F": 36, "C": 20}
# Discrete settings and their allowed values.
ALLOWED_VALUES = {
    POWER: (0, 1),
    BATT_PROT: tuple(BATT_NAMES),
    MODE: tuple(MODE_NAMES),
    UNITS: tuple(UNIT_NAMES),
}


class CommandError(ValueError):
    """A command the app would not send."""


class FridgeOffError(CommandError):
    pass


class SetpointError(CommandError):
    pass


# --- framing ----------------------------------------------------------------

def fmt_value(v):
    """Values are 3 chars: zero-padded positive, or '-' + 2 digits."""
    if not -99 <= v <= 999:
        raise ValueError("value %d out of range" % v)
    return "-%02d" % -v if v < 0 else "%03d" % v


def _encode(cmd, value):
    return ("/S%02d/1/%s\n" % (cmd, fmt_value(value))).encode()


def parse_command(payload):
    """Parse a raw b'/Scc/1/vvv' command back into (cmd, value), or None."""
    try:
        s = payload.decode()
    except UnicodeError:
        return None
    if s.endswith("\n"):
        s = s[:-1]
    parts = s.split("/")
    # ['', 'Scc', '1', 'vvv']
    if len(parts) != 4 or parts[0] or parts[2] != "1":
        return None
    cc, vvv = parts[1], parts[3]
    if len(cc) != 3 or cc[0] != "S" or not cc[1:].isdigit() or len(vvv) != 3:
        return None
    if not (vvv.isdigit() or (vvv[0] == "-" and vvv[1:].isdigit())):
        return None
    return int(cc[1:]), int(vvv)


def query():
    """Status request. Always allowed, needs no prior state."""
    return _encode(QUERY, -1)


# --- status -----------------------------------------------------------------

class Status:
    """One decoded status line. Temperatures and setpoints are in `units`."""

    __slots__ = ("power", "units", "deep_temp", "shelf_temp", "deep_set",
                 "shelf_set", "batt_prot", "voltage", "mode", "raw", "unknown")

    def __init__(self, power, units, deep_temp, shelf_temp, deep_set, shelf_set,
                 batt_prot, voltage, mode, raw, unknown):
        self.power = power            # f1  bool
        self.units = units            # f2  'C' / 'F'
        self.deep_temp = deep_temp    # f3  actual temperature, deep side
        self.shelf_temp = shelf_temp  # f4  actual temperature, shelf side
        self.deep_set = deep_set      # f5  (S03)
        self.shelf_set = shelf_set    # f6  (S05)
        self.batt_prot = batt_prot    # f7  (S04) 'low' / 'medium' / 'high'
        self.voltage = voltage        # f8  volts (sent as tenths)
        self.mode = mode              # f9  (S06) 'eco' / 'max'
        self.raw = raw
        # f0, f10, f11: meaning unknown. Kept only so they can be logged;
        # nothing should act on them.
        self.unknown = unknown

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__ if k not in ("raw", "unknown")}

    def summary(self):
        u = "°" + self.units
        return ("power=%s  mode=%s  deep=%d%s (set %d%s)  shelf=%d%s (set %d%s)  "
                "batt=%.1fV prot=%s" % (
                    "ON" if self.power else "OFF", self.mode,
                    self.deep_temp, u, self.deep_set, u,
                    self.shelf_temp, u, self.shelf_set, u,
                    self.voltage, self.batt_prot))

    def __repr__(self):
        return "Status(%s)" % self.raw


STATUS_PREFIX = "/S00/1D/"


def decode(data):
    """Decode a FFF4 notification into a Status, or None if it isn't one."""
    try:
        s = data.decode().strip()
    except UnicodeError:
        return None
    if not s.startswith(STATUS_PREFIX):
        return None
    f = s[len(STATUS_PREFIX):].split(",")
    if len(f) < 12:
        if log:
            log.debug("short status line: %r", s)
        return None
    unknown = {"f0": f[0], "f10": f[10], "f11": f[11]}
    for i in range(12, len(f)):
        unknown["f%d" % i] = f[i]
    if log:
        log.debug("status unknown fields: %s", unknown)
    try:
        return Status(
            power=f[1] == "1", units=UNIT_NAMES.get(int(f[2]), f[2]),
            deep_temp=int(f[3]), shelf_temp=int(f[4]),
            deep_set=int(f[5]), shelf_set=int(f[6]),
            batt_prot=BATT_NAMES.get(int(f[7]), f[7]), voltage=int(f[8]) / 10,
            mode=MODE_NAMES.get(int(f[9]), f[9]), raw=s, unknown=unknown,
        )
    except ValueError:
        if log:
            log.debug("malformed status line: %r", s)
        return None


# --- validated command building ----------------------------------------------

def check_setpoints(deep, shelf, units):
    lo, hi = SETPOINT_RANGE[units]
    for side, v in (("deep", deep), ("shelf", shelf)):
        if not lo <= v <= hi:
            raise SetpointError("%s setpoint %d°%s outside %d..%d°%s" % (side, v, units, lo, hi, units))
    limit = MAX_SETPOINT_SPREAD[units]
    if abs(deep - shelf) > limit:
        raise SetpointError("deep %d°%s and shelf %d°%s are %d°%s apart; max allowed is %d°%s" % (
            deep, units, shelf, units, abs(deep - shelf), units, limit, units))


def build(cmd, value, current):
    """Build a command, enforcing the app's rules against the fridge's
    current Status. Raises CommandError for anything the app wouldn't send."""
    if cmd == QUERY:
        return query()
    if cmd not in (DEEP_SET, SHELF_SET) and cmd not in ALLOWED_VALUES:
        raise CommandError("S%02d is unmapped; refusing to send it" % cmd)
    if cmd != POWER and not current.power:
        raise FridgeOffError("fridge is off; only power on is allowed")
    if cmd in ALLOWED_VALUES and value not in ALLOWED_VALUES[cmd]:
        raise CommandError("S%02d: value %d not in %s" % (cmd, value, list(ALLOWED_VALUES[cmd])))
    if cmd == DEEP_SET:
        check_setpoints(value, current.shelf_set, current.units)
    elif cmd == SHELF_SET:
        check_setpoints(current.deep_set, value, current.units)
    return _encode(cmd, value)


def applied(st, cmd, value):
    """True when a Status shows the command took effect."""
    if cmd == POWER:
        return st.power == bool(value)
    if cmd == DEEP_SET:
        return st.deep_set == value
    if cmd == SHELF_SET:
        return st.shelf_set == value
    if cmd == BATT_PROT:
        return st.batt_prot == BATT_NAMES.get(value)
    if cmd == MODE:
        return st.mode == MODE_NAMES.get(value)
    if cmd == UNITS:
        return st.units == UNIT_NAMES.get(value)
    return True
