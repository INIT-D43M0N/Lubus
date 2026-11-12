# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [6.1.0] - 2026-11-11

### Changed
- The project is now called **Yaku**. Every name that referred to the old one follows the rename: the Python package (`yaku/`), the data directory (`/usr/share/yaku`), the desktop entry and AppStream metadata (`com.github.init_d43m0n.yaku`), the icons, the Flatpak manifest, the `yaku` command, the translation domain, the RPM/DEB package names and the source tarball
- The project pages point to <https://github.com/INIT-D43M0N/Yaku>
- The GitHub repository was renamed from `Lubus` to `Yaku`; the old URL keeps redirecting to the new one

## [6.0.0] - 2026-11-10

### Added
- ChromeOS recovery images are written to the drive. The image is recognised by its GPT partition table (`EFI-SYSTEM` + `CHROMEOS`), not by a string in the bytes, and copied byte for byte
- A new **ChromeOS Options** step, shown only for ChromeOS images, with a Brunch install: the drive receives the twelve-partition ChromeOS layout, built by Yaku itself, and the Brunch kernel and bootloader are installed in it
- Brunch assets are downloaded on demand (about 900 MB), kept in `~/.local/share/yaku/brunch/`, and can be fetched again from the same screen. Secure Boot and legacy boot are both supported
- macOS installer disk images (`.dmg`) are written with the layout Apple's own `createinstallmedia` uses: an EFI system partition with the bootloader in front of an Apple_HFS partition holding the installer
- BSD and other Unix media are supported: FreeBSD, OpenBSD, NetBSD, DragonFly BSD, illumos (OmniOS, OpenIndiana) and Haiku. Hybrid ISOs and raw disk images are recognised and copied as a whole
- A GUID partition table writer (`yaku/core/gpt.py`) that produces the same byte layout as ChromeOS' own `cgpt` tool, including the protective MBR with the EFI boot GUID
- `Block.Rescan` is used after a hand-written table, so the new partitions show up without rebooting

### Changed
- The project pages point to <https://github.com/INIT-D43M0N/Yaku>
- ChromeOS, macOS and BSD/Unix images ignore the write-mode choice: they are written the way their format requires. "Direct install (portable)" is disabled for them
- Installation CDs that only work from an optical drive (OpenBSD, NetBSD, DragonFly BSD) now say so and ask for the `.img` file, instead of quietly producing a drive that does not boot

### Fixed
- The ChromeOS recognition no longer depends on a byte marker that conflicts with the protective MBR Yaku itself writes

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