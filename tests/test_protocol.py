"""Offline tests for trv.protocol (no hardware). Run: .venv/bin/python -m pytest tests/"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from travoca_rigel import protocol as P  # noqa: E402

ON_F = P.decode(b"/S00/1D/2,1,2,+63,+68,+39,+38,1,145,2,0,000\n")
OFF_F = P.decode(b"/S00/1D/2,0,2,+30,+68,-01,+33,1,145,2,0,000\n")
ON_C = P.decode(b"/S00/1D/2,1,1,+17,+20,+04,+03,1,142,2,0,000\n")


def test_decode_fields():
    st = ON_F
    assert (st.power, st.units, st.deep_temp, st.shelf_temp) == (True, "F", 63, 68)
    assert (st.deep_set, st.shelf_set, st.batt_prot, st.voltage, st.mode) == (39, 38, "low", 14.5, "max")
    assert st.unknown == {"f0": "2", "f10": "0", "f11": "000"}


def test_decode_rejects_other_lines():
    assert P.decode(b"") is None
    assert P.decode(b"/S00/1/-01\n") is None
    assert P.decode(b"/S00/1D/2,1,2\n") is None


def test_framing():
    assert P.query() == b"/S00/1/-01\n"
    assert P.build(P.POWER, 1, OFF_F) == b"/S01/1/001\n"
    assert P.build(P.DEEP_SET, 2, ON_F) == b"/S03/1/002\n"
    assert P.build(P.DEEP_SET, -1, P.decode(b"/S00/1D/2,1,2,+63,+68,+39,+33,1,145,2,0,000\n")) == b"/S03/1/-01\n"
    assert P.parse_command(b"/S05/1/039\n") == (5, 39)
    assert P.parse_command(b"/S03/1/-04") == (3, -4)
    assert P.parse_command(b"junk") is None


def test_off_allows_only_power_on():
    assert P.build(P.POWER, 1, OFF_F)
    for cmd, v in ((P.MODE, 1), (P.UNITS, 1), (P.BATT_PROT, 2), (P.DEEP_SET, 0), (P.SHELF_SET, 30)):
        with pytest.raises(P.FridgeOffError):
            P.build(cmd, v, OFF_F)


@pytest.mark.parametrize("cmd,v", [(P.DEEP_SET, -4), (P.DEEP_SET, 32), (P.SHELF_SET, 68), (P.SHELF_SET, 32)])
def test_setpoint_range_edges_ok_f(cmd, v):
    st = P.decode(b"/S00/1D/2,1,2,+63,+68,+32,+32,1,145,2,0,000\n")
    assert P.build(cmd, v, st)


@pytest.mark.parametrize("cmd,v", [(P.DEEP_SET, -5), (P.DEEP_SET, 69), (P.SHELF_SET, -5), (P.SHELF_SET, 69)])
def test_setpoint_range_violations_f(cmd, v):
    st = P.decode(b"/S00/1D/2,1,2,+63,+68,+32,+32,1,145,2,0,000\n")
    with pytest.raises(P.SetpointError, match="outside"):
        P.build(cmd, v, st)


def test_setpoint_range_c():
    st = P.decode(b"/S00/1D/2,1,1,+17,+20,+00,+00,1,142,2,0,000\n")
    assert P.build(P.DEEP_SET, -20, st) and P.build(P.SHELF_SET, 20, st)
    for cmd, v in ((P.DEEP_SET, -21), (P.SHELF_SET, 21)):
        with pytest.raises(P.SetpointError, match="outside"):
            P.build(cmd, v, st)


def test_setpoint_spread():
    # ON_F: deep 39, shelf 38
    assert P.build(P.DEEP_SET, 68, P.decode(b"/S00/1D/2,1,2,+63,+68,+39,+32,1,145,2,0,000\n"))  # 36 apart
    with pytest.raises(P.SetpointError, match="apart"):
        P.build(P.SHELF_SET, 2, ON_F)  # 37 apart
    assert P.build(P.SHELF_SET, 3, ON_F)  # 36 apart
    with pytest.raises(P.SetpointError, match="apart"):
        P.build(P.DEEP_SET, -18, ON_C)  # shelf 3 C -> 21 apart
    assert P.build(P.DEEP_SET, -17, ON_C)


def test_hil_sequence_is_valid():
    # deep 35 then shelf 0 (F) must pass the spread rule in that order
    st = P.decode(b"/S00/1D/2,1,2,+63,+68,+39,+33,1,145,2,0,000\n")
    P.build(P.DEEP_SET, 35, st)
    st = P.decode(b"/S00/1D/2,1,2,+63,+68,+35,+33,1,145,2,0,000\n")
    P.build(P.SHELF_SET, 0, st)


@pytest.mark.parametrize("cmd,v", [(P.POWER, 2), (P.MODE, 0), (P.MODE, 3), (P.UNITS, 3), (P.BATT_PROT, 4)])
def test_discrete_values(cmd, v):
    with pytest.raises(P.CommandError):
        P.build(cmd, v, ON_F)


@pytest.mark.parametrize("cmd", [2, 8, 9, 42])
def test_unmapped_refused(cmd):
    with pytest.raises(P.CommandError, match="unmapped"):
        P.build(cmd, 1, ON_F)


def test_applied():
    assert P.applied(ON_F, P.POWER, 1) and not P.applied(ON_F, P.POWER, 0)
    assert P.applied(ON_F, P.MODE, 2) and P.applied(ON_F, P.UNITS, 2)
    assert P.applied(ON_F, P.DEEP_SET, 39) and P.applied(ON_F, P.BATT_PROT, 1)


def test_every_captured_status_line_decodes():
    caps = list((ROOT / "captures").glob("*.jsonl"))
    lines = [json.loads(l) for c in caps for l in c.read_text().splitlines() if l.strip()]
    statuses = [bytes.fromhex(r["hex"]) for r in lines if r.get("char") == "fff4" and r.get("hex")]
    if not statuses:
        pytest.skip("no captures")
    assert all(P.decode(s) for s in statuses)


def test_status_as_dict_and_unknown_not_in_summary():
    d = ON_F.as_dict()
    assert d["deep_temp"] == 63 and "unknown" not in d and "raw" not in d
    assert "f10" not in ON_F.summary()


def test_protocol_module_is_dependency_free():
    # Must stay portable to CircuitPython/MicroPython: no imports besides an
    # optional logging.
    src = (ROOT / "travoca_rigel" / "protocol.py").read_text()
    imports = [l.strip() for l in src.splitlines() if l.strip().startswith(("import ", "from "))]
    assert imports == ["import logging"], imports
