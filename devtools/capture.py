"""JSONL capture log + pretty console output."""

import json
import time
from pathlib import Path

from rich.console import Console

console = Console(highlight=False)

DIR_STYLE = {
    "phone->fridge": "bold cyan",
    "fridge->phone": "bold magenta",
    "pc->fridge": "bold green",
    "fridge->pc": "bold magenta",
    "info": "dim",
}


def hexdump(b: bytes) -> str:
    return " ".join(f"{x:02x}" for x in b)


def printable(b: bytes) -> str:
    return "".join(chr(x) if 32 <= x < 127 else "." for x in b)


class Capture:
    def __init__(self, path: Path, profile: dict | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.f = path.open("a", buffering=1)
        self.t0 = time.time()
        # handle -> short uuid label for display
        self.labels: dict[int, str] = {}
        if profile:
            for s in profile["services"]:
                for c in s["characteristics"]:
                    self.labels[c["handle"]] = short_uuid(c["uuid"])
        self.quiet = False
        console.print(f"[dim]capture -> {path}[/]")

    def event(self, direction: str, op: str, handle: int | None = None,
              data: bytes | None = None, **extra):
        now = time.time()
        rec = {"ts": round(now, 6), "dir": direction, "op": op}
        if handle is not None:
            rec["handle"] = handle
            rec["char"] = self.labels.get(handle)
        if data is not None:
            rec["hex"] = data.hex()
        rec.update(extra)
        self.f.write(json.dumps(rec) + "\n")
        if getattr(self, "quiet", False):
            return

        style = DIR_STYLE.get(direction, "")
        label = f"{self.labels.get(handle, '')}#{handle}" if handle is not None else ""
        line = f"[dim]{now - self.t0:8.3f}[/] [{style}]{direction:14}[/] {op:10} {label:14}"
        if data is not None:
            line += f" ({len(data):3}) {hexdump(data)}  [dim]|{printable(data)}|[/]"
        if extra:
            line += " [dim]" + " ".join(f"{k}={v}" for k, v in extra.items()) + "[/]"
        console.print(line)

    def close(self):
        self.f.close()


def short_uuid(u: str) -> str:
    u = u.lower()
    if u.endswith("-0000-1000-8000-00805f9b34fb") and u.startswith("0000"):
        return u[4:8]
    return u[:8]
