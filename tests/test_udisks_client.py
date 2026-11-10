"""Regression tests for the UDisks2 client.

Covers the flash failure reported on UDisks2 2.10+:

  D-Bus call failed: GDBus.Error:org.freedesktop.UDisks2.Error.Failed:
  Using 'O_RDWR', 'O_RDONLY' and 'O_WRONLY' flags is not permitted.
  Use 'mode' argument instead.
"""
import os
from unittest.mock import patch

import pytest

from lubus.udisks import client


class _FakeReply:
    def __init__(self, value):
        self.value = value

    def unpack(self):
        return (self.value,)


class _FakeFdList:
    def __init__(self, fd=7):
        self.fd = fd

    def get(self, _index):
        return self.fd


class TestOpenDeviceExclusive:
    """Block.OpenDevice changed its argument contract in UDisks2 2.10."""

    OBJ = "/org/freedesktop/UDisks2/block_devices/sdb"

    def _call_recorder(self, results):
        calls = []

        def fake_call(conn, obj, interface, method, args, reply_signature, **kwargs):
            calls.append((obj, interface, method, args, kwargs))
            outcome = results.pop(0) if results else None
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return calls, fake_call

    def test_modern_api_uses_mode_and_no_access_flags(self):
        calls, fake_call = self._call_recorder([(_FakeReply(0), _FakeFdList())])
        with patch.object(client, "resolve_device", return_value=self.OBJ), \
                patch.object(client, "_call", side_effect=fake_call):
            client.open_device_exclusive(None, "/dev/sdb", "rw", os.O_SYNC)

        assert len(calls) == 1
        _obj, _iface, method, args, _kwargs = calls[0]
        assert method == "OpenDevice"
        mode, options = args.unpack()
        assert mode == "rw"
        flags = options["flags"]
        assert flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR) == 0
        assert flags & os.O_EXCL
        assert flags & os.O_SYNC

    def test_write_mode_maps_to_rw(self):
        calls, fake_call = self._call_recorder([(_FakeReply(0), _FakeFdList())])
        with patch.object(client, "resolve_device", return_value=self.OBJ), \
                patch.object(client, "_call", side_effect=fake_call):
            client.open_device_exclusive(None, "/dev/sdb", "w")

        mode, _options = calls[0][3].unpack()
        assert mode == "rw"

    def test_read_only_mode(self):
        calls, fake_call = self._call_recorder([(_FakeReply(0), _FakeFdList())])
        with patch.object(client, "resolve_device", return_value=self.OBJ), \
                patch.object(client, "_call", side_effect=fake_call):
            client.open_device_exclusive(None, "/dev/sdb", "r")

        mode, options = calls[0][3].unpack()
        assert mode == "r"
        flags = options["flags"]
        assert flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR) == 0

    def test_legacy_udisks_falls_back_to_flags(self):
        """Older UDisks2 releases still need the access bits in the flags."""
        calls, fake_call = self._call_recorder([
            client.UDisksError(
                "D-Bus call failed: Using 'O_RDWR', 'O_RDONLY' and 'O_WRONLY' "
                "flags is not permitted. Use 'mode' argument instead."),
            (_FakeReply(0), _FakeFdList()),
        ])
        with patch.object(client, "resolve_device", return_value=self.OBJ), \
                patch.object(client, "_call", side_effect=fake_call):
            client.open_device_exclusive(None, "/dev/sdb", "rw")

        assert len(calls) == 2
        _mode, options = calls[1][3].unpack()
        flags = options["flags"]
        assert flags & os.O_RDWR

    def test_other_errors_are_not_swallowed(self):
        calls, fake_call = self._call_recorder([
            client.UDisksError("D-Bus call failed: Access denied"),
        ])
        with patch.object(client, "resolve_device", return_value=self.OBJ), \
                patch.object(client, "_call", side_effect=fake_call):
            with pytest.raises(client.UDisksError):
                client.open_device_exclusive(None, "/dev/sdb", "rw")

        assert len(calls) == 1


if __name__ == "__main__":
    pytest.main([__file__])
