#!/usr/bin/env python3
"""Look at a test VM's screen and click and type in it, from outside, the way
a person at the keyboard would. It talks VNC to the hypervisor, so nothing is
needed inside the VM: no macOS permissions, and it sees everything (windows,
dialogs, the folder panel, the menu bar).

    tools/vmgui.py shot <vm> [file]        screenshot (1280 px wide) -> file (default: a temp file); prints its path
    tools/vmgui.py click <vm> X Y          click; X, Y are pixels in the screenshot
    tools/vmgui.py dclick <vm> X Y         double-click
    tools/vmgui.py type <vm> "text"        type text
    tools/vmgui.py key <vm> enter|tab|esc|bsp|ctrl-a|shift-tab|...   press a key or combo
    tools/vmgui.py scroll <vm> down|up [N] scroll N notches (default 5)
    tools/vmgui.py open <vm>               open the VM's screen in Screen Sharing, to watch or take over

The VM must have been started with VNC: tools/scenario.py start <name> --vnc
(that records each VM's address in ~/.config/casper-vm/vnc/<vm>.json). A VM
served over VNC has no window of its own; `open` shows it."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
VNCDO = HERE.parent / ".venv" / "bin" / "vncdo"
RECORDS = Path.home() / ".config" / "casper-vm" / "vnc"
SHOT_WIDTH = 1280


def record(vm: str) -> dict:
    f = RECORDS / f"{vm}.json"
    if not f.exists():
        sys.exit(f"{vm} isn't served over VNC. Start it with: tools/scenario.py start <name> --vnc")
    return json.loads(f.read_text())


def vnc(vm: str, *args: str) -> str:
    r = record(vm)
    out = subprocess.run([str(VNCDO), "-s", f"127.0.0.1::{r['port']}", "-p", r["password"], *args],
                         capture_output=True, text=True, env={**os.environ, "PYTHONWARNINGS": "ignore"})
    if out.returncode != 0:
        sys.exit(f"{vm}: VNC failed ({out.stderr.strip().splitlines()[-1] if out.stderr.strip() else out.returncode}). Is it running with --vnc?")
    return out.stdout


# Over this VNC a shifted character typed as itself arrives unshifted (":" as ";",
# "@" as "2"), so shifted characters are sent as an explicit Shift chord.
SHIFTED = {"~": "`", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
           "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/"}


def keystrokes(text: str) -> list[str]:
    """vncdo arguments that type `text`: runs of plain characters, and
    shift-down / key / shift-up for capitals and shifted symbols (a "shift--"
    chord would be parsed as an empty key)."""
    out, run = [], ""
    for ch in text:
        if ch.isupper() or ch in SHIFTED:
            if run:
                out += ["type", run]
                run = ""
            out += ["keydown", "shift", "key", ch.lower() if ch.isalpha() else SHIFTED[ch], "keyup", "shift", "pause", "0.03"]
        else:
            run += ch
    if run:
        out += ["type", run]
    return out


def native_width(png: str) -> int:
    out = subprocess.run(["sips", "-g", "pixelWidth", png], capture_output=True, text=True).stdout
    return int(out.split("pixelWidth:")[1].split()[0])


def scale(vm: str) -> float:
    """Native screen pixels per screenshot pixel."""
    cache = RECORDS / f"{vm}.scale"
    if cache.exists():
        return float(cache.read_text())
    tmp = tempfile.mktemp(suffix=".png")
    vnc(vm, "capture", tmp)
    f = native_width(tmp) / SHOT_WIDTH
    os.unlink(tmp)
    cache.write_text(str(f))
    return f


def shot(vm: str, out: str | None = None) -> str:
    png = tempfile.mktemp(suffix=".png")
    vnc(vm, "capture", png)
    (RECORDS / f"{vm}.scale").write_text(str(native_width(png) / SHOT_WIDTH))
    out = out or tempfile.mktemp(suffix=".jpg")
    subprocess.run(["sips", "-Z", str(SHOT_WIDTH), "-s", "format", "jpeg", "-s", "formatOptions", "80", png, "--out", out],
                   capture_output=True, check=True)
    os.unlink(png)
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in {"shot", "click", "dclick", "type", "key", "scroll", "open"}:
        print(__doc__)
        return 2
    cmd, vm, rest = argv[1], argv[2], argv[3:]
    if cmd == "shot":
        print(shot(vm, rest[0] if rest else None))
    elif cmd in ("click", "dclick"):
        f = scale(vm)
        x, y = round(float(rest[0]) * f), round(float(rest[1]) * f)
        vnc(vm, "move", str(x), str(y), "pause", "0.2", "click", "1", *(["click", "1"] if cmd == "dclick" else []))
    elif cmd == "type":
        vnc(vm, *keystrokes(rest[0]))
    elif cmd == "key":
        vnc(vm, "key", rest[0])
    elif cmd == "scroll":
        button = "5" if (rest[0] if rest else "down") == "down" else "4"
        vnc(vm, *["click", button] * int(rest[1] if len(rest) > 1 else 5))
    elif cmd == "open":
        r = record(vm)
        subprocess.run(["open", f"vnc://:{r['password']}@127.0.0.1:{r['port']}"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
