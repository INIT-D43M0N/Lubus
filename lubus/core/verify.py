"""Verification logic for Lubus."""
import os
import mmap
import fcntl
from typing import Callable

from ..utils.logging import get_logger

log = get_logger(__name__)

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB

ProgressCallback = Callable[[int, int], None]  # (done, total)
StoppedCallback = Callable[[], bool]            # () -> should_stop


class VerifyMismatchError(Exception):
    """Verification failed - data mismatch."""
    pass


class VerifyError(Exception):
    """Verification error."""
    pass


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
    try:
        fd = os.open(device, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as e:
        raise VerifyError(f"Cannot open device: {e}")

    try:
        # Get device size
        device_size = os.lseek(fd, 0, os.SEEK_END)
        os.lseek(fd, offset, os.SEEK_SET)

        # Get image size
        image_size = os.path.getsize(image_path)
        if length is None:
            length = image_size

        if offset + length > device_size:
            raise VerifyError(f"Verification range exceeds device size")

        if length > image_size:
            raise VerifyError(f"Verification range exceeds image size")

        # Try to use O_DIRECT to bypass page cache
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_DIRECT)
            use_direct = True
        except OSError:
            use_direct = False
            # Advise kernel to not cache
            os.posix_fadvise(fd, offset, length, os.POSIX_FADV_DONTNEED)

        # Allocate aligned buffer for O_DIRECT
        if use_direct:
            buf = mmap.mmap(-1, CHUNK_SIZE)
        else:
            buf = bytearray(CHUNK_SIZE)

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

                # Read from device
                if use_direct:
                    # O_DIRECT requires aligned reads
                    read_len = len(chunk)
                    # Round up to sector size for O_DIRECT
                    sector_size = 512
                    aligned_len = ((read_len + sector_size - 1) // sector_size) * sector_size
                    if aligned_len > CHUNK_SIZE:
                        aligned_len = CHUNK_SIZE

                    got = 0
                    while got < aligned_len:
                        n = os.read(fd, aligned_len - got)
                        if n == 0:
                            break
                        buf[got:got+len(n)] = n
                        got += len(n)

                    # Compare only the actual data length
                    if got < read_len or buf[:read_len] != chunk:
                        raise VerifyMismatchError(
                            f"Mismatch at offset {offset + done}"
                        )
                else:
                    # Regular read
                    got = 0
                    while got < len(chunk):
                        n = os.read(fd, len(chunk) - got)
                        if n == 0:
                            break
                        if buf[got:got+len(n)] != n:
                            raise VerifyMismatchError(
                                f"Mismatch at offset {offset + done + got}"
                            )
                        got += len(n)

                    if got < len(chunk) or buf[:len(chunk)] != chunk:
                        raise VerifyMismatchError(
                            f"Mismatch at offset {offset + done}"
                        )

                done += len(chunk)
                progress(done, total)

        return True

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

    try:
        fd = os.open(device, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as e:
        raise VerifyError(f"Cannot open device: {e}")

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