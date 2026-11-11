"""Smoke tests for Lubus 5.0 features (run: python3 tests/test_v5_features.py)."""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.argv = ["lubus"]

failures = []


def check(name, cond, detail=""):
    print(("  PASS: " if cond else "  FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


print("=== VERSION ===")
import lubus
check("project version is 6.0.0", lubus.__version__ == "6.0.0", lubus.__version__)

print("\n=== INSTALL CONFIG VALIDATION ===")
from lubus.core.direct_install import (
    InstallConfig, build_linux_layout, validate_install_config,
)
from lubus.core.windows_install import build_windows_layout
from lubus.core.images import FileSystem, ImageType, PartitionScheme, TargetSystem
from lubus.core.partitions import PartitionType

GiB = 1024 ** 3
live = InstallConfig()
check("live mode needs no account validation",
      validate_install_config(live, 64 * GiB) == [])

valid = InstallConfig(
    mode="direct", username="alice", password="secret",
    hostname="alice-pc", timezone="UTC", filesystem="ext4",
)
check("valid direct install config", validate_install_config(valid, 64 * GiB) == [])

bad_user = InstallConfig(mode="direct", username="Alice Smith",
                         password="x", hostname="alice-pc")
check("invalid username rejected",
      any("Username" in e for e in validate_install_config(bad_user, 64 * GiB)))

no_password = InstallConfig(mode="direct", username="alice", password="",
                            hostname="alice-pc")
check("missing password rejected",
      any("password" in e.lower() for e in validate_install_config(no_password, 64 * GiB)))

too_big = InstallConfig(
    mode="direct", partition_mode="custom", username="alice",
    password="secret", hostname="alice-pc", root_gib=128,
)
check("oversized custom layout rejected",
      any("does not fit" in e or "needs" in e
          for e in validate_install_config(too_big, 64 * GiB)))

print("\n=== LINUX PARTITION LAYOUT ===")
auto = build_linux_layout(valid, PartitionScheme.GPT, TargetSystem.UEFI,
                          "LUBUS", 64 * GiB)
check("UEFI auto layout has ESP + root", len(auto.partitions) == 2)
check("ESP is first", auto.partitions[0].type == PartitionType.EFI_SYSTEM)
check("auto root uses remaining space", auto.partitions[1].size == 0)

custom = InstallConfig(
    mode="direct", partition_mode="custom", username="alice",
    password="secret", hostname="alice-pc", root_gib=32,
)
custom_layout = build_linux_layout(custom, PartitionScheme.GPT,
                                   TargetSystem.UEFI, "LUBUS", 64 * GiB)
check("custom root gets the requested size",
      custom_layout.partitions[1].size == 32 * GiB,
      str(custom_layout.partitions[1].size))

bios = build_linux_layout(valid, PartitionScheme.MBR, TargetSystem.BIOS,
                          "LUBUS", 64 * GiB)
check("BIOS layout has a single root",
      len(bios.partitions) == 1 and bios.partitions[0].flags == ["boot"])

print("\n=== WINDOWS DIRECT-INSTALL LAYOUT ===")
win_cfg = InstallConfig(
    mode="direct", partition_mode="auto", username="alice", password="secret",
    hostname="alice-pc", timezone="UTC", filesystem="ntfs",
    boot_mode="uefi", esp_mib=512,
)
win_layout = build_windows_layout(win_cfg, 128 * GiB)
check("Windows UEFI has ESP + MSR + Windows",
      len(win_layout.partitions) == 3)
check("Windows UEFI uses GPT",
      win_layout.scheme == PartitionScheme.GPT)
check("MSR partition present",
      win_layout.partitions[1].type == PartitionType.MICROSOFT_RESERVED)
check("Windows NTFS partition present",
      win_layout.partitions[2].fs == FileSystem.NTFS)

win_custom = InstallConfig(
    mode="direct", partition_mode="custom", username="alice", password="secret",
    hostname="alice-pc", timezone="UTC", filesystem="ntfs",
    boot_mode="uefi", esp_mib=256, root_gib=64,
)
win_custom_layout = build_windows_layout(win_custom, 128 * GiB)
check("custom ESP size applied",
      win_custom_layout.partitions[0].size == 256 * 1024**2)
check("custom Windows size applied",
      win_custom_layout.partitions[2].size == 64 * GiB)

win_bios = InstallConfig(
    mode="direct", partition_mode="custom", username="alice", password="secret",
    hostname="alice-pc", timezone="UTC", filesystem="ntfs",
    boot_mode="bios", root_gib=64,
)
bios_layout = build_windows_layout(win_bios, 128 * GiB)
check("BIOS layout is MBR with one active NTFS partition",
      bios_layout.scheme == PartitionScheme.MBR
      and len(bios_layout.partitions) == 1
      and bios_layout.partitions[0].flags == ["boot"]
      and bios_layout.partitions[0].fs == FileSystem.NTFS)

bad_win = InstallConfig(
    mode="direct", partition_mode="auto", username="alice", password="secret",
    hostname="alice-pc", timezone="UTC", filesystem="ntfs",
    boot_mode="uefi", esp_mib=8,
)
check("invalid ESP size rejected",
      any("EFI System Partition" in e
          for e in validate_install_config(bad_win, 128 * GiB,
                                           ImageType.WINDOWS_INSTALLER)))

print("\n=== PROVISIONING GENERATORS ===")
from lubus.core.provisioning import (
    build_unattend_xml, build_cloud_init_user_data,
    write_windows_provisioning, write_linux_provisioning,
    write_installer_autounattend,
)
from lubus.core.windows_debloat import (
    _wimlib_delete_commands, defender_first_logon_commands,
    remove_defender_from_wim,
)

win_prov = InstallConfig(
    mode="direct", username="alice", password="secret", hostname="alice-pc",
    timezone="UTC", language="en-US", keyboard="0409:00000409",
    bypass_win11=True,
)
unattend = build_unattend_xml(win_prov.to_dict())
check("unattend contains username", "<Name>alice</Name>" in unattend)
check("unattend contains hostname", "<ComputerName>alice-pc</ComputerName>" in unattend)
check("unattend contains timezone", "<TimeZone>UTC</TimeZone>" in unattend)
check("unattend contains language/keyboard",
      "<UILanguage>en-US</UILanguage>" in unattend
      and "<InputLocale>0409:00000409</InputLocale>" in unattend)
check("unattend skips OOBE account screens",
      "<HideOnlineAccountScreens>true</HideOnlineAccountScreens>" in unattend)
check("unattend disables telemetry",
      "AllowTelemetry" in unattend)
check("unattend includes Win11 bypass",
      "BypassTPMCheck" in unattend and "BypassSecureBootCheck" in unattend)
check("unattend includes BypassNRO",
      "BypassNRO" in unattend)
check("unattend accepts EULA",
      "<AcceptEula>true</AcceptEula>" in unattend)
check("unattend keeps local account creation",
      "<LocalAccount" in unattend and "<Name>alice</Name>" in unattend)

_no_oobe = build_unattend_xml({"skip_oobe": False, "remove_defender": False})
check("skip_oobe off omits OOBE privacy settings",
      "HideOnlineAccountScreens" not in _no_oobe
      and "AllowTelemetry" not in _no_oobe)
_defender_xml = build_unattend_xml({"skip_oobe": False, "remove_defender": True})
check("Defender services disabled on first logon",
      "WinDefend" in _defender_xml
      and "WdNisSvc" in _defender_xml
      and "Sense" in _defender_xml)
check("Defender payload paths cover Program Files",
      "Windows Defender" in _wimlib_delete_commands())
check("Defender service command list is complete",
      any("WinDefend" in c for c in defender_first_logon_commands())
      and any("Sense" in c for c in defender_first_logon_commands()))

cloud = build_cloud_init_user_data(valid.to_dict())
check("cloud-init contains user", "name: alice" in cloud)
check("cloud-init contains hostname", "hostname: alice-pc" in cloud)

tmp = tempfile.mkdtemp(prefix="lubus_provision_")
try:
    win_root = os.path.join(tmp, "win")
    os.makedirs(os.path.join(win_root, "Windows", "Panther"))
    write_windows_provisioning(win_root, valid.to_dict())
    check("windows unattend written",
          os.path.isfile(os.path.join(win_root, "unattend.xml")))
    check("windows autounattend written",
          os.path.isfile(os.path.join(win_root, "autounattend.xml")))
    check("windows panther unattend written",
          os.path.isfile(os.path.join(win_root, "Windows", "Panther", "unattend.xml")))
    check("windows panther autounattend written",
          os.path.isfile(os.path.join(win_root, "Windows", "Panther", "autounattend.xml")))
    check("windows metadata written",
          os.path.isfile(os.path.join(win_root, "LubusInstall.json")))

    installer_root = os.path.join(tmp, "usb")
    os.makedirs(installer_root)
    write_installer_autounattend(installer_root, win_prov.to_dict())
    check("installer USB autounattend written",
          os.path.isfile(os.path.join(installer_root, "autounattend.xml")))

    # DISM helper contract: a wrapper should receive the WIM and indexes.
    fake_dism = os.path.join(tmp, "fake_dism.sh")
    dism_log = os.path.join(tmp, "dism.log")
    with open(fake_dism, "w") as f:
        f.write(f"#!/bin/sh\necho \"$@\" > {dism_log}\nexit 0\n")
    os.chmod(fake_dism, 0o755)
    old_dism = os.environ.get("LUBUS_DISM")
    os.environ["LUBUS_DISM"] = fake_dism
    try:
        dummy = os.path.join(tmp, "dummy.wim")
        open(dummy, "wb").write(b"x")
        remove_defender_from_wim(dummy, [1, 2])
        check("DISM helper receives WIM and indexes",
              os.path.isfile(dism_log)
              and "dummy.wim" in open(dism_log).read()
              and "1,2" in open(dism_log).read())
    finally:
        if old_dism is None:
            os.environ.pop("LUBUS_DISM", None)
        else:
            os.environ["LUBUS_DISM"] = old_dism

    lin_root = os.path.join(tmp, "linux")
    os.makedirs(os.path.join(lin_root, "etc"))
    write_linux_provisioning(lin_root, valid.to_dict())
    seed = os.path.join(lin_root, "var", "lib", "cloud", "seed", "nocloud-net")
    check("cloud-init seed written",
          os.path.isfile(os.path.join(seed, "user-data"))
          and os.path.isfile(os.path.join(seed, "meta-data")))
    check("linux metadata written",
          os.path.isfile(os.path.join(lin_root, "lubus-install.json")))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n=== WINDOWS BOOT CONFIGURATION ===")
from lubus.core.windows_boot import configure_uefi_boot, configure_bios_boot

boot_tmp = tempfile.mkdtemp(prefix="v5_boot_")
try:
    esp = os.path.join(boot_tmp, "esp")
    win = os.path.join(boot_tmp, "win")
    efi_src = os.path.join(boot_tmp, "EFI")
    bios_src = os.path.join(boot_tmp, "PCAT")
    os.makedirs(os.path.join(esp, "EFI"))
    os.makedirs(win)
    os.makedirs(efi_src)
    os.makedirs(bios_src)
    open(os.path.join(efi_src, "bootmgfw.efi"), "wb").write(b"EFI")
    open(os.path.join(bios_src, "bootmgr"), "wb").write(b"BIOS")

    bcd_template = os.path.join(boot_tmp, "template.bcd")
    open(bcd_template, "wb").write(b"BCD-TEMPLATE")
    old_template = os.environ.get("LUBUS_BCD_TEMPLATE")
    old_bcdboot = os.environ.get("LUBUS_BCDBOOT")
    os.environ["LUBUS_BCD_TEMPLATE"] = bcd_template
    os.environ.pop("LUBUS_BCDBOOT", None)
    try:
        configure_uefi_boot(win, esp, efi_src)
        check("UEFI boot manager installed",
              os.path.isfile(os.path.join(esp, "EFI", "Microsoft", "Boot",
                                          "bootmgfw.efi"))
              and os.path.isfile(os.path.join(esp, "EFI", "Boot", "bootx64.efi")))
        check("UEFI BCD template installed",
              os.path.isfile(os.path.join(esp, "EFI", "Microsoft", "Boot", "BCD")))
        configure_bios_boot(win, bios_src)
        check("BIOS bootmgr installed",
              os.path.isfile(os.path.join(win, "bootmgr")))
        check("BIOS BCD template installed",
              os.path.isfile(os.path.join(win, "Boot", "BCD")))
    finally:
        if old_template is None:
            os.environ.pop("LUBUS_BCD_TEMPLATE", None)
        else:
            os.environ["LUBUS_BCD_TEMPLATE"] = old_template
        if old_bcdboot is not None:
            os.environ["LUBUS_BCDBOOT"] = old_bcdboot
finally:
    shutil.rmtree(boot_tmp, ignore_errors=True)

print("\n=== MULTI-DRIVE PROGRESS ===")
from lubus.core.multi_flash import AggregateProgress
events = []
agg = AggregateProgress(2, lambda f, d, t: events.append((f, d, t)))
c0, c1 = agg.channel(0), agg.channel(1)
c0(50, 100)
c1(25, 100)
check("aggregate combines drive bytes",
      abs(events[-1][0] - 0.375) < 1e-9, str(events[-1]))
check("aggregate carries byte totals",
      events[-1][1:] == (75, 200), str(events[-1]))
agg.reset()
c0(100, 100)
c1(100, 100)
check("aggregate reaches 100%",
      abs(events[-1][0] - 1.0) < 1e-9, str(events[-1]))

print("\n=== LINUX PORTABLE DEPLOYMENT (mocked UDisks) ===")
import lubus.core.direct_install as di

# Tiny fake ISO tree + mocked target partitions.
iso_dir = tempfile.mkdtemp(prefix="v5_iso_")
esp_dir = tempfile.mkdtemp(prefix="v5_esp_")
root_dir = tempfile.mkdtemp(prefix="v5_root_")
os.makedirs(os.path.join(iso_dir, "EFI", "BOOT"))
os.makedirs(os.path.join(iso_dir, "casper"))
os.makedirs(os.path.join(root_dir, "etc"))
open(os.path.join(iso_dir, "EFI", "BOOT", "BOOTX64.EFI"), "wb").write(b"E" * 1024)
open(os.path.join(iso_dir, "casper", "vmlinuz"), "wb").write(b"K" * 2048)
open(os.path.join(iso_dir, "casper", "initrd"), "wb").write(b"I" * 2048)

parts = []
def fake_create_partition(conn, device, offset, size, ptype, name, fs, label, scheme):
    part = f"/fake/part{len(parts) + 1}"
    parts.append(part)
    return part
def fake_mount(conn, part, readonly=False):
    return esp_dir if part.endswith("1") else root_dir

di.bus = lambda: object()
di.resolve = lambda conn, dev: dev
di.unmount_all = lambda conn, dev: None
di.mount_filesystem = fake_mount
di.unmount_filesystem = lambda conn, part: None
di.create_partition = fake_create_partition
di.wipe_partition_table = lambda conn, dev, scheme: None
di.set_partition_active = lambda conn, part: None
di.get_device_size = lambda dev: 64 * GiB
di.release_stale_iso_mounts = lambda path=None: 0
di.install_grub_bios = lambda conn, dev: None

portable_cfg = InstallConfig(
    mode="direct", partition_mode="auto", username="alice",
    password="secret", hostname="alice-pc", timezone="UTC",
    filesystem="ext4", swap_gib=0,
)
portable_events = []
ok = di.write_linux_portable(
    "/tmp/fake.iso", "/dev/sdz", portable_cfg,
    PartitionScheme.GPT, TargetSystem.UEFI, "LUBUS",
    lambda d, t: portable_events.append((d, t)),
    lambda: False,
    iso_root=iso_dir,
    release_stale=False,
)
check("linux portable deployment returns True", ok is True)
check("two partitions created", len(parts) == 2, str(parts))
check("ISO payload copied to root",
      os.path.isfile(os.path.join(root_dir, "casper", "vmlinuz")))
check("EFI boot files copied to ESP",
      os.path.isfile(os.path.join(esp_dir, "EFI", "BOOT", "BOOTX64.EFI")))
check("cloud-init seed written",
      os.path.isfile(os.path.join(root_dir, "var", "lib", "cloud",
                                  "seed", "nocloud-net", "user-data")))
check("portable progress completes at 100%",
      portable_events and portable_events[-1][0] == portable_events[-1][1])
check("portable progress never reaches 100% early",
      all(d < t for d, t in portable_events[:-1]))

for d in (iso_dir, esp_dir, root_dir):
    shutil.rmtree(d, ignore_errors=True)

print("\n=== MULTI-DRIVE CONCURRENT WINDOW FLOW ===")
import threading
import time
import gi
from lubus.core.devices import Drive, BusType
from lubus.core.images import ImageInfo, FileSystem
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
Gtk.init(); Adw.init()
app = Adw.Application(application_id="org.lubus.v5multi"); app.register(None)
import lubus.window as window_mod
from lubus.window import LubusWindow

win = LubusWindow(app)
d1 = Drive(device="/dev/sdb", size=32 * GiB, model="Ultra Fit", vendor="SanDisk",
           product="Ultra Fit", serial="S1", wwn="W1", bus_type=BusType.USB,
           removable=True, ejectable=True, media_available=True, media_removable=True)
d2 = Drive(device="/dev/sdc", size=64 * GiB, model="Extreme", vendor="SanDisk",
           product="Extreme", serial="S2", wwn="W2", bus_type=BusType.USB,
           removable=True, ejectable=True, media_available=True, media_removable=True)

multi_info = ImageInfo(
    path="/tmp/x.iso", size=4 * GiB, image_type=ImageType.LINUX_ISHYBRID,
    label="X", suggested_scheme=PartitionScheme.GPT,
    suggested_target=TargetSystem.UEFI, suggested_fs=FileSystem.FAT32,
    windows_editions=[], is_efi_bootable=True, is_bios_bootable=True,
)
win._iso_path = "/tmp/x.iso"
win._image_info = multi_info
win._selected_drive = d1
win._selected_drives = [d1, d2]
win._settings.update({"write_mode": 0, "verify": False})

barrier = threading.Barrier(2, timeout=8)
called = []

def fake_dd(iso, device, progress, stopped, verify=False, release_stale=True,
            verify_progress=None, status_cb=None):
    called.append(device)
    barrier.wait()
    for i in range(1, 11):
        progress(i * 100, 1000)
        time.sleep(0.002)
    return True

real_dd = window_mod.copy_image_dd
real_identity = window_mod.get_drive_identity
real_release = window_mod.release_stale_iso_mounts
window_mod.copy_image_dd = fake_dd
window_mod.get_drive_identity = lambda _dev: ("id", "serial", "wwn", 1)
window_mod.release_stale_iso_mounts = lambda _p=None: 0
try:
    ok = win._do_flash()
finally:
    window_mod.copy_image_dd = real_dd
    window_mod.get_drive_identity = real_identity
    window_mod.release_stale_iso_mounts = real_release

check("multi-drive flash succeeds", ok is True)
check("both drives were written concurrently",
      set(called) == {"/dev/sdb", "/dev/sdc"}, str(called))

print("\n=== DIRECT INSTALL DISPATCH GUARD ===")
import lubus.core.direct_install as di
from lubus.core.devices import Drive, BusType
from lubus.core.images import ImageInfo, FileSystem
from lubus.core.writer import WriteError

fake_info = ImageInfo(
    path="/tmp/x.iso", size=4 * GiB, image_type=ImageType.UNKNOWN,
    label="X", suggested_scheme=PartitionScheme.GPT,
    suggested_target=TargetSystem.UEFI, suggested_fs=FileSystem.FAT32,
    windows_editions=[], is_efi_bootable=True, is_bios_bootable=False,
)
di.get_device_size = lambda _d: 64 * GiB
try:
    di.install_to_drive("/tmp/x.iso", "/dev/sdz", fake_info, valid,
                        lambda *_a: None, lambda: False)
    check("unsupported image type rejected", False, "(no exception)")
except WriteError as e:
    check("unsupported image type rejected", "not supported" in str(e).lower(), str(e))

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("RESULT: ALL 5.0 FEATURE TESTS PASSED")
