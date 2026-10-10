#!/usr/bin/env python3
"""Reset test VMs and their dev Casper accounts together, to known points.

    tools/scenario.py list                 what's defined (tools/scenarios.toml)
    tools/scenario.py start <scenario>     prepare this Mac, then reset and start every persona in it
    tools/scenario.py reset <checkpoint>   reset and start one persona at one checkpoint
    tools/scenario.py stop                 shut every test VM down

How it works: each persona's VM is cloned fresh from a local snapshot
(copy-on-write, instant), their dev account is deleted, and the checkpoint's
steps are replayed -- inside the VM through the Tart guest agent (`tart
exec`), and on the server through dev-only endpoints (casper_service/
devtools.py). Snapshots never hold account state, so a VM and the server can
never disagree.

Needs: Tart; ~/.config/casper-vm/dev-admin-token (matches DEV_ADMIN_TOKEN in
the dev deployment's .env). Optional: ~/.config/casper-vm/quit-apps, apps to
quit before starting (one per line) to free memory."""

import json
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF = tomllib.loads((HERE / "scenarios.toml").read_text())
CONFIG_DIR = Path.home() / ".config" / "casper-vm"
CASPER = "/Applications/CasperGo/Casper.app/Contents/MacOS/Casper"
VM_CPU, VM_MEMORY_MB, VM_DISPLAY = 4, 4096, "1440x900"
_print_lock = threading.Lock()


def say(who: str, text: str):
    with _print_lock:
        print(f"{who:>6}  {text}", flush=True)


# --- Tart -------------------------------------------------------------------------------
def tart(*args, check=True, capture=True) -> subprocess.CompletedProcess:
    return subprocess.run(["tart", *args], check=check, capture_output=capture, text=True)


def running_vms() -> set[str]:
    out = tart("list", "--format", "json").stdout
    return {v["Name"] for v in json.loads(out) if v.get("Running") or v.get("State") == "running"}


def vm_exec(vm: str, script: str, timeout: int = 300) -> str:
    """Runs a shell script in the VM as its user (GUI session: Keychain and
    `open` work), via the guest agent."""
    r = subprocess.run(["tart", "exec", vm, "/bin/zsh", "-lc", script], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"in {vm}: {(r.stderr or r.stdout).strip()[-600:]}")
    return r.stdout.strip()


def fresh_vm(persona: dict):
    vm = persona["vm"]
    if vm in running_vms():
        tart("stop", vm, check=False)
    tart("delete", vm, check=False)
    tart("clone", persona["snapshot"], vm)
    tart("set", vm, "--cpu", str(VM_CPU), "--memory", str(VM_MEMORY_MB), "--display", VM_DISPLAY)
    subprocess.Popen(["tart", "run", vm], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + 180
    while time.time() < deadline:
        if subprocess.run(["tart", "exec", vm, "true"], capture_output=True).returncode == 0:
            return
        time.sleep(3)
    raise RuntimeError(f"{vm} didn't come up (no guest agent after 3 minutes)")


# --- The dev deployment ----------------------------------------------------------------
def dev(method: str, path: str) -> dict:
    token = (CONFIG_DIR / "dev-admin-token").read_text().strip()
    req = urllib.request.Request(CONF["deployment"]["auth_url"] + path, method=method,
                                 headers={"X-Dev-Admin": token, "User-Agent": "casper-scenario"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def mcp(token: str, tool: str, **arguments):
    """One MCP tool call as the persona; returns its result (text, or the
    tool's structured data, e.g. list_hosts' list)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}).encode()
    req = urllib.request.Request(CONF["deployment"]["auth_url"] + "/mcp", data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream", "User-Agent": "casper-scenario"})
    with urllib.request.urlopen(req, timeout=60) as r:
        result = json.loads(r.read())["result"]
    if result.get("isError"):
        raise RuntimeError(f"{tool}: {result}")
    structured = result.get("structuredContent") or {}
    return structured["result"] if "result" in structured else "\n".join(c.get("text", "") for c in result["content"])


# --- Steps ------------------------------------------------------------------------------
def step_install_casper(p, ctx):
    url = CONF["deployment"]["download_url"]
    vm_exec(p["vm"], f"""
        curl -fsSL '{url}' -o /tmp/casper.zip && rm -rf /Applications/CasperGo &&
        unzip -oq /tmp/casper.zip -d /Applications &&
        open --env CASPER_SCRIPTED_SETUP=1 /Applications/CasperGo/Casper.app""")
    for _ in range(30):
        if "Casper app: running" in vm_exec(p["vm"], f"'{CASPER}' setup status 2>&1 || true"):
            return "installed and running"
        time.sleep(2)
    raise RuntimeError("Casper didn't start")


def step_create_account(p, ctx):
    vm_exec(p["vm"], f"'{CASPER}' setup account create --username '{p['username']}'")
    return f"account {p['username']}"


def step_pair(p, ctx):
    return vm_exec(p["vm"], f"'{CASPER}' setup pair", timeout=120).splitlines()[-1]


def step_connect_claude(p, ctx):
    vm_exec(p["vm"], f"'{CASPER}' setup agent")
    return "Claude Code has Casper's tools"


def step_chat_folder(p, ctx):
    vm_exec(p["vm"], "mkdir -p ~/chat")
    return "~/chat ready for `claude`"


def step_publish_offering(p, ctx, gb="20"):
    token = ctx.setdefault("agent_token", dev("POST", f"/dev/users/{p['username']}/agent-token")["token"])
    mine = [h["host"] for h in mcp(token, "list_hosts") if h["role"] == "owner"]
    return mcp(token, "publish_offering", host=mine[0], max_gb=float(gb), kind="mirror", preview=False).split(".")[0]


STEPS = {
    "install_casper": step_install_casper,
    "create_account": step_create_account,
    "pair": step_pair,
    "connect_claude": step_connect_claude,
    "chat_folder": step_chat_folder,
    "publish_offering": step_publish_offering,
}


# --- Checkpoints and scenarios --------------------------------------------------------------
def checkpoint_plan(name: str) -> tuple[dict, list[str]]:
    cp = CONF["checkpoints"][name]
    if "extends" in cp:
        persona, steps = checkpoint_plan(cp["extends"])
        return persona, steps + cp.get("steps", [])
    return CONF["personas"][cp["persona"]], list(cp.get("steps", []))


def reset_checkpoint(name: str):
    persona, steps = checkpoint_plan(name)
    who = persona["username"]
    t0 = time.time()
    gone = dev("DELETE", f"/dev/users/{who}")
    say(who, "dev account deleted" if gone.get("existed") else "no dev account yet")
    say(who, f"{persona['vm']}: fresh from {persona['snapshot']}, starting…")
    fresh_vm(persona)
    ctx: dict = {}
    for step in steps:
        fn, *args = step.split()
        s0 = time.time()
        say(who, f"{fn}: {STEPS[fn](persona, ctx, *args)} ({time.time() - s0:.0f}s)")
    say(who, f"at {name} ({time.time() - t0:.0f}s)")


# --- Preparing this Mac ------------------------------------------------------------------
def available_gb() -> float:
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = int(out.split("page size of ")[1].split()[0])
    pages = {k.strip(): int(v.strip().rstrip(".")) for k, v in (l.split(":") for l in out.splitlines()[1:] if ":" in l)}
    free = pages.get("Pages free", 0) + pages.get("Pages inactive", 0) + pages.get("Pages speculative", 0) + pages.get("Pages purgeable", 0)
    return free * page / 1024**3


def prepare_host(keep: set[str]):
    """Frees memory for the test VMs: stops other VMs, quits the apps listed
    in ~/.config/casper-vm/quit-apps, and purges disk caches if allowed."""
    for vm in running_vms() - keep:
        say("mac", f"stopping {vm}")
        tart("stop", vm, check=False)
    quit_list = CONFIG_DIR / "quit-apps"
    if quit_list.exists():
        for app in (a.strip() for a in quit_list.read_text().splitlines()):
            if app and subprocess.run(["pgrep", "-xq", app]).returncode == 0:
                subprocess.run(["osascript", "-e", f'tell application "{app}" to quit'], capture_output=True)
                say("mac", f"quit {app}")
    if subprocess.run(["sudo", "-n", "/usr/sbin/purge"], capture_output=True).returncode == 0:
        say("mac", "purged disk caches")
    need = len(keep) * VM_MEMORY_MB / 1024 + 1
    have = available_gb()
    swap = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout.split("used = ")[1].split()[0]
    say("mac", f"{have:.1f} GB memory available, {swap} in swap; the VMs need about {need:.0f} GB")
    if have < need:
        heavy = subprocess.run(["ps", "-axo", "rss=,comm="], capture_output=True, text=True).stdout.splitlines()
        top = sorted(((int(l.split(None, 1)[0]), l.split(None, 1)[1]) for l in heavy if l.strip()), reverse=True)[:5]
        say("mac", "tight on memory -- the VMs may lag. Biggest users: " + ", ".join(
            f"{Path(c).name.split('.app')[0]} ({r / 1024 / 1024:.1f} GB)" for r, c in top))


# --- CLI ----------------------------------------------------------------------------------
def run_parallel(checkpoints: list[str]):
    errors = []

    def go(cp):
        try:
            reset_checkpoint(cp)
        except Exception as e:
            errors.append(f"{cp}: {e}")
            say(checkpoint_plan(cp)[0]["username"], f"FAILED: {e}")

    threads = [threading.Thread(target=go, args=(cp,)) for cp in checkpoints]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        print("Scenarios:")
        for name, s in CONF["scenarios"].items():
            print(f"  {name:<22} {s['about']}")
        print("Checkpoints:")
        for name, c in CONF["checkpoints"].items():
            print(f"  {name:<22} {c['about']}")
        return 0
    if cmd == "stop":
        for p in CONF["personas"].values():
            if p["vm"] in running_vms():
                tart("stop", p["vm"], check=False)
                say("mac", f"stopped {p['vm']}")
        return 0
    if cmd in ("start", "reset") and len(argv) > 2:
        name = argv[2]
        checkpoints = CONF["scenarios"][name]["checkpoints"] if cmd == "start" else [name]
        vms = {checkpoint_plan(cp)[0]["vm"] for cp in checkpoints}
        if cmd == "start":
            prepare_host(keep=vms)
        errors = run_parallel(checkpoints)
        if errors:
            print("\nNot ready:\n  " + "\n  ".join(errors))
            return 1
        if cmd == "start" and CONF["scenarios"][name].get("next"):
            print("\nReady." + CONF["scenarios"][name]["next"])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
