"""Segmented HTTP ISO downloader with pause/resume/cancel and SHA-256 verification."""
import hashlib
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request

from lubus.utils.logging import get_logger

log = get_logger(__name__)

USER_AGENT = "Lubus/5.0 (+https://github.com/yourusername/lubus)"
CHUNK_SIZE = 256 * 1024


class DownloadError(Exception):
    pass


class _RangeNotSupported(Exception):
    pass


class IsoDownload:
    """Download one ISO/image with HTTP range segments.

    The object stays alive while paused so in-session resume continues from
    each segment's current offset.  Verification is mandatory: no file is
    moved into place without a matching SHA-256.
    """

    def __init__(
        self,
        url: str,
        dest_dir: str,
        filename: str,
        sha256: str,
        checksum_url: str = "",
        segments: int = 4,
        on_progress=None,
        on_status=None,
        on_done=None,
        on_error=None,
        on_cancelled=None,
    ):
        self.url = url
        self.dest_dir = dest_dir
        self.filename = os.path.basename(filename) or "image.iso"
        self.sha256 = (sha256 or "").lower()
        self.checksum_url = checksum_url
        self.segment_count = max(1, min(8, int(segments)))
        self.on_progress = on_progress or (lambda *_a: None)
        self.on_status = on_status or (lambda *_a: None)
        self.on_done = on_done or (lambda *_a: None)
        self.on_error = on_error or (lambda *_a: None)
        self.on_cancelled = on_cancelled or (lambda *_a: None)

        self.part_path = os.path.join(dest_dir, self.filename + ".part")
        self.final_path = os.path.join(dest_dir, self.filename)

        self._total = 0
        self._downloaded = 0
        self._segments = []          # [start, end, position]
        self._fd = None
        self._threads = []
        self._lock = threading.Lock()
        self._paused = threading.Event()
        self._cancelled = threading.Event()
        self._resume_event = threading.Event()
        self._running = False
        self._error = None
        self._range_error = False
        self._start_time = 0.0
        self._last_emit = 0.0
        self._last_bytes = 0
        self._manager_thread = None

    # ---------------------------------------------------------------- public

    @property
    def running(self) -> bool:
        return self._running

    def start(self):
        if self._manager_thread and self._manager_thread.is_alive():
            return
        os.makedirs(self.dest_dir, exist_ok=True)
        if not self.sha256:
            self.on_error("No SHA-256 checksum is available; download refused.")
            return
        self._running = True
        self._start_time = time.monotonic()
        self._manager_thread = threading.Thread(
            target=self._manager, daemon=True, name="iso-download-manager"
        )
        self._manager_thread.start()

    def pause(self):
        if not self._running or self._paused.is_set():
            return
        self._paused.set()
        for thread in list(self._threads):
            thread.join(timeout=2.0)
        self.on_status("Paused")

    def resume(self):
        if not self._running or not self._paused.is_set():
            return
        self._paused.clear()
        self._resume_event.set()
        self.on_status("Downloading...")

    def cancel(self):
        if not self._running:
            return
        self._cancelled.set()
        self._paused.clear()
        self._resume_event.set()
        self.on_status("Cancelling...")

    # ------------------------------------------------------------- internals

    def _manager(self):
        try:
            self._prepare()
            while True:
                if self._cancelled.is_set():
                    self._finish_cancel()
                    return
                if self._paused.is_set():
                    self.on_status("Paused")
                    self._resume_event.wait(0.25)
                    self._resume_event.clear()
                    continue
                if self._all_done():
                    break
                self._start_workers()
                self._join_workers()
                if self._cancelled.is_set():
                    self._finish_cancel()
                    return
                if self._paused.is_set():
                    continue
                if self._range_error:
                    self._retry_single_stream()
                    continue
                if self._error:
                    raise DownloadError(self._error)
                if not self._all_done():
                    # A worker stopped early without a pause/cancel/error;
                    # retry the remaining ranges once.
                    time.sleep(0.2)
                    if not self._all_done():
                        raise DownloadError("Download stopped before completion")

            os.fsync(self._fd)
            self._close_fd()
            self._verify_and_finish()
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            log.exception("ISO download failed")
            self._close_fd()
            self._cleanup_part()
            self._running = False
            self.on_error(str(exc))

    def _prepare(self):
        self.on_status("Connecting...")
        total, ranges = 0, False
        final_url = self.url
        try:
            req = urllib.request.Request(
                self.url, method="HEAD",
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                final_url = resp.geturl()
                total = int(resp.headers.get("Content-Length", "0") or 0)
                ranges = "bytes" in (resp.headers.get("Accept-Ranges", "").lower())
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError):
            # Some mirrors reject HEAD; probe with a 1-byte range request.
            req = urllib.request.Request(
                self.url, headers={
                    "User-Agent": USER_AGENT, "Range": "bytes=0-0",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                final_url = resp.geturl()
                content_range = resp.headers.get("Content-Range", "")
                m = re.search(r"/(\d+)$", content_range)
                if m:
                    total = int(m.group(1))
                    ranges = True
                else:
                    total = int(resp.headers.get("Content-Length", "0") or 0)

        self.url = final_url
        self._total = total
        if total <= 0:
            raise DownloadError("The server did not report a download size")

        self._segments = []
        if ranges and total > 0:
            count = min(self.segment_count, max(1, total // (2 * 1024**2)))
            step = total // count
            for i in range(count):
                start = i * step
                end = total - 1 if i == count - 1 else (i + 1) * step - 1
                self._segments.append([start, end, start])
        else:
            self._segments.append([0, total - 1, 0])

        self._fd = os.open(
            self.part_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644
        )
        try:
            os.ftruncate(self._fd, total)
        except OSError:
            pass
        self._downloaded = 0
        self._emit_progress(force=True)
        self.on_status("Downloading...")

    def _start_workers(self):
        self._threads = []
        for segment in self._segments:
            if segment[2] > segment[1]:
                continue
            t = threading.Thread(
                target=self._worker, args=(segment,), daemon=True,
                name="iso-download-worker",
            )
            self._threads.append(t)
            t.start()

    def _join_workers(self):
        for t in self._threads:
            t.join(timeout=1.0)
        # A worker may be blocked in a socket read; give it a little more
        # time while still allowing pause/cancel to respond.
        for t in self._threads:
            while t.is_alive() and not self._cancelled.is_set():
                if self._paused.is_set() or self._cancelled.is_set():
                    break
                t.join(timeout=0.2)
        if self._paused.is_set() or self._cancelled.is_set():
            for t in self._threads:
                t.join(timeout=2.0)

    def _worker(self, segment):
        start, end, pos = segment
        attempts = 0
        while pos <= end:
            if self._cancelled.is_set() or self._paused.is_set():
                segment[2] = pos
                return
            try:
                headers = {"User-Agent": USER_AGENT}
                if start != 0 or end != self._total - 1 or len(self._segments) > 1:
                    headers["Range"] = f"bytes={pos}-{end}"
                req = urllib.request.Request(self.url, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    if "Range" in headers and resp.status == 200 and pos > 0:
                        raise _RangeNotSupported()
                    while pos <= end:
                        if self._cancelled.is_set() or self._paused.is_set():
                            segment[2] = pos
                            return
                        data = resp.read(min(CHUNK_SIZE, end - pos + 1))
                        if not data:
                            break
                        os.pwrite(self._fd, data, pos)
                        pos += len(data)
                        segment[2] = pos
                        with self._lock:
                            self._downloaded += len(data)
                        self._emit_progress()
                attempts = 0
            except _RangeNotSupported:
                self._range_error = True
                segment[2] = pos
                return
            except (urllib.error.URLError, OSError) as exc:
                attempts += 1
                if attempts >= 4:
                    self._error = f"Network error: {exc}"
                    segment[2] = pos
                    return
                time.sleep(min(2.0, 0.3 * attempts))
        segment[2] = min(pos, end + 1)

    def _retry_single_stream(self):
        if len(self._segments) == 1:
            raise DownloadError("The mirror does not support range requests")
        self.on_status("Restarting download (mirror has no range support)...")
        self._range_error = False
        self._segments = [[0, self._total - 1, 0]]
        self._downloaded = 0
        try:
            os.ftruncate(self._fd, self._total)
        except OSError:
            pass
        self._emit_progress(force=True)

    def _all_done(self):
        return bool(self._segments) and all(
            s[2] > s[1] for s in self._segments
        )

    def _emit_progress(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_emit < 0.1:
            return
        with self._lock:
            done = self._downloaded
        total = self._total
        elapsed = max(0.001, now - self._start_time)
        speed = done / elapsed
        eta = int((total - done) / speed) if speed > 0 and total > done else 0
        self._last_emit = now
        self.on_progress(done, total, speed, eta)

    def _verify_and_finish(self):
        self.on_status("Verifying SHA-256...")
        digest = hashlib.sha256()
        with open(self.part_path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual.lower() != self.sha256:
            self._cleanup_part()
            raise DownloadError(
                "SHA-256 verification failed. The download was discarded."
            )
        os.replace(self.part_path, self.final_path)
        self._running = False
        self.on_status("Verified")
        self.on_done(self.final_path)

    def _finish_cancel(self):
        self._close_fd()
        self._cleanup_part()
        self._running = False
        self.on_status("Cancelled")
        self.on_cancelled()

    def _close_fd(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def _cleanup_part(self):
        try:
            os.unlink(self.part_path)
        except OSError:
            pass
