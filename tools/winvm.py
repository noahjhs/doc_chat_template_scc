#!/usr/bin/env python3
"""The Windows 11 ARM test VM: headless QEMU (Apple's hypervisor, so near-native
speed) with its screen served over VNC and OpenSSH Server inside, the Windows
counterpart of the Tart Mac VMs. tools/scenario.py drives it as a persona;
tools/vmgui.py looks at and clicks in its screen; this file is the machinery.

    tools/winvm.py install [iso] [--yes]   one-time (--yes: replace an existing VM without asking): unattended Windows install, then the clean snapshot `windows-base`
    tools/winvm.py start           boot the VM as it is (screen on VNC, SSH on 127.0.0.1:2222)
    tools/winvm.py reset           discard changes (back to windows-base), then start
    tools/winvm.py stop            shut Windows down cleanly and release the e2e lock
    tools/winvm.py exec <powershell>   run a command in the VM over SSH
    tools/winvm.py gui <program>   start a program on the logged-in desktop (so vmgui.py sees it)
    tools/winvm.py status          running? the VNC and SSH addresses
    tools/winvm.py save            make the VM's current disk the new windows-base (VM must be stopped)
    tools/winvm.py utm             a UTM copy of the VM, for hands-on testing (the e2e rig never touches it)

Files: ~/.cache/casper-vm/windows/ (disk.qcow2 with the snapshot, UEFI variables);
~/.config/casper-vm/windows-password (the Windows account's password) and
windows_ed25519 (the SSH key); the VNC address lands in ~/.config/casper-vm/vnc/windows.json
for vmgui.py. Needs: brew install qemu cdrtools; the installer ISO is built by
UUP dump (see docs/product/windows.md, "Test VM").

Whenever the VM runs, this holds the e2e lock (~/.cache/casper-e2e.lock, the one
tools/e2e.sh holds) with QEMU's pid as holder -- so it never runs next to an
e2e run, and the lock frees itself if QEMU dies. Under tools/e2e.sh
(CASPER_E2E_LOCK_HELD=1) the run already holds it."""

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path.home() / ".config" / "casper-vm"
STATE = Path.home() / ".cache" / "casper-vm" / "windows"
DISK, VARS, VARS_BASE = STATE / "disk.qcow2", STATE / "efivars.fd", STATE / "efivars-base.fd"
PIDFILE, QMP, LOG = STATE / "qemu.pid", STATE / "qmp.sock", STATE / "qemu.log"
SNAPSHOT = "windows-base"
VM = "windows"  # the name vmgui.py knows it by
USER = "noah"
SSH_PORT = 2222
VNC_DISPLAY = 41  # port 5941
CPUS, MEMORY_MB, DISK_GB = 4, 4096, 64
QEMU_SHARE = Path("/opt/homebrew/share/qemu")
KEY = CONFIG / "windows_ed25519"
PASSWORD_FILE = CONFIG / "windows-password"
VNC_RECORD = CONFIG / "vnc" / f"{VM}.json"
LOCK = Path.home() / ".cache" / "casper-e2e.lock"
ISO_DEFAULTS = [Path.home() / "vmimg" / "windows11-arm64.iso"]
VIRTIO_ISO = Path.home() / "vmimg" / "virtio-win.iso"


def say(text: str):
    print(f"windows  {text}", flush=True)


# --- The e2e lock (same protocol as tools/lock.sh) -----------------------------------------
def _holder() -> int | None:
    try:
        return int((LOCK / "pid").read_text())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def lock_take(timeout: int = 6 * 3600):
    if os.environ.get("CASPER_E2E_LOCK_HELD"):
        return
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    said, end = False, time.time() + timeout
    while True:
        try:
            LOCK.mkdir()
            (LOCK / "pid").write_text(str(os.getpid()))
            return
        except FileExistsError:
            pid = _holder()
            if pid is not None and pid == vm_pid():
                return  # our own VM already holds it
            if pid is not None and not _alive(pid):
                say(f"e2e lock: holder {pid} is gone, taking it over")
                shutil.rmtree(LOCK, ignore_errors=True)
                continue
            if pid is None and time.time() - LOCK.stat().st_mtime > 60:
                shutil.rmtree(LOCK, ignore_errors=True)
                continue
            if not said:
                say(f"e2e lock: waiting for {pid or 'another run'} ({LOCK})")
                said = True
            if time.time() > end:
                sys.exit("gave up waiting for the e2e lock")
            time.sleep(5)


def lock_hand_to(pid: int):
    if not os.environ.get("CASPER_E2E_LOCK_HELD"):
        (LOCK / "pid").write_text(str(pid))


def lock_release():
    if not os.environ.get("CASPER_E2E_LOCK_HELD"):
        shutil.rmtree(LOCK, ignore_errors=True)


# --- Secrets ---------------------------------------------------------------------------------
def password() -> str:
    if not PASSWORD_FILE.exists():
        CONFIG.mkdir(parents=True, exist_ok=True)
        PASSWORD_FILE.write_text(secrets.token_urlsafe(12) + "\n")
        PASSWORD_FILE.chmod(0o600)
    return PASSWORD_FILE.read_text().strip()


def ensure_key():
    if not KEY.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "casper-windows-vm", "-f", str(KEY)], check=True)


# --- QEMU ------------------------------------------------------------------------------------
def vm_pid() -> int | None:
    try:
        pid = int(PIDFILE.read_text())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def qmp(*commands: dict, timeout: float = 10) -> list:
    """Sends QMP commands over the VM's control socket; returns the replies."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    s.connect(str(QMP))
    f = s.makefile("rw")
    f.readline()
    out = []
    for c in [{"execute": "qmp_capabilities"}, *commands]:
        f.write(json.dumps(c) + "\n")
        f.flush()
        while True:
            reply = json.loads(f.readline())
            if "return" in reply or "error" in reply:
                out.append(reply)
                break
    s.close()
    return out[1:]


def send_keys(*keys: str):
    qmp(*[{"execute": "send-key", "arguments": {"keys": [{"type": "qcode", "data": k}]}} for k in keys])


def qemu_args(vnc_password: str, cdroms: list[Path] = ()) -> list[str]:
    args = [
        "qemu-system-aarch64", "-name", "casper-windows",
        "-machine", "virt,highmem=on", "-accel", "hvf", "-cpu", "host", "-smp", str(CPUS), "-m", str(MEMORY_MB),
        "-drive", f"if=pflash,format=raw,file={QEMU_SHARE / 'edk2-aarch64-code.fd'},readonly=on",
        "-drive", f"if=pflash,format=raw,file={VARS}",
        "-device", "ramfb", "-device", "qemu-xhci", "-device", "usb-kbd", "-device", "usb-tablet",
        "-drive", f"file={DISK},if=none,id=sys,format=qcow2,discard=unmap,cache=writeback",
        "-device", "nvme,drive=sys,serial=casperwin,bootindex=10",
        "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{SSH_PORT}-:22", "-device", "virtio-net-pci,netdev=net0",
        "-vnc", f"127.0.0.1:{VNC_DISPLAY},password=on", "-display", "none",
        "-qmp", f"unix:{QMP},server=on,wait=off",
        "-pidfile", str(PIDFILE), "-daemonize",
    ]
    for i, iso in enumerate(cdroms):
        args += ["-drive", f"file={iso},if=none,id=cd{i},media=cdrom,readonly=on", "-device", f"usb-storage,drive=cd{i},bootindex={i}"]
    return args


def start_qemu(cdroms: list[Path] = ()):
    if vm_pid():
        return
    lock_take()
    STATE.mkdir(parents=True, exist_ok=True)
    QMP.unlink(missing_ok=True)
    vnc_pw = secrets.token_hex(4)  # VNC passwords are 8 characters at most
    r = subprocess.run(qemu_args(vnc_pw, cdroms), capture_output=True, text=True)
    if r.returncode != 0:
        lock_release()
        sys.exit(f"QEMU didn't start: {r.stderr.strip()[-400:]}")
    for _ in range(50):
        if QMP.exists():
            break
        time.sleep(0.2)
    pid = vm_pid()
    lock_hand_to(pid)
    qmp({"execute": "set_password", "arguments": {"protocol": "vnc", "password": vnc_pw}})
    VNC_RECORD.parent.mkdir(parents=True, exist_ok=True)
    VNC_RECORD.write_text(json.dumps({"password": vnc_pw, "port": 5900 + VNC_DISPLAY, "server": "qemu"}))
    VNC_RECORD.chmod(0o600)
    say(f"QEMU running (pid {pid}); screen on VNC port {5900 + VNC_DISPLAY} (tools/vmgui.py shot {VM})")


# --- SSH -------------------------------------------------------------------------------------
def ssh_cmd(*remote: str) -> list[str]:
    return ["ssh", "-i", str(KEY), "-p", str(SSH_PORT), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"{USER}@127.0.0.1", *remote]


def ssh_up() -> bool:
    try:
        return subprocess.run(ssh_cmd("echo ok"), capture_output=True, text=True, timeout=15).stdout.strip() == "ok"
    except subprocess.TimeoutExpired:
        return False


def wait_ssh(seconds: int, what: str = "Windows"):
    end = time.time() + seconds
    while time.time() < end:
        if not vm_pid():
            sys.exit(f"QEMU exited while waiting for {what} (see {LOG})")
        if ssh_up():
            return
        time.sleep(5)
    sys.exit(f"{what} didn't answer on SSH within {seconds // 60} minutes; look at the screen: tools/vmgui.py shot {VM}")


def run(script: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """A PowerShell command in the VM, over SSH. Raises on a nonzero exit."""
    r = subprocess.run(ssh_cmd(script), capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"in {VM}: {(r.stderr or r.stdout).strip()[-600:]}")
    return r


def run_gui(command: str):
    """Starts a program on the logged-in desktop (where vmgui.py sees it). A
    command run over SSH lives in its own session and shows nothing; a
    one-shot scheduled task run as the user, interactively, does."""
    tn = "casper-gui"
    # The command goes through a small .cmd file, so it may hold quotes and spaces.
    import base64
    b64 = base64.b64encode((command + "\r\n").encode()).decode()
    cmdfile = r"C:\casper-gui.cmd"
    # The start time must be well-formed; /run starts it now regardless.
    run(f"[IO.File]::WriteAllText('{cmdfile}', [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}'))); "
        f'schtasks /create /tn {tn} /sc once /st 00:00 /ru {USER} /it /rl highest /f /tr {cmdfile} | Out-Null; '
        f'schtasks /run /tn {tn} | Out-Null; Start-Sleep 2; schtasks /delete /tn {tn} /f | Out-Null')


# --- Commands --------------------------------------------------------------------------------
def start(wait: bool = True):
    if not DISK.exists():
        sys.exit("No Windows disk yet: tools/winvm.py install")
    start_qemu()
    if wait:
        wait_ssh(300)
        say("up: SSH answers")


def stop():
    pid = vm_pid()
    if not pid:
        lock_release()
        say("already stopped")
        return
    how = "shut down"
    try:
        subprocess.run(ssh_cmd("shutdown /s /t 0"), capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        pass
    for _ in range(60):
        if not vm_pid():
            break
        time.sleep(2)
    if vm_pid():
        try:
            qmp({"execute": "system_powerdown"})
        except OSError:
            pass
        for _ in range(30):
            if not vm_pid():
                break
            time.sleep(2)
    if vm_pid():
        how = "stopped (power cut: Windows didn't answer)"
        os.kill(pid, 15)
        time.sleep(2)
    lock_release()
    say(how)


def reset():
    if vm_pid():
        stop()
    if SNAPSHOT not in subprocess.run(["qemu-img", "snapshot", "-l", str(DISK)], capture_output=True, text=True).stdout:
        sys.exit(f"No snapshot {SNAPSHOT}: tools/winvm.py install")
    lock_take()
    try:
        subprocess.run(["qemu-img", "snapshot", "-a", SNAPSHOT, str(DISK)], check=True, capture_output=True)
        shutil.copyfile(VARS_BASE, VARS)
    finally:
        if not vm_pid():
            lock_release()
    say(f"back to {SNAPSHOT}, starting…")
    start()


def make_config_iso() -> Path:
    """The small ISO Setup finds by itself: the answer file, the first-logon script, the SSH public key."""
    ensure_key()
    d = Path(tempfile.mkdtemp())
    xml = (HERE / "windows" / "autounattend.xml").read_text().replace("@USER@", USER).replace("@PASSWORD@", password())
    (d / "autounattend.xml").write_text(xml)
    shutil.copy(HERE / "windows" / "casper-setup.ps1", d / "casper-setup.ps1")
    shutil.copy(str(KEY) + ".pub", d / "ssh_key.pub")
    iso = STATE / "config.iso"
    subprocess.run(["mkisofs", "-quiet", "-J", "-r", "-V", "CASPERCFG", "-o", str(iso), str(d)], check=True)
    shutil.rmtree(d)
    return iso


def install(iso: Path | None, yes: bool = False):
    iso = iso or next((p for p in ISO_DEFAULTS if p.exists()), None)
    if not iso or not iso.exists() or not VIRTIO_ISO.exists():
        sys.exit(f"Need the Windows 11 ARM ISO ({ISO_DEFAULTS[0]}) and {VIRTIO_ISO}")
    if DISK.exists() and SNAPSHOT in subprocess.run(["qemu-img", "snapshot", "-l", str(DISK)], capture_output=True, text=True).stdout \
            and not yes and input(f"{DISK} exists -- replace the VM? Type 'yes': ").strip() != "yes":
        sys.exit("kept it")
    if vm_pid():
        stop()
    shutil.rmtree(STATE, ignore_errors=True)
    STATE.mkdir(parents=True)
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(DISK), f"{DISK_GB}G"], check=True)
    shutil.copyfile(QEMU_SHARE / "edk2-arm-vars.fd", VARS)
    cfg = make_config_iso()
    t0 = time.time()
    start_qemu([iso, cfg, VIRTIO_ISO])
    # "Press any key to boot from CD": pressed for it, only in the first half minute.
    for _ in range(25):
        send_keys("ret")
        time.sleep(1)
    say("Windows Setup is running unattended (about 20-40 minutes; watch with tools/vmgui.py shot windows)")
    wait_ssh(75 * 60, "the unattended install")
    for _ in range(60):  # the setup script writes its marker last
        if "ready" in subprocess.run(ssh_cmd("if (Test-Path C:\\casper-ready) { 'ready' }"), capture_output=True, text=True).stdout:
            break
        time.sleep(5)
    else:
        sys.exit("SSH is up but the setup script never finished (C:\\casper-setup.log in the VM)")
    say(f"installed and SSH works ({time.time() - t0:.0f}s); taking the clean snapshot")
    stop()
    snapshot_base()


def snapshot_base():
    if vm_pid():
        sys.exit("Stop the VM first")
    subprocess.run(["qemu-img", "snapshot", "-d", SNAPSHOT, str(DISK)], capture_output=True)
    subprocess.run(["qemu-img", "snapshot", "-c", SNAPSHOT, str(DISK)], check=True)
    shutil.copyfile(VARS, VARS_BASE)
    say(f"saved snapshot {SNAPSHOT}")


def status():
    pid = vm_pid()
    print(f"windows: {'running (pid %d)' % pid if pid else 'stopped'}; disk {DISK if DISK.exists() else 'missing'}")
    if pid and VNC_RECORD.exists():
        print(f"VNC 127.0.0.1:{json.loads(VNC_RECORD.read_text())['port']}   SSH {USER}@127.0.0.1 -p {SSH_PORT}   password: {PASSWORD_FILE}")


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "install":
        args = [a for a in argv[2:] if a != "--yes"]
        install(Path(args[0]) if args else None, yes="--yes" in argv)
    elif cmd == "start":
        start()
    elif cmd == "reset":
        reset()
    elif cmd == "stop":
        stop()
    elif cmd == "status":
        status()
    elif cmd == "save":
        snapshot_base()
    elif cmd == "exec" and len(argv) > 2:
        r = subprocess.run(ssh_cmd(" ".join(argv[2:])), text=True)
        return r.returncode
    elif cmd == "gui" and len(argv) > 2:
        run_gui(" ".join(argv[2:]))
    elif cmd == "utm":
        import winutm
        winutm.export()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
