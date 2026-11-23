"""Control and telemetry for the Travoca Rigel 12 V fridge over BLE."""

from travoca_rigel import protocol
from travoca_rigel.protocol import (CommandError, FridgeOffError, SetpointError,
                                    Status, build, decode)

__all__ = ["protocol", "Status", "decode", "build",
           "CommandError", "FridgeOffError", "SetpointError",
           "Fridge", "NotAcknowledged", "NotConnected", "discover"]


def __getattr__(name):
    # Lazy so `protocol` can be used without bleak installed.
    if name in ("Fridge", "NotAcknowledged", "NotConnected", "discover"):
        from travoca_rigel import fridge
        return getattr(fridge, name)
    raise AttributeError(name)
