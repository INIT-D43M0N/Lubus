Name:           lubus
Version:        5.0.0
Release:        5%{?dist}
Summary:        Create bootable USB drives on Linux (Rufus alternative)

License:        GPL-3.0-or-later
URL:            https://github.com/yourusername/lubus
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
Lubus is a modern, native Linux application for creating bootable USB drives,
functionally identical to Rufus but built specifically for the Linux ecosystem
using GTK4, Libadwaita, and Python.

Features:
- Automatic detection of removable USB drives and SD cards
- Support for ISO, IMG, and other bootable disk images
- Automatic detection of image type (Windows, Linux, hybrid)
- Partition scheme selection (GPT/MBR) and target system (UEFI/BIOS)
- Filesystem selection (FAT32, NTFS, exFAT, ext4, btrfs)
- DD mode (block copy) and ISO extract mode
- Real-time progress with speed and ETA
- Optional verification after write
- Windows installer media creation
- Windows To Go support
- Direct drive installation / portable mode (Windows and Linux)
- Windows GPT/UEFI + MSR, MBR/BIOS, autounattend, OOBE and Win11 bypasses
- Optional Windows Defender removal / debloat
- Automatic or custom partitioning with first-boot user provisioning
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
desktop-file-validate %{buildroot}%{_datadir}/applications/com.github.yourusername.lubus.desktop
appstream-util validate-relax --nonet %{buildroot}%{_datadir}/metainfo/com.github.yourusername.lubus.metainfo.xml

%files
%license LICENSE
%doc README.md CHANGELOG.md
%{_bindir}/lubus
%{_datadir}/applications/com.github.yourusername.lubus.desktop
%{_datadir}/metainfo/com.github.yourusername.lubus.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/com.github.yourusername.lubus.svg
%{_datadir}/icons/hicolor/1024x1024/apps/com.github.yourusername.lubus.png
%{_datadir}/lubus/
%{python3_sitelib}/lubus/

%changelog
* Sun Nov 08 2026 Lubus Contributors <lubus@github.com> - 5.0.0-5
- Remove the Screen 4 mini-game and the WebKitGTK runtime dependency
- Keep the flash page focused on progress, result actions and the log

* Sun Nov 08 2026 Lubus Contributors <lubus@github.com> - 5.0.0-4
- Rebuild Select Drive with a native libadwaita preferences layout
- Remove the custom CRT dashboard widget and its bundled media
- Move all-drive selection into a standard Adw.Dialog with a header bar,
  per-drive checkboxes and a Select All master control
- Keep the two-drive dashboard summary and native empty/error states

* Sun Nov 08 2026 Lubus Contributors <lubus@github.com> - 5.0.0-3
- Add the retro CRT television dashboard widget with the animated waifu GIF
- Rebuild Screen 1 hierarchy with the TV above installation and drive controls
- Show only the first two drives on the dashboard; add a More Drives modal
  listing every drive with a Select All master checkbox

* Sun Nov 08 2026 Lubus Contributors <lubus@github.com> - 5.0.0-2
- Add the Linux ISO catalog, mirror resolver and segmented downloader with
  pause/resume/cancel and SHA-256 verification
- Add Windows direct-install GPT/UEFI (ESP + MSR + NTFS) and MBR/BIOS layouts
- Add autounattend generation: OOBE skip, local account, telemetry controls,
  Windows 11 LabConfig bypasses and BypassNRO
- Add optional Windows Defender removal/debloat pipeline
- Allow secondary internal drives for direct installs and multi-target writes

* Sun Nov 08 2026 Lubus Contributors <lubus@github.com> - 5.0.0-1
- Version 5.0
- Add direct drive installation / portable mode with automatic and custom
  partitioning plus user provisioning
- Add simultaneous flashing to multiple drives for live and direct modes
- Fix progress reporting so 100% is only shown after writeback is flushed
- Select Drive rebuilt with native Libadwaita widgets

* Sun Nov 08 2026 Lubus Contributors <lubus@github.com> - 1.0.0-5
- Remove the character artwork from Select Drive
- Rebuild Select Drive with native Libadwaita widgets: preferences group,
  boxed drive rows, a disclosure for extra drives and an erasure banner

* Sun Nov 08 2026 Lubus Contributors <lubus@github.com> - 1.0.0-4
- Add the new application icon (PNG + scalable wrapper) across the desktop
  entry, window and About dialog
- Redesign the Select Drive step around the leaning character and a rounded
  drive panel, with top-two cards and an expandable list for extra drives
- Update the disc artwork used by the Step 2 flip and the Step 4 HDD platter

* Sat Nov 07 2026 Lubus Contributors <lubus@github.com> - 1.0.0-3
- Redesign the flash step to match the image-selection layout
- Replace the generic flash icon with an animated open-HDD graphic whose
  platter is the character disc, driven by a sweeping actuator arm
- Keep the safety subtitle visible and show the writer phase separately
- Promote the mini-game toggle to a primary pill button below the progress

* Sat Nov 07 2026 Lubus Contributors <lubus@github.com> - 1.0.0-2
- Add interactive 3D flip-disc easter egg on the image selection step
- Ship cleaned, centered transparent disc artwork assets

* Wed Nov 4 2026 Lubus Contributors <lubus@github.com> - 1.0.0-1
- Initial RPM release