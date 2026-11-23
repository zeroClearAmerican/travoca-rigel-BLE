# Contributing

Bug reports, captures from other models and pull requests are all welcome.

## Setup

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

The dev tools in `devtools/` need Linux with BlueZ. The library and CLI work wherever bleak does.

## Ground rules for protocol changes

- **Commands and rules come from the official app.** A command is only added to the library once it has been
  captured from the app with `python -m devtools relay`, along with what the app was doing when it sent it.
  Please don't send unmapped commands to a fridge to see what happens. Some of them may be factory or
  calibration settings.
- **Keep the app's limits.** `protocol.build()` refuses anything the app wouldn't send: the setpoint range, the
  36 °F gap between zones, and changing settings while the fridge is off. Changes that loosen these limits won't
  be merged unless the app itself allows it on some model.
- **`travoca_rigel/protocol.py` stays dependency-free** (an optional `logging` import is the only exception), so
  it can run on a microcontroller. A test checks this.
- **Document what you find in PROTOCOL.md**, including how each field or command was identified.

## Personal data

The fridge advertises its own BLE address inside its manufacturer data, so captures and profiles contain it.
`captures/` and `profiles/` are gitignored. Before pasting anything into an issue or committing test data,
replace addresses with something like `AA:BB:CC:DD:EE:FF`. That includes the phone's address in relay
captures.

## Tests

- `pytest` runs the offline tests. They need to pass for every PR.
- `python tests/hil_roundtrip.py [--reconnect]` runs against a real fridge. It turns the fridge on, changes
  several settings and turns it off, so don't run it on a fridge full of food you care about. Mention in the PR if
  you ran it and on which model.

## Pull requests

`main` is protected. Every change goes through a pull request and needs the maintainer's approval before it
can be merged. Smaller PRs that do one thing get reviewed faster.

## License

This project uses the [PolyForm Noncommercial 1.0.0](LICENSE) license. By opening a pull request, you agree
your contribution is licensed under the same terms.
