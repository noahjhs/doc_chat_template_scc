"""`tools/winvm.py utm`: a copy of the Windows test VM as a UTM virtual machine, for
hands-on testing in UTM's own window. It is a separate copy (an instant APFS
clone of the disk, taken at windows-base): the e2e rig never touches it, and
UTM's changes never touch the rig's VM. Run it again to start over from
windows-base. Don't run it while the rig's VM is up (the disk is then in use)."""

import plistlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import winvm

CONTAINER = Path.home() / "Library" / "Containers" / "com.utmapp.UTM" / "Data" / "Documents"
NAME = "Casper Windows 11"


def config(disk_name: str) -> dict:
    return {
        "Backend": "QEMU",
        "ConfigurationVersion": 4,
        "Display": [{"Hardware": "ramfb", "DynamicResolution": False, "NativeResolution": False,
                     "UpscalingFilter": "Linear", "DownscalingFilter": "Linear"}],
        "Drive": [{"Identifier": str(uuid.uuid4()).upper(), "ImageName": disk_name, "ImageType": "Disk",
                   "Interface": "NVMe", "InterfaceVersion": 1, "ReadOnly": False}],
        "Information": {"Name": NAME, "UUID": str(uuid.uuid4()).upper(), "Icon": "windows",
                        "Notes": "Windows 11 ARM test VM (tools/winvm.py). Account noah; password in ~/.config/casper-vm/windows-password."},
        "Input": {"MaximumUsbShare": 3, "UsbBusSupport": "3.0", "UsbSharing": False},
        "Network": [{"Hardware": "virtio-net-pci", "IsolateFromHost": False, "Mode": "Emulated",
                     "PortForward": [{"Protocol": "TCP", "GuestAddress": "", "GuestPort": 22, "HostAddress": "127.0.0.1", "HostPort": 2223}]}],
        "QEMU": {"AdditionalArguments": [], "BalloonDevice": False, "DebugLog": False, "Hypervisor": True,
                 "PS2Controller": False, "RNGDevice": True, "RTCLocalTime": False, "TPMDevice": False, "TSO": False,
                 "UEFIBoot": True},
        "Serial": [],
        "Sharing": {"ClipboardSharing": True, "DirectoryShareMode": "None", "DirectoryShareReadOnly": False},
        "Sound": [],
        "System": {"Architecture": "aarch64", "CPU": "default", "CPUCount": 0, "CPUFlagsAdd": [], "CPUFlagsRemove": [],
                   "ForceMulticore": False, "JITCacheSize": 0, "MemorySize": winvm.MEMORY_MB, "Target": "virt"},
    }


def export():
    if winvm.vm_pid():
        sys.exit("The rig's Windows VM is running; stop it first (tools/winvm.py stop).")
    dest_root = CONTAINER if CONTAINER.exists() else Path.home() / "Documents"
    bundle = dest_root / f"{NAME}.utm"
    if bundle.exists():
        shutil.rmtree(bundle)
    data = bundle / "Data"
    data.mkdir(parents=True)
    disk = data / "windows.qcow2"
    subprocess.run(["cp", "-c", str(winvm.DISK), str(disk)], check=True)  # APFS clone: instant, no extra space until changed
    subprocess.run(["qemu-img", "snapshot", "-a", winvm.SNAPSHOT, str(disk)], check=True, capture_output=True)
    shutil.copyfile(winvm.VARS_BASE, data / "efi_vars.fd")
    with open(bundle / "config.plist", "wb") as f:
        plistlib.dump(config(disk.name), f)
    winvm.say(f"UTM copy at {bundle}")
    if dest_root != CONTAINER:
        winvm.say("UTM hasn't run yet: open that file in UTM (double-click) to import it")
    else:
        winvm.say("open UTM: it's in the list (account noah; password: ~/.config/casper-vm/windows-password)")
