# Travoca Rigel BLE protocol

The Travoca Rigel is a rebadged ICECO fridge (Device Information reports manufacturer `ICECO`, model `iceco`,
module `SUNTEK`, firmware 1001, software 1003). There's no pairing or encryption.

## Advertising

- Flags `0x06`, 16-bit service UUID `FFF0`
- Manufacturer data: the fridge's own MAC address (e.g. `AA:BB:CC:DD:EE:FF` appears as `aabb ccddeeff`: the first two bytes land in the "company ID" slot), then `1D`, then `travoca`
- Scan response: complete name `travoca`

The app matches the fridge using the MAC address in the manufacturer data. A clone that copies this data
is treated as the fridge you already added, so it doesn't show up as a new device. To use the relay, remove
the fridge in the app and add the clone instead.

## GATT

| Handle | UUID | Properties | Use |
|---|---|---|---|
| 24 | `FFF1` | write, write-without-response | commands (the app uses write with response) |
| 26 | `FFF4` | read, notify | status lines (reading it returns nothing) |
| 129/131 | `AE01`/`AE02` | write-without-response / notify | not used by the app; probably firmware update |

## Framing

Everything is ASCII and each line ends with `\n`. Every write and every notification carried exactly one
complete line. Status lines are about 44 bytes and arrive whole, so nothing needs reassembling.

Command: `/S<cc>/1/<vvv>\n`

- `cc` is a 2-digit command number.
- `vvv` is 3 characters: either zero-padded (`001`, `039`) or a minus sign plus 2 digits (`-01`).

| cmd | Meaning | Values |
|---|---|---|
| `S00` | status request | `-01` (the app sends this when it connects and after some changes) |
| `S01` | power | `000` off, `001` on |
| `S03` | deep-side setpoint | temperature in the current display unit, e.g. `040`, `-01` |
| `S04` | battery protection | `001` low, `002` medium, `003` high |
| `S05` | shelf-side setpoint | temperature in the current display unit |
| `S06` | mode | `001` eco, `002` max |
| `S07` | display units | `001` °C, `002` °F |

`S02` and `S08`+ are **unmapped/unused**. The app covers every feature this fridge has and never sends them,
though other models might. The library refuses to send them.

## Status line (FFF4 notify)

`/S00/1D/2,1,2,+63,+68,+39,+38,1,145,2,0,000\n`

The fridge sends one of these in reply to every command. It also sends them on its own, about every 0.8
seconds when a value changes (mostly voltage jitter).

| # | Example | Meaning | Evidence |
|---|---|---|---|
| f0 | `2` | unknown (number of zones?) | always 2. Debug-logged only |
| f1 | `1` | power | follows `S01` |
| f2 | `2` | units, 1 = °C, 2 = °F | follows `S07`; all temperatures convert when it changes |
| f3 | `+63` | actual temperature, deep side | confirmed: the side set by `S03`, which cooled first |
| f4 | `+68` | actual temperature, shelf side | |
| f5 | `+39` | deep-side setpoint | follows `S03` |
| f6 | `+38` | shelf-side setpoint | follows `S05` |
| f7 | `1` | battery protection level | follows `S04` |
| f8 | `145` | supply voltage × 10 | 14.5 V idle, about 13.8 V once the compressor runs |
| f9 | `2` | mode, 1 = eco, 2 = max | follows `S06` |
| f10 | `0` | unknown, maybe a fault flag | always 0. Debug-logged only |
| f11 | `000` | unknown, maybe an error code | always 000. Debug-logged only |

The `1D` in the status header also appears in the advertisement after the MAC address, so it may be a
model or protocol identifier.

## Rules enforced by the app (and by `travoca_rigel.protocol.build`)

Every command goes through `protocol.build(cmd, value, current_status)`, which checks it against a fresh
status line:

- While the fridge is **off**, only power-on is allowed. Every other setting and both setpoints are refused.
- Setpoint range for both zones is **-4 to 68 °F** (exactly -20 to 20 °C).
- The deep and shelf setpoints may be at most **36 °F (20 °C) apart**. The fridge probably can't hold a wider
  gap between the zones.
- Discrete settings only take their listed values. Unmapped commands are refused.

`Fridge.send()`, the `travoca-rigel` CLI and the devtools `client` REPL all go through `build`. The REPL also
refuses raw FFF1 writes that don't parse as a command, and blocks writes to any other characteristic (such as
`AE01`, which is probably firmware update) unless you pass `--unsafe`.

## Open questions

- f0, f10 and f11: there's no way to trigger them on purpose yet. They're logged at debug level (`-v`) in case
  they ever change.
