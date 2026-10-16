#!/usr/bin/env python3
"""Reset test VMs and their dev Casper accounts together, to known points.

    tools/scenario.py list                 what's defined (tools/scenarios.toml)
    tools/scenario.py start <name>         stop other VMs, free memory, then reset a scenario (every persona in it) or one checkpoint
    tools/scenario.py reset <name>         the same, but leave other running VMs alone
    tools/scenario.py stop [scenario]      shut every test VM down (or just the scenario's), and wipe headless personas' Macs
    tools/scenario.py newmac <persona>     a lost Mac's replacement: discard the VM, clone a fresh one; the account stays
    tools/scenario.py mail [address]       the newest email the test stack kept instead of sending
    tools/scenario.py ask <persona> [-c] "text"   talk to a headless persona's own agent (-c: carry on the conversation)
    ... --env test                         use the end-to-end test deployment (tools/test_stack.sh) instead of dev
    ... start|reset|edit <name> --vnc      serve each VM's screen over VNC instead of opening its window,
                                           so tools/vmgui.py can look at it and click (see that file)
    tools/scenario.py edit <snapshot>      open a starting snapshot itself, to change it (e.g. riley-base)
    tools/scenario.py save <snapshot>      shut it down cleanly -- the changes are saved
    tools/scenario.py copy <from> <to> ["what it is"]   duplicate a starting snapshot (instant, no extra disk until changed)
    tools/scenario.py delete <snapshot>    delete a starting snapshot (asks first; refuses if a persona uses it)
    tools/scenario.py rename <old> <new>   rename a snapshot, persona, checkpoint or scenario (references follow)
    tools/scenario.py show <name>          detail on a scenario, checkpoint, persona or snapshot
    tools/scenario.py capture <name>       save a checkpoint (or every one in a scenario) as it is right now -- VM and server together
    tools/scenario.py uncapture <checkpoint>  forget a capture: back to replaying its steps

How it works: each persona's VM is cloned fresh from a local snapshot
(copy-on-write, instant), their dev accounts are deleted, and the checkpoint's
steps are replayed. A captured checkpoint is restored instead: its VM
snapshot (cp-<checkpoint>) and its server state, exported at the same moment
-- so tweaks made by hand are kept. Steps are replayed -- inside the VM through the Tart guest agent (`tart
exec`), and on the server through dev-only endpoints (casper_service/
devtools.py). Snapshots never hold account state, so a VM and the server can
never disagree.

Needs: Tart; ~/.config/casper-vm/dev-admin-token (matches DEV_ADMIN_TOKEN in
the dev deployment's .env). Optional: ~/.config/casper-vm/quit-apps, apps to
quit before starting (one per line) to free memory."""

import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF = tomllib.loads((HERE / "scenarios.toml").read_text())
CONFIG_DIR = Path.home() / ".config" / "casper-vm"
CASPER = "/Applications/Casper.app/Contents/MacOS/Casper"
VM_CPU, VM_MEMORY_MB, VM_DISPLAY = 4, 4096, "1440x900"
_print_lock = threading.Lock()
# python.org's Python ships without root certificates; macOS has its own bundle.
_TLS = ssl.create_default_context(cafile="/etc/ssl/cert.pem" if os.path.exists("/etc/ssl/cert.pem") else None)


def say(who: str, text: str):
    with _print_lock:
        print(f"{who:>6}  {text}", flush=True)


# --- Tart -------------------------------------------------------------------------------
def tart(*args, check=True, capture=True) -> subprocess.CompletedProcess:
    return subprocess.run(["tart", *args], check=check, capture_output=capture, text=True)


def running_vms() -> set[str]:
    # `tart list` can fail for a moment while another tart command (a clone
    # starting in parallel) holds a VM's config: retry before giving up.
    for attempt in range(8):
        r = subprocess.run(["tart", "list", "--format", "json"], capture_output=True, text=True)
        if r.returncode == 0:
            return {v["Name"] for v in json.loads(r.stdout) if v.get("Running") or v.get("State") == "running"}
        time.sleep(1 + attempt)
    raise RuntimeError(f"tart list kept failing: {r.stderr.strip()[-200:]}")


def vm_exec(vm: str, script: str, timeout: int = 300) -> str:
    """Runs a shell script in the VM as its user (GUI session: Keychain and
    `open` work), via the guest agent."""
    r = subprocess.run(["tart", "exec", vm, "/bin/zsh", "-lc", script], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"in {vm}: {(r.stderr or r.stdout).strip()[-600:]}")
    return r.stdout.strip()


def _guest_agent_up(vm: str) -> bool:
    try:
        return subprocess.run(["tart", "exec", vm, "true"], capture_output=True, timeout=8).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _wait_stopped(vm: str, seconds: int) -> bool:
    for _ in range(seconds // 2):
        if vm not in running_vms():
            return True
        time.sleep(2)
    return vm not in running_vms()


def shutdown_vm(vm: str) -> str:
    """Shuts a VM down without anything inside it blocking: ends whatever is
    running in a terminal (e.g. claude) and quits Terminal -- whose "terminate
    running processes?" question otherwise stalls shutdown -- then asks macOS
    to shut down normally, so apps save their state. If something still
    holds out, it's forced from inside (shutdown -h now), and only as a last
    resort is the power cut (tart stop). Returns how it went."""
    if vm not in running_vms():
        return "already stopped"
    if _guest_agent_up(vm):
        ended = subprocess.run(["tart", "exec", vm, "/bin/sh", "-c", """
            pids=$(ps -U "$(id -u)" -o pid=,tty= | awk '$2 != "??" {print $1}')
            names=$(ps -U "$(id -u)" -o tty=,comm= | awk '$1 != "??" {print $2}' | xargs -n1 basename 2>/dev/null | grep -v -x -e zsh -e bash -e sh -e login | sort -u | tr '\\n' ' ')
            [ -n "$pids" ] && kill -TERM $pids 2>/dev/null; sleep 1
            [ -n "$pids" ] && kill -KILL $pids 2>/dev/null
            defaults write com.apple.Terminal NSQuitAlwaysKeepsWindows -bool false
            osascript -e 'if application "Terminal" is running then tell application "Terminal" to quit' 2>/dev/null
            echo "$names"
        """], capture_output=True, text=True, timeout=30).stdout.strip()
        subprocess.run(["tart", "exec", vm, "osascript", "-e", 'tell application "System Events" to shut down'],
                       capture_output=True, timeout=30)
        if _wait_stopped(vm, 60):
            return "shut down" + (f" (ended {ended} in Terminal)" if ended else "")
        pw = (CONFIG_DIR / "password").read_text().strip() if (CONFIG_DIR / "password").exists() else ""
        if pw:
            subprocess.run(["tart", "exec", "-i", vm, "sudo", "-S", "-p", "", "shutdown", "-h", "now"],
                           input=pw + "\n", capture_output=True, text=True, timeout=30)
            if _wait_stopped(vm, 40):
                return "shut down (an app was holding out, so it was forced from inside)"
    tart("stop", vm, "--timeout", "10", check=False)
    return "stopped (power cut: the VM didn't respond)"


VNC = False  # --vnc: serve each VM's screen over VNC (from the hypervisor), for tools/vmgui.py
_CHROME_TIDY = r"""
import glob, json, os
base = os.path.expanduser("~/Library/Application Support/Google/Chrome")
for f in glob.glob(base + "/*/Preferences") + [base + "/Local State"]:
    try:
        d = json.load(open(f))
        d.setdefault("profile", {})["exit_type"] = "Normal"
        d["profile"]["exited_cleanly"] = True
        d["exited_cleanly"] = True
        json.dump(d, open(f, "w"))
    except Exception:
        pass
"""


def launch(vm: str):
    """Opens the VM: its window on this Mac, or (--vnc) a VNC server whose
    address and password are written for tools/vmgui.py. A VNC-served VM has
    no window of its own; open it in Screen Sharing to watch (vmgui.py open)."""
    if not VNC:
        subprocess.Popen(["tart", "run", vm], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return
    (CONFIG_DIR / "vnc").mkdir(parents=True, exist_ok=True)
    log = CONFIG_DIR / "vnc" / f"{vm}.log"
    log.write_text("")
    subprocess.Popen(["tart", "run", vm, "--vnc-experimental"], stdout=open(log, "w"), stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(60):
        m = re.search(r"vnc://:(\S+)@127\.0\.0\.1:(\d+)", log.read_text())
        if m:
            (CONFIG_DIR / "vnc" / f"{vm}.json").write_text(json.dumps({"password": m.group(1), "port": int(m.group(2))}))
            return
        time.sleep(0.5)
    raise RuntimeError(f"{vm}: no VNC address after 30s ({log})")


def fresh_vm(persona: dict):
    vm = persona["vm"]
    if vm in running_vms():
        tart("stop", vm, "--timeout", "5", check=False)  # about to be deleted: nothing to keep
    tart("delete", vm, check=False)
    tart("clone", persona["snapshot"], vm)
    tart("set", vm, "--cpu", str(VM_CPU), "--memory", str(VM_MEMORY_MB), "--display", VM_DISPLAY)
    launch(vm)
    deadline = time.time() + 180
    while time.time() < deadline:
        if subprocess.run(["tart", "exec", vm, "true"], capture_output=True).returncode == 0:
            # Chrome, saved with the snapshot while running, would greet the
            # person with "Restore pages? Chrome didn't shut down correctly".
            subprocess.run(["tart", "exec", "-i", vm, "/usr/bin/python3", "-"], input=_CHROME_TIDY, text=True, capture_output=True)
            return
        time.sleep(3)
    raise RuntimeError(f"{vm} didn't come up (no guest agent after 3 minutes)")


# --- The deployment (dev by default; --env test for the end-to-end test stack) ------------
DEPLOY = CONF["deployments"]["dev"]


def dev(method: str, path: str, body: dict | None = None) -> dict:
    token = (CONFIG_DIR / "dev-admin-token").read_text().strip()
    req = urllib.request.Request(DEPLOY["auth_url"] + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Dev-Admin": token, "User-Agent": "casper-scenario", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60, context=_TLS) as r:
        return json.loads(r.read() or b"{}")


def mcp(token: str, tool: str, **arguments):
    """One MCP tool call as the persona; returns its result (text, or the
    tool's structured data, e.g. list_hosts' list)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}).encode()
    req = urllib.request.Request(DEPLOY["auth_url"] + "/mcp", data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream", "User-Agent": "casper-scenario"})
    with urllib.request.urlopen(req, timeout=60, context=_TLS) as r:
        result = json.loads(r.read())["result"]
    if result.get("isError"):
        raise RuntimeError(f"{tool}: {result}")
    structured = result.get("structuredContent") or {}
    return structured["result"] if "result" in structured else "\n".join(c.get("text", "") for c in result["content"])


def api(method: str, path: str, body: dict | None = None, token: str = "") -> dict:
    """A plain call to the deployment's API (as a person, with their session)."""
    req = urllib.request.Request(DEPLOY["auth_url"] + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": "casper-scenario",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=60, context=_TLS) as r:
        return json.loads(r.read() or b"{}")


# --- Headless personas ------------------------------------------------------------------
# A persona with `headless = true` has no VM: their "Mac" is
# agent/cmd/casper-testdaemon plus Syncthing, running on the mini (always on,
# no screen, nothing on this Mac's), joined to the relay and reporting presence
# like the app. Their agent is a Claude Code with only Casper's tools
# (`tools/scenario.py ask`). Local state (credentials) is kept in
# ~/.config/casper-vm/headless/<persona>.json, owner-only.
MINI = "casper-mini"
HEADLESS = CONFIG_DIR / "headless"


def mini(script: str, timeout: int = 120) -> str:
    r = subprocess.run(["ssh", MINI, script], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"on the mini: {(r.stderr or r.stdout).strip()[-600:]}")
    return r.stdout.strip()


def headless_state(p: dict) -> dict:
    f = HEADLESS / f"{p['username']}.json"
    return json.loads(f.read_text()) if f.exists() else {}


def save_headless_state(p: dict, state: dict):
    HEADLESS.mkdir(parents=True, exist_ok=True)
    os.chmod(HEADLESS, 0o700)
    f = HEADLESS / f"{p['username']}.json"
    f.write_text(json.dumps(state))
    os.chmod(f, 0o600)


def headless_stop(p: dict) -> str:
    """Stops the persona's daemon on the mini and wipes its "Mac" (Syncthing
    identity and whatever it held), so the next start is a fresh machine."""
    d = f"~/casper-headless/{p['username']}"
    mini(f"[ -f {d}/pid ] && kill $(cat {d}/pid) 2>/dev/null; sleep 2; rm -rf {d}; echo ok")
    (HEADLESS / f"{p['username']}.json").unlink(missing_ok=True)
    return "headless Mac wiped"


def headless_start(p: dict, st: dict):
    """Builds the test daemon, copies it to the mini and starts it there."""
    out = Path("/tmp/casper-testdaemon-arm64")
    subprocess.run(["go", "build", "-o", str(out), "./cmd/casper-testdaemon"], cwd=HERE.parent / "agent", check=True,
                   env={**os.environ, "GOOS": "darwin", "GOARCH": "arm64"}, capture_output=True)
    mini("mkdir -p ~/casper-headless/bin && cp -n ~/st-spike/bin/syncthing ~/casper-headless/bin/syncthing 2>/dev/null; true")
    subprocess.run(["scp", "-q", str(out), f"{MINI}:casper-headless/bin/casper-testdaemon"], check=True)
    d = f"casper-headless/{p['username']}"
    args = " ".join(f"{k} {json.dumps(v)}" for k, v in [
        ("-port", str(p.get("port", 8720))), ("-auth-domain", DEPLOY["auth_domain"]), ("-relay-domain", DEPLOY["relay_domain"]),
        ("-routing-key", st["routing_key"]), ("-username", p["username"]), ("-device-token", st["device_token"]),
        ("-command-key", st["command_key"]), ("-home", f"$HOME/{d}/home"), ("-data", f"$HOME/{d}/data"),
        ("-syncthing", "$HOME/casper-headless/bin/syncthing"), ("-st-listen", f"tcp://0.0.0.0:{p.get('st_port', 22020)}")])
    mini(f"""mkdir -p ~/{d}/home ~/{d}/data && cd ~/{d} &&
        nohup ~/casper-headless/bin/casper-testdaemon {args} > daemon.out 2> daemon.log < /dev/null & echo $! > ~/{d}/pid
        for i in $(seq 30); do grep -q ready ~/{d}/daemon.out && exit 0; sleep 1; done; tail -5 ~/{d}/daemon.log; exit 1""")


def ask(persona: str, text: str, continue_: bool = False) -> str:
    """One message to a headless persona's own agent: Claude Code with only
    Casper's tools, in its own clean config (none of this Mac's settings or
    memory), signed in with the long-lived test token."""
    p = CONF["personas"][persona]
    st = headless_state(p)
    if not st.get("agent_token"):
        raise RuntimeError(f"{persona} has no agent yet: reset a checkpoint of theirs first")
    home = HEADLESS / f"{p['username']}-claude"
    (home / "chat").mkdir(parents=True, exist_ok=True)
    mcp_config = {"mcpServers": {"casper": {"type": "http", "url": DEPLOY["auth_url"] + "/mcp",
                                            "headers": {"Authorization": f"Bearer {st['agent_token']}"}}}}
    token = mini("cat ~/.config/casper-test/claude-token")
    r = subprocess.run(["claude", *(["-c"] if continue_ else []), "-p", text, "--mcp-config", json.dumps(mcp_config),
                        "--strict-mcp-config", "--allowedTools", "mcp__casper__*", "--output-format", "text"],
                       cwd=home / "chat", capture_output=True, text=True, timeout=300,
                       env={**os.environ, "CLAUDE_CONFIG_DIR": str(home / "config"), "CLAUDE_CODE_OAUTH_TOKEN": token})
    if r.returncode != 0:
        raise RuntimeError(f"{persona}'s agent: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout.strip()


# --- Steps ------------------------------------------------------------------------------
def step_name_mac(p, ctx, *name):
    """Gives the persona's Mac a real name (what friends see), e.g.
    name_mac Riley's MacBook. Needs the VM's admin password."""
    pretty = " ".join(name)
    local = "".join(c if c.isalnum() else "-" for c in pretty.replace("'", "")).strip("-")
    if p.get("headless"):
        ctx["mac_name"] = local
        return f"{pretty} ({local})"
    pw = (CONFIG_DIR / "password").read_text().strip()
    vm_exec(p["vm"], f"""for k in ComputerName HostName LocalHostName; do
        v={json.dumps(pretty)}; [ $k = ComputerName ] || v={json.dumps(local)}
        echo {json.dumps(pw)} | sudo -S -p '' scutil --set $k "$v"; done""")
    return f"{pretty} ({local})"


def step_claude_token(p, ctx):
    """Keeps Claude Code signed in however old the snapshot is. A sign-in
    saved in a snapshot goes stale (the first clone to refresh it invalidates
    the saved copy for every later one), so each reset hands the VM the
    long-lived test token kept on the mini (~/.config/casper-test/claude-token)
    through its environment (~/.zshenv, owner-only). Never saved in a
    snapshot, never printed."""
    token = subprocess.run(["ssh", "casper-mini", "cat ~/.config/casper-test/claude-token"], capture_output=True, text=True)
    if token.returncode != 0 or not token.stdout.strip():
        raise RuntimeError("couldn't read the Claude test token on the mini (~/.config/casper-test/claude-token)")
    r = subprocess.run(["tart", "exec", "-i", p["vm"], "/bin/sh", "-c",
                        'read -r T; umask 077; printf "export CLAUDE_CODE_OAUTH_TOKEN=%s\\n" "$T" > ~/.zshenv'],
                       input=token.stdout.strip() + "\n", capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"couldn't give {p['vm']} the token: {r.stderr.strip()[-200:]}")
    return "Claude Code signed in with the test token"


def step_quiet_apps(p, ctx, *names):
    """Quits apps a snapshot happens to reopen at login (use _ for spaces), so
    a walkthrough starts with a clean desktop -- and the Principal's own
    Telegram chats and Gmail tabs aren't on screen."""
    names = [n.replace("_", " ") for n in names]
    for name in names:
        subprocess.run(["tart", "exec", p["vm"], "pkill", "-x", name], capture_output=True)
    return "quit " + ", ".join(names)


def step_install_casper(p, ctx):
    url = DEPLOY["download_url"]
    vm_exec(p["vm"], f"""
        pkill -x Casper; sleep 1
        curl -fsSL '{url}' -o /tmp/casper.zip && rm -rf /Applications/CasperGo /Applications/Casper.app &&
        unzip -oq /tmp/casper.zip -d /Applications &&
        open --env CASPER_SCRIPTED_SETUP=1 /Applications/Casper.app""")
    for _ in range(30):
        if "Casper app: running" in vm_exec(p["vm"], f"'{CASPER}' setup status 2>&1 || true"):
            return "installed and running"
        time.sleep(2)
    raise RuntimeError("Casper didn't start")


def step_create_account(p, ctx):
    if p.get("headless"):
        password = os.urandom(12).hex()
        token = api("POST", "/signup", {"username": p["username"], "password": password})["token"]
        save_headless_state(p, {"password": password, "session": token})
        return f"account {p['username']}"
    vm_exec(p["vm"], f"'{CASPER}' setup account create --username '{p['username']}'")
    return f"account {p['username']}"


def step_pair(p, ctx):
    if p.get("headless"):
        st = headless_state(p)
        rk = "rk-" + os.urandom(8).hex()
        name = ctx.get("mac_name", f"{p['username']}-mac")
        st.update(routing_key=rk, **{k: v for k, v in api("POST", "/hosts/pair", {"routing_key": rk, "hostname": name},
                                                         token=st["session"]).items() if k in ("device_token", "command_key", "host_id")})
        st["agent_token"] = ctx.setdefault("agent_token", dev("POST", f"/dev/users/{p['username']}/agent-token")["token"])
        save_headless_state(p, st)
        headless_start(p, st)
        return f"Paired: {name}, running headless on the mini"
    return vm_exec(p["vm"], f"'{CASPER}' setup pair", timeout=120).splitlines()[-1]


def step_connect_claude(p, ctx):
    vm_exec(p["vm"], f"'{CASPER}' setup agent")
    return "Claude Code has Casper's tools"


def step_chat_folder(p, ctx):
    vm_exec(p["vm"], "mkdir -p ~/chat")
    return "~/chat ready for `claude`"


def step_publish_offering(p, ctx, gb="20"):
    token = ctx.setdefault("agent_token", dev("POST", f"/dev/users/{p['username']}/agent-token")["token"])
    if p.get("headless"):
        save_headless_state(p, {**headless_state(p), "agent_token": token})
    mine = [h["host"] for h in mcp(token, "list_hosts") if h["role"] == "owner"]
    return mcp(token, "publish_offering", host=mine[0], max_gb=float(gb), kind="mirror", preview=False).split(".")[0]


STEPS = {
    "name_mac": step_name_mac,
    "quiet_apps": step_quiet_apps,
    "claude_token": step_claude_token,
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


CAPTURES = CONFIG_DIR / "captures"


def persona_accounts(persona: dict) -> list[str]:
    """Every dev account that's this persona's right now: the names matching
    its `accounts` patterns (default: just its username)."""
    names: set[str] = set()
    for pattern in persona.get("accounts", [persona["username"]]):
        names.update(dev("GET", "/dev/users?" + urllib.parse.urlencode({"glob": pattern}))["usernames"])
    return sorted(names)


def delete_accounts(persona: dict) -> list[str]:
    names = persona_accounts(persona)
    for name in names:
        dev("DELETE", f"/dev/users/{urllib.parse.quote(name)}")
    return names


def capture_of(checkpoint: str) -> dict | None:
    f = CAPTURES / f"{checkpoint}.json"
    return json.loads(f.read_text()) if f.exists() else None


def capture_snapshot(checkpoint: str) -> str:
    return f"cp-{checkpoint}"


def reset_checkpoint(name: str):
    persona, _ = checkpoint_plan(name)
    who = persona["username"]
    t0 = time.time()
    chain = checkpoint_chain(name)
    # The deepest captured checkpoint on the way here, if any: restore it,
    # then replay only the steps after it.
    at = max((i for i, (cp, _) in enumerate(chain) if capture_of(cp)), default=None)
    if persona.get("headless"):
        gone = delete_accounts(persona)
        say(who, f"dev accounts deleted: {', '.join(gone)}" if gone else "no dev accounts yet")
        say(who, headless_stop(persona))
        steps = [s for _, steps in chain for s in steps]
    elif at is not None:
        cp = chain[at][0]
        cap = capture_of(cp)
        delete_accounts(persona)  # including ones made since the capture
        exports = cap.get("exports") or [cap["export"]]
        dev("POST", "/dev/users/import", {"exports": exports})
        say(who, f"server state restored from the {cp} capture ({cap['captured_at'][:16]})")
        say(who, f"{persona['vm']}: from {cap['snapshot']}, starting…")
        fresh_vm({**persona, "snapshot": cap["snapshot"]})
        steps = [s for _, steps in chain[at + 1:] for s in steps]
        if any("install_casper" in s for _, steps_ in chain[:at + 1] for s in steps_):
            steps = ["install_casper"] + steps  # keep the app current; its sign-in survives
    else:
        gone = delete_accounts(persona)
        say(who, f"dev accounts deleted: {', '.join(gone)}" if gone else "no dev accounts yet")
        say(who, f"{persona['vm']}: fresh from {persona['snapshot']}, starting…")
        fresh_vm(persona)
        steps = [s for _, steps in chain for s in steps]
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


def prepare_host(keep: set[str], stop_others: bool = True):
    """Frees memory for the test VMs: stops other VMs (for `start`), quits
    the apps listed in ~/.config/casper-vm/quit-apps, and purges disk caches
    if allowed."""
    for vm in (running_vms() - keep) if stop_others else ():
        say("mac", f"{vm}: {shutdown_vm(vm)}")  # may be a snapshot being edited: shut it down properly
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
        label = lambda c: "a running VM" if "Virtualization.VirtualMachine" in c else Path(c.split(".app/")[0]).name  # noqa: E731
        say("mac", "tight on memory -- the VMs may lag. Biggest users: " + ", ".join(
            f"{label(c)} ({r / 1024 / 1024:.1f} GB)" for r, c in top))


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


def checkpoint_chain(name: str) -> list[tuple[str, list[str]]]:
    """[(checkpoint, its own steps)], base first."""
    cp = CONF["checkpoints"][name]
    return (checkpoint_chain(cp["extends"]) if "extends" in cp else []) + [(name, list(cp.get("steps", [])))]


def describe(name: str) -> str:
    lines = []
    snapshots, personas = CONF["snapshots"], CONF["personas"]
    vms = {v["Name"]: v for v in json.loads(tart("list", "--format", "json").stdout)}

    def snapshot_line(snap):
        state = "missing!" if snap not in vms else ("running" if vms[snap].get("Running") else "saved")
        return f"{snap} ({state}) -- {snapshots.get(snap, 'not in scenarios.toml')}"

    def checkpoint_lines(cp, indent="  "):
        persona, _ = checkpoint_plan(cp)
        out = [f"{indent}{cp}: {CONF['checkpoints'][cp]['about']}",
               f"{indent}  persona  {persona['username']} (" + ("headless, on the mini" if persona.get("headless") else f"VM {persona['vm']}")
               + f", dev account {persona['username']})",
               f"{indent}  starts   " + ("a wiped headless Mac" if persona.get("headless") else snapshot_line(persona["snapshot"])),
               f"{indent}  then     delete their dev accounts ({', '.join(persona.get('accounts', [persona['username']]))}), and:"]
        chain = checkpoint_chain(cp)
        at = max((i for i, (c, _) in enumerate(chain) if capture_of(c)), default=None)
        if at is not None:
            cap = capture_of(chain[at][0])
            out[-2:] = [f"{indent}  starts   the {chain[at][0]} capture ({cap['captured_at'][:16]}): VM {cap['snapshot']} and its server state",
                        f"{indent}  then     update Casper to the latest build" + (", and:" if any(s for _, s in chain[at + 1:]) else "")]
            chain = chain[at + 1:]
        n = 0
        for src, steps in chain:
            for step in steps:
                n += 1
                out.append(f"{indent}    {n}. {step}" + (f"   (from {src})" if src != cp else ""))
        if not n:
            out.append(f"{indent}    (no steps)")
        return out

    if name in CONF["scenarios"]:
        sc = CONF["scenarios"][name]
        lines += [f"scenario {name}: {sc['about']}", "starts, in parallel:"]
        for cp in sc["checkpoints"]:
            lines += checkpoint_lines(cp)
        if sc.get("next"):
            lines += ["then:"] + ["  " + l for l in sc["next"].strip().splitlines()]
    elif name in CONF["checkpoints"]:
        lines += ["checkpoint"] + checkpoint_lines(name, "")
        used = [s for s, sc in CONF["scenarios"].items() if name in sc["checkpoints"]]
        lines.append(f"in scenarios: {', '.join(used) or 'none'}")
    elif name in personas:
        p = personas[name]
        lines += [f"persona {name}: " + ("headless, on the mini" if p.get("headless") else f"VM {p['vm']}") + f", dev account {p['username']}",
                  "starts " + ("a wiped headless Mac" if p.get("headless") else snapshot_line(p["snapshot"])),
                  "checkpoints: " + ", ".join(c for c in CONF["checkpoints"] if checkpoint_plan(c)[0] is p)]
    elif name in snapshots or name in vms:
        users = [n for n, p in personas.items() if p.get("snapshot") == name]
        lines += [f"snapshot {snapshot_line(name)}", f"used by: {', '.join(users) or 'no persona'}"]
    else:
        lines.append(f"Nothing called {name!r}. Try: tools/scenario.py list")
    return "\n".join(lines)


def rename(old: str, new: str) -> int:
    """Renames a snapshot, persona, checkpoint or scenario, and every
    reference to it in scenarios.toml."""
    import re

    if not re.fullmatch(r"[A-Za-z0-9_-]+", new):
        print("Names are letters, digits, - and _.")
        return 2
    kinds = [k for k, table in (("snapshot", CONF["snapshots"]), ("persona", CONF["personas"]),
                                ("checkpoint", CONF["checkpoints"]), ("scenario", CONF["scenarios"])) if old in table]
    if not kinds:
        print(f"Nothing called {old!r}. Try: tools/scenario.py list")
        return 2
    if len(kinds) > 1:
        print(f"{old!r} is a {' and a '.join(kinds)} -- rename it in tools/scenarios.toml by hand.")
        return 2
    kind = kinds[0]
    if new in {"snapshot": CONF["snapshots"], "persona": CONF["personas"], "checkpoint": CONF["checkpoints"], "scenario": CONF["scenarios"]}[kind]:
        print(f"There's already a {kind} called {new!r}.")
        return 2
    if kind == "snapshot":
        existing = tart("list", "-q").stdout.split()
        if new in existing:
            print(f"A VM called {new!r} already exists.")
            return 2
        if old in running_vms():
            print(f"{old} is running -- save it first (tools/scenario.py save {old}).")
            return 2
        if old in existing:
            tart("rename", old, new)
    toml = HERE / "scenarios.toml"
    q = re.escape(old)
    out = []
    for line in toml.read_text().splitlines(keepends=True):
        if kind == "snapshot":
            # Snapshot names are distinctive, so mentions in descriptions and
            # comments follow too -- whole names only (renaming casper-base
            # leaves casper-base-claude alone).
            line = re.sub(rf"(?<![\w-]){q}(?![\w-])", new, line)
        else:
            table = {"persona": "personas", "checkpoint": "checkpoints", "scenario": "scenarios"}[kind]
            line = re.sub(rf"^\[{table}\.{q}\]", f"[{table}.{new}]", line)
            if kind == "persona":
                line = re.sub(rf'^(persona = )"{q}"', rf'\1"{new}"', line)
            if kind == "checkpoint" and re.match(r"^(extends|checkpoints) = ", line):
                line = re.sub(rf'"{q}"', f'"{new}"', line)
        out.append(line)
    text = "".join(out)
    tomllib.loads(text)  # never write a config that doesn't parse
    toml.write_text(text)
    if kind == "checkpoint" and (cap := capture_of(old)):
        tart("rename", capture_snapshot(old), capture_snapshot(new), check=False)
        cap.update(checkpoint=new, snapshot=capture_snapshot(new))
        (CAPTURES / f"{new}.json").write_text(json.dumps(cap))
        os.chmod(CAPTURES / f"{new}.json", 0o600)
        (CAPTURES / f"{old}.json").unlink()
    say("mac", f"renamed {kind} {old} to {new}")
    if kind == "persona":
        p = CONF["personas"][old]
        say("mac", f"(its VM is still {p['vm']} and its dev account still {p['username']} -- change those in tools/scenarios.toml if you like)")
    return 0


def capture(name: str) -> int:
    """Saves checkpoints as they are now: each persona's VM (shut down
    cleanly, then snapshotted) and their server state, exported while the
    VMs are off so nothing changes in between. Then starts the VMs again."""
    checkpoints = CONF["scenarios"][name]["checkpoints"] if name in CONF["scenarios"] else [name]
    if any(cp not in CONF["checkpoints"] for cp in checkpoints):
        print(f"No scenario or checkpoint called {name!r}.")
        return 2
    existing = tart("list", "-q").stdout.split()
    personas = [(cp, checkpoint_plan(cp)[0]) for cp in checkpoints]
    if any(p.get("headless") for _, p in personas):
        print("Headless personas can't be captured (their Mac is rebuilt in seconds anyway).")
        return 2
    missing = [p["vm"] for _, p in personas if p["vm"] not in existing]
    if missing:
        print(f"Nothing to capture: {', '.join(missing)} doesn't exist (start the scenario first).")
        return 2
    for _, p in personas:
        say(p["username"], f"{p['vm']}: {shutdown_vm(p['vm'])}")
    when = time.strftime("%Y-%m-%dT%H:%M:%S")
    exports = {cp: [dev("GET", f"/dev/users/{urllib.parse.quote(n)}/export") for n in persona_accounts(p)] for cp, p in personas}
    CAPTURES.mkdir(parents=True, exist_ok=True)
    os.chmod(CAPTURES, 0o700)
    for cp, p in personas:
        snap = capture_snapshot(cp)
        tart("delete", snap, check=False)
        tart("clone", p["vm"], snap)
        f = CAPTURES / f"{cp}.json"
        f.write_text(json.dumps({"checkpoint": cp, "persona": p["username"], "captured_at": when, "group": when,
                                 "snapshot": snap, "exports": exports[cp]}))
        os.chmod(f, 0o600)  # holds the persona's credential hashes
        say(p["username"], f"captured {cp}: VM as {snap}, plus its server state")
    for _, p in personas:
        launch(p["vm"])
    say("mac", "VMs starting again; resets of these checkpoints now restore this moment")
    return 0


def uncapture(checkpoint: str) -> int:
    if not capture_of(checkpoint):
        print(f"{checkpoint} isn't captured.")
        return 2
    tart("delete", capture_snapshot(checkpoint), check=False)
    (CAPTURES / f"{checkpoint}.json").unlink()
    say("mac", f"{checkpoint}: capture forgotten; resets replay its steps again")
    return 0


def main(argv: list[str]) -> int:
    global VNC, DEPLOY
    VNC = "--vnc" in argv
    argv = [a for a in argv if a != "--vnc"]
    if "--env" in argv:
        i = argv.index("--env")
        DEPLOY = CONF["deployments"][argv[i + 1]]
        argv = argv[:i] + argv[i + 2:]
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        print("Scenarios:")
        for name, s in CONF["scenarios"].items():
            print(f"  {name:<22} {s['about']}")
        print("Checkpoints:")
        for name, c in CONF["checkpoints"].items():
            cap = capture_of(name)
            print(f"  {name:<22} {c['about']}" + (f"  [captured {cap['captured_at'][:16]}]" if cap else ""))
        print("Personas:")
        for name, p in CONF["personas"].items():
            print(f"  {name:<22} " + ("headless, on the mini" if p.get("headless") else f"VM {p['vm']}, from snapshot {p['snapshot']}"))
        print("Snapshots:")
        for name, about in CONF["snapshots"].items():
            print(f"  {name:<22} {about}")
        print("\nDetail on any of these: tools/scenario.py show <name>")
        return 0
    if cmd == "capture" and len(argv) > 2:
        return capture(argv[2])
    if cmd == "uncapture" and len(argv) > 2:
        return uncapture(argv[2])
    if cmd == "show" and len(argv) > 2:
        print(describe(argv[2]))
        return 0
    if cmd == "rename" and len(argv) > 3:
        return rename(argv[2], argv[3])
    if cmd == "delete" and len(argv) > 2:
        snap = argv[2]
        if snap.startswith("cp-") and capture_of(snap[3:]):
            print(f"{snap} is the capture of {snap[3:]} -- use: tools/scenario.py uncapture {snap[3:]}")
            return 2
        users = [n for n, p in CONF["personas"].items() if p.get("snapshot") == snap]
        if users:
            print(f"{snap} is the starting point for {', '.join(users)} -- point them elsewhere in tools/scenarios.toml first.")
            return 2
        if snap in running_vms():
            print(f"{snap} is running -- save or stop it first.")
            return 2
        if snap not in tart("list", "-q").stdout.split():
            print(f"No snapshot {snap!r}.")
            return 2
        if input(f"Delete snapshot {snap}? This can't be undone. Type its name to confirm: ").strip() != snap:
            print("Not deleted.")
            return 1
        tart("delete", snap)
        toml = HERE / "scenarios.toml"
        toml.write_text("".join(l for l in toml.read_text().splitlines(keepends=True) if not l.startswith(f"{snap} = ")))
        say("mac", f"deleted {snap}")
        return 0
    if cmd == "edit" and len(argv) > 2:
        snap = argv[2]
        if snap not in CONF["snapshots"]:
            print(f"No snapshot {snap!r}; known: {', '.join(CONF['snapshots'])}")
            return 2
        launch(snap)
        say("mac", f"{snap} is opening. Changes you make in it are kept; it holds only what's local to a Mac -- "
                   f"no Casper account. When done: tools/scenario.py save {snap}")
        return 0
    if cmd == "save" and len(argv) > 2:
        snap = argv[2]
        if snap not in running_vms():
            say("mac", f"{snap} isn't running -- nothing to save (it's saved whenever it's shut down).")
            return 0
        how = shutdown_vm(snap)
        if snap in running_vms() or how.startswith("stopped"):
            say("mac", f"{snap}: {how} -- changes since its last clean shutdown may be lost")
            return 1
        say("mac", f"saved {snap} ({how}); every reset from it now starts here")
        return 0
    if cmd == "copy" and len(argv) > 3:
        src, dst = argv[2], argv[3]
        about = argv[4] if len(argv) > 4 else f"A copy of {src}."
        if src not in CONF["snapshots"]:
            print(f"No snapshot {src!r}; known: {', '.join(CONF['snapshots'])}")
            return 2
        if dst in CONF["snapshots"] or tart("list", "-q").stdout.split().count(dst):
            print(f"{dst!r} already exists.")
            return 2
        if src in running_vms():
            print(f"{src} is running -- save it first (tools/scenario.py save {src}).")
            return 2
        tart("clone", src, dst)
        toml = HERE / "scenarios.toml"
        text = toml.read_text()
        at = text.index("[snapshots]")
        end = text.find("\n\n", at)
        text = text[:end] + f"\n{dst} = {json.dumps(about)}" + text[end:]
        toml.write_text(text)
        say("mac", f"{dst} is a copy of {src}. To change it: tools/scenario.py edit {dst}; to use it, set a persona's "
                   f"snapshot = \"{dst}\" in tools/scenarios.toml")
        return 0
    if cmd == "stop":
        only = argv[2] if len(argv) > 2 else None  # a scenario: just its personas
        personas = ([checkpoint_plan(cp)[0] for cp in CONF["scenarios"][only]["checkpoints"]] if only in CONF["scenarios"]
                    else CONF["personas"].values())
        for p in personas:
            if p.get("headless"):
                if headless_state(p):
                    say(p["username"], headless_stop(p))
            elif p["vm"] in running_vms():
                say("mac", f"{p['vm']}: {shutdown_vm(p['vm'])}")
        return 0
    if cmd == "newmac" and len(argv) > 2:
        # A replacement Mac for a persona whose Mac was "lost": the VM is
        # discarded and a fresh one cloned from the same snapshot. Unlike
        # reset, the persona's Casper account (and the server's rows) stay.
        persona = CONF["personas"].get(argv[2])
        if not persona or persona.get("headless"):
            print(f"{argv[2]!r} isn't a persona with a VM: {', '.join(n for n, p in CONF['personas'].items() if not p.get('headless'))}")
            return 2
        prepare_host(keep={persona["vm"]}, stop_others=False)
        say(persona["username"], f"{persona['vm']}: the old Mac is gone; a new one from {persona['snapshot']}, starting…")
        fresh_vm(persona)
        ctx: dict = {}
        for step in persona.get("newmac_steps", []):
            fn, *args = step.split()
            say(persona["username"], f"{fn}: {STEPS[fn](persona, ctx, *args)}")
        return 0
    if cmd == "mail":
        # The newest email the deployment kept instead of sending (MAIL_SINK: the test stack).
        to = argv[2] if len(argv) > 2 else ""
        msgs = dev("GET", "/dev/mail?" + urllib.parse.urlencode({"to": to}))["messages"]
        if not msgs:
            print("No email kept" + (f" for {to}" if to else "") + " (only the test stack keeps email: --env test).")
            return 1
        m = msgs[0]
        print(f"To: {m['to']}\nFrom: {m['from']}\nSubject: {m['subject']}\n\n{m['text']}")
        return 0
    if cmd == "ask" and len(argv) > 3:
        cont = "-c" in argv
        rest = [a for a in argv[2:] if a != "-c"]
        print(ask(rest[0], rest[1], continue_=cont))
        return 0
    if cmd in ("start", "reset") and len(argv) > 2:
        # Either takes a scenario (every persona in it) or one checkpoint.
        name = argv[2]
        if name in CONF["scenarios"]:
            checkpoints = CONF["scenarios"][name]["checkpoints"]
        elif name in CONF["checkpoints"]:
            checkpoints = [name]
        else:
            print(f"No scenario or checkpoint called {name!r}.\n"
                  f"  scenarios:   {', '.join(CONF['scenarios'])}\n  checkpoints: {', '.join(CONF['checkpoints'])}")
            return 2
        prepare_host(keep={checkpoint_plan(cp)[0]["vm"] for cp in checkpoints if not checkpoint_plan(cp)[0].get("headless")},
                     stop_others=cmd == "start")
        errors = run_parallel(checkpoints)
        if errors:
            print("\nNot ready:\n  " + "\n  ".join(errors))
            return 1
        if name in CONF["scenarios"] and CONF["scenarios"][name].get("next"):
            print("\nReady.\n" + CONF["scenarios"][name]["next"].strip())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
