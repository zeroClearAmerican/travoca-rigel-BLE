# Security

## Reporting a problem

Please don't open a public issue for security problems. Use
[private vulnerability reporting](https://github.com/zeroClearAmerican/travoca-rigel-BLE/security/advisories/new)
instead, which only the maintainer can see.

## Scope

This project talks to a fridge over BLE. The fridge itself has no pairing or encryption, so anyone in Bluetooth
range can already control it. That's a property of the hardware and out of scope here.

Things that are in scope:

- A way to get the library or CLI to send a command that `protocol.build()` should have refused
- Bugs in the dev tools that could affect the host they run on (for example, the relay's BlueZ D-Bus service)
- Personal data (BLE addresses and the like) ending up somewhere it shouldn't, such as logs meant for sharing
