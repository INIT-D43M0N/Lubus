"""Regression tests for the write verification.

Covers the flash failure reported after a successful write:

  Verification failed on /dev/sdb: Cannot open device: [Errno 13]
  Permissão negada: '/dev/sdb'

The device node belongs to root, so the verification has to go through
UDisks2 (polkit) like the write path does, and it should reuse the
descriptor the writer already holds instead of opening the device a second
time (every privileged open may need its own authorization).
"""
import os
import tempfile
from unittest.mock import patch

import pytest

from yaku.core import verify
from yaku.core.verify import (
    VerifyError,
    VerifyMismatchError,
    verify_device_matches_image,
    verify_fd_matches_image,
)


def _progress(_done, _total):
    return None


def _not_stopped():
    return False


class TestVerifyOpen:
    """The device has to be opened through UDisks2."""

    def test_uses_udisks_instead_of_a_plain_open(self):
        fd = os.open(os.devnull, os.O_RDONLY)
        with patch.object(verify, "_open_device_readonly", return_value=fd) as opener, \
                patch.object(verify, "verify_fd_matches_image", return_value=True):
            verify_device_matches_image("/dev/sdb", "/img.iso", _progress, _not_stopped)
        assert opener.called
        # verify_device_matches_image closes the descriptor it was given.

    def test_direct_open_failure_is_reported(self):
        from yaku.udisks import client

        with patch.object(client, "bus", return_value=None), \
                patch.object(client, "open_device_exclusive",
                             side_effect=client.UDisksError("nope")), \
                patch("os.open", side_effect=PermissionError(13, "Permission denied")):
            with pytest.raises(VerifyError) as exc:
                verify._open_device_readonly("/dev/sdb")
        assert "Cannot open device" in str(exc.value)

    def test_udisks_open_does_not_ask_for_an_exclusive_lock(self):
        """The write path may still hold the device open during verification."""
        from yaku.udisks import client

        calls = {}

        def fake_open(_conn, device, mode="w", flags=0, exclusive=True):
            calls["mode"] = mode
            calls["exclusive"] = exclusive
            return os.open(os.devnull, os.O_RDONLY)

        with patch.object(client, "bus", return_value=None), \
                patch.object(client, "open_device_exclusive", side_effect=fake_open):
            fd = verify._open_device_readonly("/dev/sdb")
            os.close(fd)

        assert calls["mode"] == "r"
        assert calls["exclusive"] is False


class TestVerifyFd:
    """Verification through an already open descriptor."""

    def _image(self, tmp, payload):
        path = os.path.join(tmp, "image.iso")
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    def _device(self, tmp, payload):
        path = os.path.join(tmp, "device.img")
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    def test_matching_data_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = b"A" * 4096 + b"B" * 8192
            image = self._image(tmp, payload)
            device = self._device(tmp, payload)
            fd = os.open(device, os.O_RDONLY)
            try:
                assert verify_fd_matches_image(fd, image, _progress, _not_stopped) is True
            finally:
                os.close(fd)

    def test_mismatch_is_reported_with_the_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = self._image(tmp, b"A" * 4096)
            device = self._device(tmp, b"A" * 2048 + b"B" * 2048)
            fd = os.open(device, os.O_RDONLY)
            try:
                with pytest.raises(VerifyMismatchError) as exc:
                    verify_fd_matches_image(fd, image, _progress, _not_stopped)
            finally:
                os.close(fd)
        assert "Mismatch at offset 2048" in str(exc.value)

    def test_range_beyond_the_device_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = self._image(tmp, b"A" * 8192)
            device = self._device(tmp, b"A" * 1024)
            fd = os.open(device, os.O_RDONLY)
            try:
                with pytest.raises(VerifyError):
                    verify_fd_matches_image(fd, image, _progress, _not_stopped)
            finally:
                os.close(fd)


if __name__ == "__main__":
    pytest.main([__file__])
