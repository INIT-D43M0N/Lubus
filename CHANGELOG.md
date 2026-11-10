# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [5.0.0] - 2026-11-08

### Added
- Direct drive installation / portable mode for Windows and Linux images
- Automatic and custom partitioning for direct installs
- User provisioning (username, password, full name, hostname, timezone)
- Simultaneous flashing to multiple drives with aggregated progress
- Native Libadwaita Select Drive screen with multi-drive selection

### Fixed
- Progress no longer reaches 100% before the write and sync complete

## [Unreleased]

### Added
- Initial project structure
- Core modules for device detection, image analysis, partition layouts
- UDisks2 D-Bus integration for safe disk operations
- GTK4/Libadwaita UI with wizard-style flow
- Support for Windows installer media (UEFI/GPT, BIOS/MBR)
- Optional storage-controller driver staging for Intel VMD/RST and AMD RAID systems
- Support for Linux ISOhybrid (dd mode) and installer (extract mode)
- Windows To Go support (experimental)
- Real-time progress tracking with speed/ETA
- Optional verification after write
- Multi-language support (English, Portuguese, Russian)
- Theme support (System, Light, Dark)
- Comprehensive safety checks and confirmation dialogs
- ISO9660 directory-tree parser used for image detection and bootability checks

### Changed
- N/A

### Fixed
- Opening the drive no longer sends the access bits in the `flags` option of `Block.OpenDevice`, which UDisks2 has refused since 2.7.3, and the unreachable legacy fallback was removed: the access mode travels in the `mode` argument ("r", "w" or "rw") and a daemon without `Block.OpenDevice` now gets a clear message instead of a cryptic D-Bus error
- Verifying the written data no longer fails with "Cannot open device: [Errno 13] Permission denied": the device is opened through UDisks2 (polkit) like the write path, and the dd writer reuses the descriptor it already holds, so no second privileged open is needed
- The read back uses sector aligned `O_DIRECT` transfers with a buffered fallback, so it cannot fail on alignment rules, and reports the offset of the first differing byte on a mismatch
- Flashing no longer fails with "Using 'O_RDWR', 'O_RDONLY' and 'O_WRONLY' flags is not permitted. Use 'mode' argument instead.": the drive is now opened through the `mode` argument of `Block.OpenDevice`, which has been the contract since UDisks2 2.7.3 (2017)
- Linux ISOs (Fedora, Ubuntu, …) are no longer misdetected as Windows media: detection reads the ISO directory tree instead of scanning raw bytes, where the GRUB binaries embedded in the image made "windows", "bootmgr" and "bcd" match
- Image analysis no longer mounts the image, so it cannot fail with `No such interface "org.freedesktop.UDisks2.Filesystem" on object at path …/block_devices/loop0`
- Hybrid images (Fedora, recent Ubuntu) are mounted through the partition that holds the ISO9660 filesystem instead of the loop device, which has no Filesystem interface
- Windows edition listing no longer aborts image analysis when the image cannot be mounted
- Stale loop mounts are unmounted through their child partitions and detached with a bounded timeout, so a leftover mount can no longer block the wizard
- Windows 11 24H2+ installer media keeps `Sources\SetupHost.exe` and the install image on the same NTFS volume, fixing the misleading “Install driver to show hardware” prompt caused by split FAT32+NTFS media

## [1.0.0] - 2026-11-04

### Added
- First public release