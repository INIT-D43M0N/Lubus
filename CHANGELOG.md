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

### Changed
- N/A

### Fixed
- Windows 11 24H2+ installer media keeps `Sources\SetupHost.exe` and the install image on the same NTFS volume, fixing the misleading “Install driver to show hardware” prompt caused by split FAT32+NTFS media

## [1.0.0] - 2026-11-04

### Added
- First public release