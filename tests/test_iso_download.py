"""Tests for the Linux ISO catalog and segmented downloader."""
import hashlib
import http.server
import os
import re
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.argv = ["yaku"]

failures = []


def check(name, cond, detail=""):
    print(("  PASS: " if cond else "  FAIL: ") + name +
          (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


print("=== ISO CATALOG ===")
import yaku.core.iso_catalog as catalog_mod
from yaku.core.iso_catalog import IsoCatalog, CatalogError

cat = IsoCatalog.load()
check("catalog loads local fallback", len(cat.distros) >= 15, str(len(cat.distros)))
check("Ubuntu and flavors present",
      any(d["id"] == "ubuntu" for d in cat.distros)
      and any(d["id"] == "kubuntu" for d in cat.distros)
      and any(d["id"] == "xubuntu" for d in cat.distros)
      and any(d["id"] == "lubuntu" for d in cat.distros)
      and any(d["id"] == "ubuntustudio" for d in cat.distros))
check("Debian stable/testing/sid branches present",
      any(len([b for b in d["branches"]
               if b["id"] in ("stable", "testing", "sid")]) == 3
          for d in cat.distros if d["id"] == "debian"))
check("other requested distros present",
      all(any(d["id"] == i for d in cat.distros)
          for i in ("linuxmint", "fedora", "arch", "manjaro", "popos",
                    "kali", "tails", "alpine", "zorin", "mxlinux")))
check("search finds Ubuntu flavors", len(cat.search("kubuntu")) >= 1)
check("search finds category tags", len(cat.search("lightweight")) >= 1)

# Resolver with a fake checksum mirror.
debian = next(d for d in cat.distros if d["id"] == "debian")
branch = next(b for b in debian["branches"] if b["id"] == "stable")
edition = next(e for e in branch["editions"] if e["id"] == "netinst")
fake_hash = "a" * 64
real_fetch = catalog_mod.fetch_text


def fake_fetch(url, timeout=25, max_bytes=8 * 1024**2):
    if "SHA256SUMS" in url:
        return f"{fake_hash}  debian-13.1.0-amd64-netinst.iso\n"
    return ""


catalog_mod.fetch_text = fake_fetch
try:
    resolved = cat.resolve(debian, branch, edition)
finally:
    catalog_mod.fetch_text = real_fetch
check("mirror resolver constructs direct URL",
      resolved.filename == "debian-13.1.0-amd64-netinst.iso"
      and resolved.url.endswith(resolved.filename))
check("mirror resolver returns official SHA-256",
      resolved.sha256 == fake_hash)

print("\n=== SEGMENTED DOWNLOAD / HASH VERIFICATION ===")
data = os.urandom(2 * 1024 * 1024)
sha = hashlib.sha256(data).hexdigest()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d+)-(\d+)$", rng)
            start, end = int(m.group(1)), min(int(m.group(2)), len(data) - 1)
            body = data[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{len(data)}")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_port}/test.iso"

from yaku.core.iso_downloader import IsoDownload

dest = tempfile.mkdtemp(prefix="yaku_dl_")
done = {}
event = threading.Event()


def on_done(path):
    done["path"] = path
    event.set()


def on_error(message):
    done["error"] = message
    event.set()


download = IsoDownload(
    url, dest, "test.iso", sha, segments=4,
    on_done=on_done, on_error=on_error,
)
download.start()
completed = event.wait(30)
server.shutdown()

check("segmented download completes", completed and "path" in done, str(done))
final = os.path.join(dest, "test.iso")
check("downloaded file exists", os.path.isfile(final))
if os.path.isfile(final):
    check("downloaded bytes match",
          hashlib.sha256(open(final, "rb").read()).hexdigest() == sha)

print("\n=== BAD CHECKSUM REJECTION ===")
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_port}/test.iso"
dest2 = tempfile.mkdtemp(prefix="yaku_dl_bad_")
bad = {}
event2 = threading.Event()
download = IsoDownload(
    url, dest2, "bad.iso", "0" * 64,
    on_done=lambda p: (bad.__setitem__("path", p), event2.set()),
    on_error=lambda e: (bad.__setitem__("error", e), event2.set()),
)
download.start()
event2.wait(30)
server.shutdown()
check("bad checksum is rejected", "error" in bad, str(bad))
check("bad file is not published", not os.path.exists(os.path.join(dest2, "bad.iso")))

import shutil
for d in (dest, dest2):
    shutil.rmtree(d, ignore_errors=True)

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("RESULT: ISO DOWNLOAD TESTS PASSED")
