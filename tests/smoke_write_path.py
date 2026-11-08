"""Smoke test for the Windows write path (run: python3 tests/smoke_write_path.py)

Verifies the ENOSPC fix: ESP sizing, FAT32 guard, and GPT write flow.

Reproduces the user's failure: Windows 11 ISO (8.1 GiB) on a 32 GiB drive
where ~2.5 GiB of boot files were copied into a fixed 512 MiB ESP.
"""
import sys, os, tempfile, shutil, errno, builtins
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.argv = ['lubus']
import gi
gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1')
import warnings; warnings.filterwarnings('ignore')

from lubus.core.writer import (
    _measure_iso_tree, _compute_esp_size, _copy_file_with_progress,
    _copy_tree_with_progress, _ProgressMeter, WriteError, WIN_SKIP_PATHS,
    FAT32_MAX_FILE,
)

failures = []
def check(name, cond, detail=""):
    if cond:
        print(f"  PASS: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        failures.append(name)

# ---------------------------------------------------------------------------
print("=== TEST 1: Measure a Windows-11-like ISO tree (sparse files) ===")
iso = tempfile.mkdtemp(prefix='win11_iso_')
def sparse(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.truncate(size)

GiB = 1024**3
sparse(os.path.join(iso, 'sources', 'install.wim'), int(5.6 * GiB))
sparse(os.path.join(iso, 'sources', 'boot.wim'), int(0.5 * GiB))
sparse(os.path.join(iso, 'efi', 'boot', 'bootx64.efi'), 1 * 1024**2)
sparse(os.path.join(iso, 'boot', 'memtest.efi'), 2 * 1024**2)
sparse(os.path.join(iso, 'setup.exe'), 10 * 1024**2)
sparse(os.path.join(iso, 'sources', 'license.txt'), int(2.0 * GiB))  # fat32 part

fat32_bytes, wim_bytes, oversized = _measure_iso_tree(iso, WIN_SKIP_PATHS)
print(f"  fat32_bytes={fat32_bytes/GiB:.2f} GiB, wim_bytes={wim_bytes/GiB:.2f} GiB")
check("wim excluded from FAT32 set", wim_bytes == int(5.6 * GiB))
check("rest measured for FAT32", fat32_bytes > int(1.9 * GiB))
check("no oversized files", not oversized)

esp_size = _compute_esp_size(fat32_bytes, wim_bytes, 32 * GiB)
print(f"  computed ESP size = {esp_size/GiB:.2f} GiB (old code: fixed 0.50 GiB)")
check("ESP sized to fit boot files", esp_size >= fat32_bytes + 64 * 1024**2)
check("ESP > 2 GiB (was 512 MiB)", esp_size > 2 * GiB)
check("leaves room for wim on 32G drive", esp_size + wim_bytes + 256 * 1024**2 <= 32 * GiB)

# ---------------------------------------------------------------------------
print("\n=== TEST 2: Too-small drive raises WriteError (not ENOSPC) ===")
try:
    _compute_esp_size(fat32_bytes, wim_bytes, 8 * GiB)
    check("small drive rejected", False, "(no exception)")
except WriteError as e:
    check("small drive rejected with clear message", "too small" in str(e).lower(), str(e))
except Exception as e:
    check("small drive rejected", False, f"wrong exception {type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
print("\n=== TEST 3: File >= 4 GiB destined for FAT32 is rejected ===")
big = tempfile.mkdtemp(prefix='big_iso_')
sparse(os.path.join(big, 'sources', 'install.wim'), int(5.6 * GiB))
sparse(os.path.join(big, 'sources', 'huge.dat'), FAT32_MAX_FILE + 1)
_, _, oversized = _measure_iso_tree(big, WIN_SKIP_PATHS)
check("oversized file detected", len(oversized) == 1 and oversized[0][0].endswith('huge.dat'))

# ---------------------------------------------------------------------------
print("\n=== TEST 4: ENOSPC during copy becomes a clear WriteError ===")
src_dir = tempfile.mkdtemp()
dst_dir = tempfile.mkdtemp()
src = os.path.join(src_dir, 'install.wim')
with open(src, 'wb') as f:
    f.write(b'x' * 1024)

real_open = builtins.open
class ENOSPCOnWrite:
    def __init__(self, fh): self._fh = fh
    def write(self, data): raise OSError(errno.ENOSPC, 'No space left on device')
    def __enter__(self): return self
    def __exit__(self, *a): self._fh.close(); return False
    def __getattr__(self, k): return getattr(self._fh, k)

def fake_open(path, mode='r', *a, **kw):
    fh = real_open(path, mode, *a, **kw)
    if 'w' in mode and str(path).startswith(dst_dir):
        return ENOSPCOnWrite(fh)
    return fh

builtins.open = fake_open
try:
    _copy_file_with_progress(src, os.path.join(dst_dir, 'install.wim'),
                             lambda n: None, lambda: False)
    check("ENOSPC converted to WriteError", False, "(no exception)")
except WriteError as e:
    check("ENOSPC converted to WriteError", 'install.wim' in str(e) and 'space' in str(e).lower(), str(e))
except OSError as e:
    check("ENOSPC converted to WriteError", False, f"raw OSError leaked: {e}")
finally:
    builtins.open = real_open

# ---------------------------------------------------------------------------
print("\n=== TEST 4b: progress meter reserves 100% for finish ===")
meter_events = []
meter = _ProgressMeter(100, lambda d, t: meter_events.append((d, t)))
meter.add(60)
meter.add(40)
check("meter caps intermediate progress below total",
      all(d < t for d, t in meter_events), str(meter_events))
check("meter reports 99/100 at the byte boundary",
      meter_events[-1] == (99, 100), str(meter_events[-1]))
meter.finish()
check("meter reports exact 100% only on finish",
      meter_events[-1] == (100, 100), str(meter_events[-1]))

# ---------------------------------------------------------------------------
print("\n=== TEST 5: Full GPT write flow with mocked D-Bus (regression) ===")
import lubus.core.writer as writer
import lubus.udisks as udisks_mod

# Small fake ISO so copying is fast; also tests the sources/ dir creation
iso2 = tempfile.mkdtemp(prefix='fakeiso_')
os.makedirs(os.path.join(iso2, 'sources'))
os.makedirs(os.path.join(iso2, 'efi', 'boot'))
with open(os.path.join(iso2, 'sources', 'install.wim'), 'wb') as f:
    f.write(b'W' * 4096)
with open(os.path.join(iso2, 'sources', 'boot.wim'), 'wb') as f:
    f.write(b'B' * 8192)
with open(os.path.join(iso2, 'efi', 'boot', 'bootx64.efi'), 'wb') as f:
    f.write(b'E' * 1024)

esp_mnt = tempfile.mkdtemp(prefix='esp_')
win_mnt = tempfile.mkdtemp(prefix='win_')
OBJ = "/org/freedesktop/UDisks2/block_devices/sdd"
created_sizes = []

class FakeISO:
    def __enter__(self): return iso2
    def __exit__(self, *a): return False

udisks_mod.bus = lambda: object()
udisks_mod.resolve = lambda c, d: OBJ
udisks_mod.unmount_all = lambda c, p: None
udisks_mod.unmount_filesystem = lambda c, p: None
udisks_mod.mount_filesystem = lambda c, p, readonly=False: (esp_mnt if p.endswith('1') else win_mnt)
udisks_mod.wipe_partition_table = lambda c, d, s: None
def mk_part(conn, dev, offset, size, ptype, name, fs, label, scheme):
    created_sizes.append(size)
    return f"{OBJ}/{len(created_sizes)}"
udisks_mod.create_partition = mk_part
udisks_mod.set_partition_active = lambda c, p: None
udisks_mod.can_format_fstype = lambda f: True
udisks_mod.mount_iso_readonly = lambda p: FakeISO()

for attr in ('bus', 'resolve', 'unmount_all', 'unmount_filesystem',
             'mount_filesystem', 'create_partition', 'set_partition_active',
             'mount_iso_readonly'):
    setattr(writer, attr, getattr(udisks_mod, attr))
writer.wipe_partition_table = lambda c, d, s: None
writer.get_device_size = lambda d: 32 * GiB
writer.release_stale_iso_mounts = lambda p=None: 0
writer._apply_windows_icon = lambda m: False
writer.install_grub_bios = lambda c, d: None

from lubus.core.images import PartitionScheme as PS
progress = []
progress_after_sync = []
sync_calls = []
real_sync = os.sync

def tracked_sync():
    sync_calls.append(True)
    real_sync()

def record_progress(done, total):
    progress.append((done, total))
    progress_after_sync.append(bool(sync_calls))

writer.os.sync = tracked_sync
try:
    ok = writer.write_windows_installer(
        '/tmp/fake.iso', '/dev/sdd', PS.GPT, 'WINUSB',
        record_progress, lambda: False,
        windows_options={
            "skip_oobe": True,
            "bypass_win11": True,
            "remove_defender": False,
            "username": "alice",
            "password": "secret",
        },
    )
finally:
    writer.os.sync = real_sync

check("GPT write returns True", ok is True)
check("2 partitions created", len(created_sizes) == 2, f"got {len(created_sizes)}")
check("ESP >= 512 MiB", created_sizes[0] >= 512 * 1024**2, f"got {created_sizes[0]}")
check("NTFS gets the rest (~31 GiB)", created_sizes[1] > 25 * GiB, f"got {created_sizes[1]/GiB:.1f} GiB")

# The install.wim must land in win_mount/sources/ (dir did not exist before!)
wim_dst = os.path.join(win_mnt, 'sources', 'install.wim')
check("install.wim copied to NTFS partition", os.path.exists(wim_dst))
check("boot.wim copied to ESP", os.path.exists(os.path.join(esp_mnt, 'sources', 'boot.wim')))
check("boot files copied to ESP", os.path.exists(os.path.join(esp_mnt, 'efi', 'boot', 'bootx64.efi')))
check("install.wim NOT copied to ESP", not os.path.exists(os.path.join(esp_mnt, 'sources', 'install.wim')))
check("autounattend.xml written to installer root",
      os.path.isfile(os.path.join(esp_mnt, 'autounattend.xml')))
check("autounattend.xml written to NTFS root",
      os.path.isfile(os.path.join(win_mnt, 'autounattend.xml')))
_at = open(os.path.join(esp_mnt, 'autounattend.xml')).read()
check("installer autounattend carries account/OOBE",
      "<Name>alice</Name>" in _at and "BypassTPMCheck" in _at
      and "<HideOnlineAccountScreens>true</HideOnlineAccountScreens>" in _at)
check("progress reported", len(progress) > 0)

# 4096-byte install.wim + 8192 boot.wim + 1024 EFI file
expected_total = 4096 + 8192 + 1024
check("progress total spans boot files + WIM",
      progress[-1] == (expected_total, expected_total),
      str(progress[-1]))
check("no 100% event before the final flush",
      all(done < total for done, total in progress[:-1]), str(progress[-3:]))
check("final 100% event is emitted after os.sync()",
      progress_after_sync[-1] is True and bool(sync_calls),
      f"sync={sync_calls} tail={progress_after_sync[-3:]}")

# ---------------------------------------------------------------------------
print("\n=== TEST 6: existing unit tests still pass ===")
import importlib.util, types, contextlib
sys.modules.setdefault('pytest', types.SimpleNamespace(main=lambda *a, **k: None))
spec = importlib.util.spec_from_file_location(
    'test_core', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test_core.py'))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
passed = failed = 0
for cname in dir(mod):
    cls = getattr(mod, cname)
    if not (isinstance(cls, type) and cname.startswith('Test')):
        continue
    inst = cls()
    for mname in dir(inst):
        if not mname.startswith('test_'):
            continue
        try:
            getattr(inst, mname)()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  UNIT-FAIL {cname}.{mname}: {type(e).__name__}: {e}")
print(f"  unit tests: {passed} passed, {failed} failed")
check("unit test suite green", failed == 0)

# cleanup
for d in (iso, big, src_dir, dst_dir, iso2, esp_mnt, win_mnt):
    shutil.rmtree(d, ignore_errors=True)

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("RESULT: ALL TESTS PASSED")
