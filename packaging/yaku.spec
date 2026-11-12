Name:           yaku
Version:        6.1.2
Release:        1%{?dist}
Summary:        Create bootable USB drives on Linux (Rufus alternative)

License:        GPL-3.0-or-later
URL:            https://github.com/INIT-D43M0N/Yaku
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  meson >= 1.0
BuildRequires:  ninja-build
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
BuildRequires:  gettext

Requires:       python3-gobject
Requires:       gtk4 >= 4.10
Requires:       libadwaita >= 1.3
Requires:       udisks2
Requires:       wimlib-utils
Requires:       grub2-tools
Requires:       dosfstools
Requires:       ntfs-3g
Requires:       exfatprogs
Requires:       e2fsprogs
Requires:       btrfs-progs
Requires:       hicolor-icon-theme

%description
Yaku is a modern, native Linux application for creating bootable USB drives,
functionally identical to Rufus but built specifically for the Linux ecosystem
using GTK4, Libadwaita, and Python.

Features:
- Automatic detection of removable USB drives and SD cards
- Support for ISO, IMG, DD, RAW, BIN and DMG images
- Automatic detection of the image type (Windows, Linux, ChromeOS, macOS, BSD)
- Partition scheme selection (GPT/MBR) and target system (UEFI/BIOS)
- Filesystem selection (FAT32, NTFS, exFAT, ext4, btrfs)
- DD mode (block copy) and ISO extract mode
- Real-time progress with speed and ETA
- Optional verification after write
- Windows installer media creation
- Windows To Go support
- Storage-controller driver staging for Intel VMD/RST and AMD RAID systems
- Direct drive installation / portable mode (Windows and Linux)
- Windows GPT/UEFI + MSR, MBR/BIOS, autounattend, OOBE and Win11 bypasses
- Optional Windows Defender removal / debloat
- Automatic or custom partitioning with first-boot user provisioning
- ChromeOS recovery images, with optional Brunch for regular computers
- macOS installer disk images (EFI + HFS+ layout)
- FreeBSD, OpenBSD, NetBSD, DragonFly BSD, illumos and Haiku media
- Simultaneous flashing to multiple drives
- Integrated Linux ISO catalog and segmented verified downloader
- Retro CRT dashboard with animated character GIF
- Two-drive dashboard with a More Drives modal and Select All
- Safety checks and confirmation dialogs

%prep
%autosetup -p1

%build
%meson
%meson_build

%install
%meson_install

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/com.github.init_d43m0n.yaku.desktop
appstream-util validate-relax --nonet %{buildroot}%{_datadir}/metainfo/com.github.init_d43m0n.yaku.metainfo.xml

%files
%license LICENSE
%doc README.md CHANGELOG.md
%{_bindir}/yaku
%{_datadir}/applications/com.github.init_d43m0n.yaku.desktop
%{_datadir}/metainfo/com.github.init_d43m0n.yaku.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/com.github.init_d43m0n.yaku.svg
%{_datadir}/icons/hicolor/1024x1024/apps/com.github.init_d43m0n.yaku.png
%{_datadir}/yaku/
%{python3_sitelib}/yaku/
# The bytecode compiled during the build belongs to the build machine and
# must not be shipped.
%exclude %{python3_sitelib}/yaku/__pycache__
%exclude %{python3_sitelib}/yaku/core/__pycache__
%exclude %{python3_sitelib}/yaku/pages/__pycache__
%exclude %{python3_sitelib}/yaku/udisks/__pycache__
%exclude %{python3_sitelib}/yaku/utils/__pycache__

%changelog
* Thu Nov 12 2026 Yaku Contributors <yaku@github.com> - 6.1.2-1
- Build the packages in containers in the CI: the RPM in Fedora and the
  DEB in Debian, so the published artifacts do not depend on the machine
  that runs the build
- Fix the script that tells the binary RPM from the source RPM. Its grep
  pattern treated the dot as a wildcard and discarded every file under a
  path like /src/rpmbuild
- Thu Nov 12 2026 Yaku Contributors <yaku@github.com> - 6.1.1-1
- Fix the Linux direct install: the hostname and the timezone are written
  again when the ISO payload has no /etc directory, and the filesystems
  are flushed before they are unmounted, so the volume is no longer left
  busy on slow USB drives
* Wed Nov 11 2026 Yaku Contributors <yaku@github.com> - 6.1.0-1
- Rename the project from Lubus to Yaku. The Python package, the data
  directory, the desktop entry, the AppStream metadata, the icons, the
  Flatpak app id and the RPM/DEB package names all follow the new name,
  so nothing refers to the old one any more
- Move the project pages to https://github.com/INIT-D43M0N/Yaku
* Tue Nov 10 2026 Yaku Contributors <yaku@github.com> - 6.0.0-1
- Write ChromeOS recovery images. They are raw disk images with their own
  partition table, so they are copied to the drive byte for byte
- Add a ChromeOS options step: a recovery image can be written as it is or
  as a Brunch install, which runs ChromeOS on a regular computer. Brunch
  needs its own files (about 900 MB), which are downloaded once and kept
- Write macOS installer disk images. The DMG is converted and the drive
  gets the layout Apple's own createinstallmedia uses: an EFI system
  partition with the bootloader in front of an Apple_HFS partition
- Support the BSDs and the other Unixes: FreeBSD, OpenBSD, NetBSD,
  DragonFly BSD, illumos and Haiku. Their hybrid ISOs and their raw disk
  images are recognised, and the installation CDs that only work from an
  optical drive say so instead of writing a drive that does not boot
- Write the GUID partition table in the application itself, byte for byte
  like ChromeOS' own cgpt tool does, so the tables the firmware reads are
  exactly the ones the project expects
- Link the project pages to https://github.com/INIT-D43M0N/Yaku
* Tue Nov 10 2026 Yaku Contributors <yaku@github.com> - 5.0.0-10
- Stop sending the access bits in the flags option of Block.OpenDevice,
  which UDisks2 has refused since 2.7.3, and remove the unreachable legacy
  fallback; the access mode now travels in the mode argument, as the daemon
  expects
- Report a clear error when the UDisks2 daemon is too old to have
  Block.OpenDevice, instead of a cryptic D-Bus failure
- Ask for the drive with the least privilege needed: a write-only open for
  the write phase and a read-write one only when the data is verified
- Document the Polkit authentication agent as a runtime requirement

* Mon Nov 09 2026 Yaku Contributors <yaku@github.com> - 5.0.0-9
- Verify the written data through UDisks2 instead of opening the device
  node directly, fixing "Verification failed on /dev/sdb: Cannot open
  device: [Errno 13] Permission denied"
- Reuse the descriptor the dd writer already holds for the read back, so the
  device does not have to be opened (and authorized) a second time
- Read back with sector aligned O_DIRECT transfers and fall back to buffered
  reads when the kernel refuses them, and report the offset of the first
  differing byte on a mismatch

* Mon Nov 09 2026 Yaku Contributors <yaku@github.com> - 5.0.0-8
- Open the target drive with the mode argument of Block.OpenDevice, fixing
  "Using 'O_RDWR', 'O_RDONLY' and 'O_WRONLY' flags is not permitted. Use
  'mode' argument instead." during flashing
- Detect the operating system from the ISO9660 directory tree instead of
  scanning raw bytes, so Linux media is no longer misdetected as Windows
  because of strings embedded in the GRUB bootloader
- Analyze images without mounting them, so image analysis cannot fail with
  "No such interface org.freedesktop.UDisks2.Filesystem" any more
- Mount hybrid images (Fedora, recent Ubuntu) through the partition holding
  the ISO filesystem, since the loop device itself has no Filesystem
  interface
- Release stale loop mounts through their child partitions with bounded
  timeouts, so a leftover mount can no longer block the wizard
- Fix the startup error that prevented the custom stylesheet from loading

* Mon Nov 09 2026 Yaku Contributors <yaku@github.com> - 5.0.0-7
- Detect the operating system from the ISO9660 directory tree instead of
  scanning raw bytes, so Linux media is no longer misdetected as Windows
  because of strings embedded in the GRUB bootloader
- Analyze images without mounting them, so image analysis cannot fail with
  "No such interface org.freedesktop.UDisks2.Filesystem" any more
- Mount hybrid images (Fedora, recent Ubuntu) through the partition holding
  the ISO filesystem, since the loop device itself has no Filesystem
  interface
- Release stale loop mounts through their child partitions with bounded
  timeouts, so a leftover mount can no longer block the wizard
- Fix the startup error that prevented the custom stylesheet from loading

* Sun Nov 08 2026 Yaku Contributors <yaku@github.com> - 5.0.0-6
- Stage storage-controller drivers for Intel VMD/RST and AMD RAID media
- Keep Sources\SetupHost.exe and the install image on the same NTFS volume
  so Windows 11 24H2+ ConX setup finds the installation media
- Add regression tests for the Windows media layout and driver staging

* Sun Nov 08 2026 Yaku Contributors <yaku@github.com> - 5.0.0-5
- Remove the Screen 4 mini-game and the WebKitGTK runtime dependency
- Keep the flash page focused on progress, result actions and the log

* Sun Nov 08 2026 Yaku Contributors <yaku@github.com> - 5.0.0-4
- Rebuild Select Drive with a native libadwaita preferences layout
- Remove the custom CRT dashboard widget and its bundled media
- Move all-drive selection into a standard Adw.Dialog with a header bar,
  per-drive checkboxes and a Select All master control
- Keep the two-drive dashboard summary and native empty/error states

* Sun Nov 08 2026 Yaku Contributors <yaku@github.com> - 5.0.0-3
- Add the retro CRT television dashboard widget with the animated waifu GIF
- Rebuild Screen 1 hierarchy with the TV above installation and drive controls
- Show only the first two drives on the dashboard; add a More Drives modal
  listing every drive with a Select All master checkbox

* Sun Nov 08 2026 Yaku Contributors <yaku@github.com> - 5.0.0-2
- Add the Linux ISO catalog, mirror resolver and segmented downloader with
  pause/resume/cancel and SHA-256 verification
- Add Windows direct-install GPT/UEFI (ESP + MSR + NTFS) and MBR/BIOS layouts
- Add autounattend generation: OOBE skip, local account, telemetry controls,
  Windows 11 LabConfig bypasses and BypassNRO
- Add optional Windows Defender removal/debloat pipeline
- Allow secondary internal drives for direct installs and multi-target writes

* Sun Nov 08 2026 Yaku Contributors <yaku@github.com> - 5.0.0-1
- Version 5.0
- Add direct drive installation / portable mode with automatic and custom
  partitioning plus user provisioning
- Add simultaneous flashing to multiple drives for live and direct modes
- Fix progress reporting so 100% is only shown after writeback is flushed
- Select Drive rebuilt with native Libadwaita widgets

* Sun Nov 08 2026 Yaku Contributors <yaku@github.com> - 1.0.0-5
- Remove the character artwork from Select Drive
- Rebuild Select Drive with native Libadwaita widgets: preferences group,
  boxed drive rows, a disclosure for extra drives and an erasure banner

* Sun Nov 08 2026 Yaku Contributors <yaku@github.com> - 1.0.0-4
- Add the new application icon (PNG + scalable wrapper) across the desktop
  entry, window and About dialog
- Redesign the Select Drive step around the leaning character and a rounded
  drive panel, with top-two cards and an expandable list for extra drives
- Update the disc artwork used by the Step 2 flip and the Step 4 HDD platter

* Sat Nov 07 2026 Yaku Contributors <yaku@github.com> - 1.0.0-3
- Redesign the flash step to match the image-selection layout
- Replace the generic flash icon with an animated open-HDD graphic whose
  platter is the character disc, driven by a sweeping actuator arm
- Keep the safety subtitle visible and show the writer phase separately
- Promote the mini-game toggle to a primary pill button below the progress

* Sat Nov 07 2026 Yaku Contributors <yaku@github.com> - 1.0.0-2
- Add interactive 3D flip-disc easter egg on the image selection step
- Ship cleaned, centered transparent disc artwork assets

* Wed Nov 4 2026 Yaku Contributors <yaku@github.com> - 1.0.0-1
- Initial RPM release