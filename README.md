# travoca-rigel-BLE

Reimplementation of the BLE protocol used by Travoca Rigel-series fridges, so you can read telemetry and
control the fridge from a computer instead of the phone app.

The Rigel is a rebadged ICECO unit, so other ICECO-based fridges may speak the same protocol. Only one fridge
has been tested so far.

Unofficial and not affiliated with Travoca or ICECO.

## What works

- Reading status: power, mode, units, actual and target temperature for both zones, supply voltage, battery
  protection level
- Setting power, eco/max mode, °C/°F, battery protection, and both setpoints
- The same input rules the app applies (see below), so you can't send the fridge something the app wouldn't
- Every command waits for the fridge to confirm the change in its status
- Reconnecting with backoff when the link drops

The full protocol is written up in [PROTOCOL.md](PROTOCOL.md).

## Install

Python 3.11 or newer. Uses [bleak](https://github.com/hbldh/bleak), so it runs on Linux, macOS and Windows.

```sh
python -m venv .venv
.venv/bin/pip install -e .          # library + CLI
.venv/bin/pip install -e ".[dev]"   # plus dev tools and tests
```

## CLI

The fridge only takes one BLE connection at a time, so close the phone app (or turn off the phone's Bluetooth)
first.

```sh
travoca-rigel discover               # list fridges in range
travoca-rigel status
travoca-rigel watch                  # live telemetry, keeps going through link drops
travoca-rigel power on               # on | off
travoca-rigel mode eco               # eco | max
travoca-rigel units f                # c | f
travoca-rigel batt medium            # low | medium | high
travoca-rigel deep 35                # setpoints are in the fridge's current units
travoca-rigel shelf 38
```

The address comes from `--address`, then `$TRAVOCA_ADDRESS`, then discovery if exactly one fridge is in range.
Add `-v` for debug logging, which includes the status fields we haven't identified yet.

## Library

```python
import asyncio
from travoca_rigel import Fridge, CommandError

async def main():
    async with Fridge("AA:BB:CC:DD:EE:FF") as fridge:
        st = await fridge.status()
        print(st.deep_temp, st.shelf_temp, st.voltage)

        await fridge.set_power(True)
        try:
            await fridge.set_deep(-10)   # refused if it breaks the app's rules
        except CommandError as e:
            print("refused:", e)

asyncio.run(main())
```

`travoca_rigel.protocol` handles framing, decoding and validation. It has no dependencies and avoids anything
CircuitPython/MicroPython lacks, so it should work on a microcontroller too, though nobody has tried that yet.
`travoca_rigel.fridge` is the bleak-based client.

### Rules

These match what the official app enforces:

- While the fridge is off, the only thing you can do is turn it on.
- Each setpoint must be between -4 and 68 °F (-20 to 20 °C).
- The two setpoints can't be more than 36 °F (20 °C) apart.
- Commands the app never sends are refused.

## Dev tools

`devtools/` holds the tools used to work out the protocol. They aren't part of the library, and they need Linux
with BlueZ.

```sh
python -m devtools scan              # nearby devices and their advertisement data
python -m devtools profile <mac>     # dump advertisement + GATT table to profiles/
python -m devtools relay             # man-in-the-middle between the app and the fridge
python -m devtools client            # interactive raw GATT client
```

`relay` connects to the fridge (which then stops advertising), then advertises a copy of it for the phone
and forwards and logs everything in both directions to `captures/`. The app recognizes the fridge by the
MAC address in its advertisement, so the copy looks like the fridge you already added and won't show up as a
new device. Remove the fridge in the app and add the copy instead. When you're done, remove it again and
re-add the real fridge.

The `client` REPL runs `FFF1` writes through the same validation as the library, and refuses writes to any other
characteristic unless you pass `--unsafe`.

## Tests

```sh
pytest                                   # offline
python tests/hil_roundtrip.py            # against a real fridge
python tests/hil_roundtrip.py --reconnect
```

The hardware test turns the fridge on, toggles eco/max, switches to °C and back to °F, sets deep to 35 °F and
shelf to 0 °F, and turns it off. Each step has to show up in the fridge's status before the test moves on.
`--reconnect` also drops the link through BlueZ in the middle and checks the client reconnects. The fridge is
left off with those setpoints.

## Roadmap

Home Assistant integration, either through a Bluetooth adapter on the HA host or through an ESP32 on Wi-Fi.
