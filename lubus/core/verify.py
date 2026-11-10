"""Verification logic for Lubus."""
import os
import mmap
import fcntl
from typing import Callable, Tuple

from ..utils.logging import get_logger

log = get_logger(__name__)

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB
SECTOR_SIZE = 512

ProgressCallback = Callable[[int, int], None]  # (done, total)
StoppedCallback = Callable[[], bool]            # () -> should_stop


class VerifyMismatchError(Exception):
    """Verification failed - data mismatch."""
    pass


class VerifyError(Exception):
    """Verification error."""
    pass


def _open_device_readonly(device: str) -> int:
    """Open a block device for reading, without requiring root.

    The node itself usually belongs to root (0660 root:disk), so a plain
    open fails with "Permission denied". UDisks2 performs the privileged
    part through polkit, exactly like the write path does, so the device can
    be read back for verification.
    """
    try:
        from ..udisks.client import bus, open_device_exclusive
        conn = bus()
        # No O_EXCL here: the write path may still hold the device open.
        return open_device_exclusive(conn, device, "r", exclusive=False)
    except Exception as exc:  # noqa: BLE001 - fall back to a direct open
        log.debug("Could not open %s through UDisks2: %s", device, exc)

    try:
        return os.open(device, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as e:
        raise VerifyError(f"Cannot open device: {e}") from e


def _set_direct(fd: int, enabled: bool) -> bool:
    """Enable or disable O_DIRECT on an open descriptor."""
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if enabled:
            flags |= os.O_DIRECT
        else:
            flags &= ~os.O_DIRECT
        fcntl.fcntl(fd, fcntl.F_SETFL, flags)
        return enabled
    except OSError:
        return False


def _read_at(fd: int, size: int, position: int, buf, use_direct: bool) -> Tuple[bytes, bool]:
    """Read up to ``size`` bytes at ``position``.

    With O_DIRECT the transfer has to be sector aligned and the buffer
    page aligned, so the read is rounded up to the sector size and the
    result is truncated afterwards. When the kernel refuses the transfer
    (for instance on a device with 4 KiB sectors) the read is retried
    buffered, so a verification never fails because of alignment rules.

    Returns (data, use_direct) - the flag can switch to False.
    """
    if not use_direct:
        return os.pread(fd, size, position), False

    aligned = ((size + SECTOR_SIZE - 1) // SECTOR_SIZE) * SECTOR_SIZE
    if aligned > CHUNK_SIZE:
        aligned = CHUNK_SIZE
    view = memoryview(buf)[:aligned]

    try:
        got = os.preadv(fd, [view], position)
    except OSError as exc:
        log.debug("O_DIRECT read failed (%s); retrying buffered", exc)
        _set_direct(fd, False)
        return os.pread(fd, size, position), False

    return bytes(view[:min(got, size)]), True


def verify_fd_matches_image(
    fd: int,
    image_path: str,
    progress: ProgressCallback,
    stopped: StoppedCallback,
    offset: int = 0,
    length: int = None,
) -> bool:
    """Verify that an already open device descriptor matches the image.

    Used by the dd writer, which still holds the descriptor it wrote with,
    so the device does not have to be opened (and authorized) a second time.
    """
    device_size = os.lseek(fd, 0, os.SEEK_END)

    image_size = os.path.getsize(image_path)
    if length is None:
        length = image_size

    if offset + length > device_size:
        raise VerifyError("Verification range exceeds device size")

    if length > image_size:
        raise VerifyError("Verification range exceeds image size")

    # Bypass the page cache when the device allows it.
    use_direct = _set_direct(fd, True)
    if not use_direct:
        os.posix_fadvise(fd, offset, length, os.POSIX_FADV_DONTNEED)

    # Page aligned buffer, required by O_DIRECT.
    buf = mmap.mmap(-1, CHUNK_SIZE)

    total = length
    done = 0

    with open(image_path, "rb") as img:
        img.seek(offset)
        while done < length:
            if stopped():
                return False

            to_read = min(CHUNK_SIZE, length - done)
            chunk = img.read(to_read)
            if not chunk:
                break

            data, use_direct = _read_at(fd, len(chunk), offset + done, buf, use_direct)

            if len(data) < len(chunk) or data != chunk:
                bad = 0
                for index, (a, b) in enumerate(zip(data, chunk, strict=False)):
                    if a != b:
                        bad = index
                        break
                else:
                    bad = min(len(data), len(chunk))
                raise VerifyMismatchError(
                    f"Mismatch at offset {offset + done + bad}"
                )

            done += len(chunk)
            progress(done, total)

    return True


def verify_device_matches_image(
    device: str,
    image_path: str,
    progress: ProgressCallback,
    stopped: StoppedCallback,
    offset: int = 0,
    length: int = None,
) -> bool:
    """Verify that device matches image (byte-for-byte).

    Args:
        device: Device path (e.g., /dev/sdb)
        image_path: Path to image file
        progress: Progress callback (done, total)
        stopped: Stop check callback
        offset: Start offset on device (bytes)
        length: Number of bytes to verify (None = entire image)

    Returns:
        True if verified, False if stopped.

    Raises:
        VerifyMismatchError: Data mismatch
        VerifyError: Other error
    """
    fd = _open_device_readonly(device)
    try:
        return verify_fd_matches_image(
            fd, image_path, progress, stopped, offset=offset, length=length
        )
    finally:
        os.close(fd)


def verify_partition_matches_image(
    partition: str,
    image_path: str,
    progress: ProgressCallback,
    stopped: StoppedCallback,
) -> bool:
    """Verify a partition matches an image (for filesystem-level verify)."""
    # For filesystem-level verification, we'd need to mount both
    # and compare file trees. This is more complex.
    # For now, just do block-level verify of the partition.
    return verify_device_matches_image(partition, image_path, progress, stopped)


def calculate_checksum(device: str, algorithm: str = "sha256", progress=None, stopped=None) -> str:
    """Calculate checksum of a device or partition."""
    import hashlib

    fd = _open_device_readonly(device)

    try:
        hasher = hashlib.new(algorithm)
        done = 0
        total = os.lseek(fd, 0, os.SEEK_END)
        os.lseek(fd, 0, os.SEEK_SET)

        while True:
            if stopped and stopped():
                return None
            chunk = os.read(fd, CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)

        return hasher.hexdigest()
    finally:
        os.close(fd)


def verify_checksum(device: str, expected: str, algorithm: str = "sha256",
                    progress=None, stopped=None) -> bool:
    """Verify device checksum matches expected."""
    actual = calculate_checksum(device, algorithm, progress, stopped)
    return actual is not None and actual.lower() == expected.lower()
