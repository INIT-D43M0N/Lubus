# Yaku

**A modern, native Linux application for creating bootable USB drives and deploying portable operating systems.**

Yaku is a Rufus alternative built for the Linux ecosystem with **GTK4**, **Libadwaita** and **Python**, using GObject introspection. It writes ISO images to removable drives, builds Windows installer media, installs Windows To Go systems, writes ChromeOS recovery images, creates macOS installer disks and recognises the BSDs and the other Unixes.

**Project page: <https://github.com/INIT-D43M0N/Yaku>**

[![License](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://python.org)
[![GTK4](https://img.shields.io/badge/GTK-4.0+-orange.svg)](https://gtk.org)
[![Libadwaita](https://img.shields.io/badge/Libadwaita-1.3+-green.svg)](https://gnome.pages.gitlab.gnome.org/libadwaita/)
[![Flatpak](https://img.shields.io/badge/flatpak-com.github.INIT-D43M0N.yaku-purple.svg)](com.github.INIT-D43M0N.yaku.yml)

> **Note:** this project was formerly called *Lubus*. From version 6.1.0 on, every
> name — the Python package, the app id, the icons, the package files and the
> data directories — follows the **Yaku** name.

---

## Features

- **Native GNOME interface** — a clean libadwaita layout with grouped settings, accessible rows, dialogs and responsive pages.
- **Drive detection** — lists removable USB drives and SD cards with model, device path, bus type and capacity. Internal system disks and mounted drives are excluded by default.
- **Two-drive summary with multi-target selection** — the dashboard shows the first two drives; **More Drives…** opens a dialog with every detected drive and a **Select All** master control.
- **Image selection** — choose a local ISO, IMG, DD, RAW, BIN or DMG file, with automatic image-type detection.
- **Linux ISO downloader** — browse Debian, Ubuntu, Kubuntu, Xubuntu, Lubuntu, Ubuntu Budgie, Ubuntu MATE, Ubuntu Studio and Ubuntu Unity editions, resolve mirrors automatically and verify the download with SHA-256.
- **Live USB mode** — write a bootable installer image straight to one or more drives.
- **Direct install / portable mode** — deploy a portable system with automatic or custom partitioning and unattended user provisioning.
- **Windows deployment options** — create Windows installer media, configure Windows To Go-style portable installs, skip OOBE screens, bypass the Windows 11 requirements, optionally remove Windows Defender and stage storage-controller drivers for Intel VMD/RST and AMD RAID systems.
- **ChromeOS support** — write a ChromeOS recovery image byte for byte, exactly as Google describes it. The recovery image is recognised by its partition table, not by a lucky string in the bytes.
- **Brunch support** — turn the same recovery image into a [Brunch](https://github.com/sebanc/brunch) install, which runs ChromeOS on a regular computer. The twelve-partition layout ChromeOS expects is built by Yaku itself — byte for byte like the project's own `cgpt` tool — and the Brunch files are downloaded once and kept for the next time.
- **macOS support** — write Apple disk images. The DMG is converted and the drive gets the layout Apple's own `createinstallmedia` uses: an EFI system partition with the bootloader in front of an Apple_HFS partition holding the installer.
- **BSD and Unix support** — FreeBSD, OpenBSD, NetBSD, DragonFly BSD, illumos (OmniOS, OpenIndiana) and Haiku. Their hybrid ISOs and their raw disk images are recognised from the partition table; the installation CDs that only work from an optical drive say so instead of quietly producing a drive that does not boot.
- **Writing modes** — DD block copy or ISO file extraction, depending on the image.
- **Progress and results** — live speed, ETA, target information, result actions and a technical log.
- **Safety checks** — destructive confirmation, system-disk filtering, drive identity verification, safe unmounting and optional verification after writing.

---

## Screenshots

| Select Drive | Direct Install / Portable |
| --- | --- |
| ![Select Drive](screenshots/1.png) | ![Direct install configuration](screenshots/2.png) |

| Select Image | Download Linux ISO |
| --- | --- |
| ![Select bootable image](screenshots/3.png) | ![Linux ISO downloader](screenshots/4.png) |

| Summary | Flashing |
| --- | --- |
| ![Installation summary](screenshots/5.png) | ![Flash progress](screenshots/6.png) |

---

## Architecture

```text
yaku/
├── __init__.py              # Package metadata
├── main.py                  # Application entry point
├── window.py                # Main window and wizard navigation
├── config.py                # User configuration
├── pages/                   # GTK4/Libadwaita pages
│   ├── base.py              # Shared page loader
│   ├── drive_page.py        # Step 1: installation mode and target drives
│   ├── drive_dialog.py      # All-drives dialog with Select All
│   ├── image_page.py        # Step 2: image selection and write options
│   ├── brunch_page.py       # Step 3: ChromeOS / Brunch options
│   ├── iso_download.py      # Linux ISO catalog and downloader UI
│   ├── summary_page.py      # Confirmation and Windows options
│   ├── flash_page.py        # Progress, result and logs
│   ├── disc.py              # Interactive disc artwork
│   └── hdd.py               # Animated HDD progress artwork
├── core/                    # Non-GTK application logic
│   ├── devices.py           # Drive enumeration and identity
│   ├── images.py            # Image analysis and detection
│   ├── partitions.py        # GPT/MBR partition layouts
│   ├── gpt.py               # GUID partition table reader and writer
│   ├── chromeos.py          # ChromeOS recovery image recognition
│   ├── brunch.py            # Brunch layout, copy plan and assets
│   ├── macos.py             # macOS DMG and HFS+ handling
│   ├── bsd.py               # BSD and other Unix media detection
│   ├── writer.py            # Live USB write operations
│   ├── direct_install.py    # Portable installation logic
│   ├── multi_flash.py       # Parallel progress aggregation
│   ├── provisioning.py      # Linux and Windows unattended setup
│   ├── windows_install.py   # Windows installer media
│   ├── windows_togo.py      # Portable Windows deployment
│   ├── windows_boot.py      # UEFI/BIOS boot files
│   ├── windows_debloat.py   # Optional Windows cleanup
│   ├── windows_drivers.py   # Storage-driver staging for Setup
│   ├── iso_catalog.py       # Distribution catalog and mirrors
│   ├── iso_downloader.py    # Segmented downloads and SHA-256 checks
│   ├── bcd.py               # Windows BCD handling
│   ├── verify.py            # Write verification
│   └── icon_utils.py        # Icon helpers
├── udisks/                  # UDisks2 D-Bus client
│   ├── __init__.py
│   └── client.py
└── utils/                   # Shared helpers
    ├── i18n.py
    ├── logging.py
    ├── resources.py
    └── threading.py
```

The data files live in `data/` (the GTK4 `.ui` definitions, the stylesheet, the artwork and the ISO catalog) and are installed into `/usr/share/yaku`.

---

## Requirements

### Runtime

- Python 3.10 or newer
- PyGObject
- GTK 4.10 or newer
- Libadwaita 1.3 or newer
- UDisks2 2.7.3 or newer (the `Block.OpenDevice` method with the `mode` argument)
- Polkit
- A Polkit authentication agent, so the authorization dialog for opening the drive can be shown (`polkit-gnome` on GNOME, `polkit-kde` on KDE). Without one, Polkit cannot ask for the password and the privileged operations either hang or fail
- `dosfstools` for FAT32
- `ntfs-3g` for NTFS
- `exfatprogs` for exFAT
- `e2fsprogs` for ext4
- `btrfs-progs` for btrfs
- wimlib for Windows image handling
- GRUB tools for BIOS and UEFI boot files
- `dmg2img` for macOS disk images (`.dmg`)
- Internet access for the Brunch files, which are downloaded on demand and kept in `~/.local/share/yaku/brunch/`

### Build

- Meson 1.0 or newer
- Ninja
- `pkg-config`
- Gettext
- `desktop-file-validate`
- `appstream-util`
- GTK4, Libadwaita, GLib and GObject development packages

---

## Installation

Download the artifact for your distribution from the [Releases](https://github.com/INIT-D43M0N/Yaku/releases) page.

### Fedora

```bash
sudo dnf install ./yaku-6.1.0-1.fc45.noarch.rpm
```

### SUSE Linux Enterprise / openSUSE

```bash
sudo zypper install ./yaku-6.1.0-1.fc45.noarch.rpm
```

### Ubuntu / Debian

```bash
sudo apt install ./yaku_6.1.0-1_all.deb
```

### From source

```bash
git clone https://github.com/INIT-D43M0N/Yaku.git
cd Yaku

meson setup builddir --prefix=/usr
meson compile -C builddir
sudo meson install -C builddir
```

### Flatpak

```bash
flatpak-builder --force-clean build-dir com.github.INIT-D43M0N.yaku.yml
flatpak-builder --run build-dir com.github.INIT-D43M0N.yaku.yml yaku
```

---

## Usage

1. **Select Drive** — choose Live USB or Direct install, then select one or more target drives.
2. **Select Image** — choose a local image or download a Linux ISO.
3. **ChromeOS Options** — only shown for a ChromeOS recovery image: write the image as it is, or turn it into a Brunch install that runs ChromeOS on a regular computer.
4. **Review Summary** — confirm the image, the targets, the installation mode and the Windows options.
5. **Flash** — start the write and follow the progress until it completes.

When more than two drives are connected, use **More Drives…** to open the complete drive list and include several targets at once.

The command line entry point is `yaku`:

```bash
yaku --version
yaku --iso ubuntu.iso --drive /dev/sdb
yaku ubuntu.iso
```

### ChromeOS and Brunch

A ChromeOS recovery image is a raw disk image with its own partition table, so Yaku recognises it by that table and copies it to the drive byte for byte — the same thing Google asks you to do.

To run ChromeOS on an ordinary computer instead, choose **ChromeOS with Brunch** in the *ChromeOS Options* step:

- Brunch needs its own files (about 900 MB). They are downloaded once, on demand, and kept in `~/.local/share/yaku/brunch/` for the next time.
- Pick **Secure Boot** or **Legacy boot** depending on your computer.
- The drive gets the twelve-partition ChromeOS layout, built by Yaku itself, byte for byte like the `cgpt` tool of the Brunch project.
- The drive must have at least 14 GB of space, and the image must be the recovery image (`chromeos_image.bin`), not the zip file Google downloads.

The [Brunch project](https://github.com/sebanc/brunch) explains which recovery image matches your computer and how to pick the right kernel.

### macOS disk images

Yaku writes Apple's installer disk images. The `.dmg` is converted with `dmg2img`, and the drive receives the layout Apple's own `createinstallmedia` uses: an EFI system partition with the bootloader in front of an Apple_HFS partition holding the installer. The HFS+ volume is written as raw blocks and never mounted.

### BSD and Unix media

The BSDs and the other Unixes ship their media in two shapes, and Yaku tells them apart:

- **Hybrid ISOs** (FreeBSD `disc1`, the OmniOS installer, the Haiku *anyboot* image) and **raw disk images** (`installXX.img`, `*-install.img`, `memstick.img`, `*.usb-dd`) are copied to the drive as a whole, keeping their own partition table.
- **Installation CDs** (OpenBSD, NetBSD, DragonFly BSD) only work from an optical drive. Yaku says so and asks for the `.img` file instead of quietly producing a drive that does not boot.

FreeBSD, OpenBSD, NetBSD, DragonFly BSD, illumos (OmniOS, OpenIndiana) and Haiku are recognised from the files inside the ISO or from the partition table of the image.

### Windows Setup and storage drivers

Windows Setup only ships the in-box AHCI/NVMe drivers. On systems with Intel VMD/RST or AMD RAID enabled, the target disk is not detected and Setup asks for a driver.

Yaku writes the complete Windows media tree — including `sources\install.wim` and `sources\SetupHost.exe` — to the NTFS volume, and keeps only the boot chain on the FAT32 volume. Windows 11 24H2+ (ConX setup) locates the media by `SetupHost.exe` and then expects the install image on that same volume; split media would otherwise show the misleading **“Install driver to show hardware”** dialog even when the USB is fine.

- In **Summary → Windows Setup → Storage Controller Drivers**, select the extracted vendor driver folder. Yaku copies it to `\drivers` on the USB so Setup's **Load driver → Browse** can use it.
- Intel RST VMD: extract `f6vmdflpy-x64.zip` (`iaStorVD.inf`); Intel AHCI/RAID: `f6flpy-x64.zip` (`iaStorAC.inf`); AMD RAID: `rcraid.inf`, `rcbottom.inf`, `rccfg.inf`.
- Alternatively, disable Intel VMD/RST or switch SATA mode to AHCI in the firmware before installing.

---

## Safety

- Internal system and mounted drives are excluded by default.
- A warning banner and a confirmation dialog are shown before destructive writes.
- Drive serial numbers and WWNs are checked before writing, to prevent a target swap.
- Privileged operations use UDisks2 and Polkit; Yaku does not run as root.
- Partitions are unmounted safely before and after the operations.
- Optional verification is available for the supported raw writes.

---

## Development

### Run the tests

```bash
python3 -m pytest tests/
```

### Code style

```bash
ruff check yaku/
ruff format yaku/
```

### Type checking

```bash
mypy yaku/
```

### Build the packages

```bash
# RPM (Fedora, RHEL, openSUSE)
./packaging/build-rpm.sh --local

# DEB (Debian, Ubuntu)
./packaging/build-deb.sh

# Source tarball
git archive --format=tar.gz --prefix="yaku-6.1.0/" -o yaku-6.1.0.tar.gz HEAD
```

The RPM is written to `rpmbuild/RPMS/` and the DEB to `debbuild/`; the artifacts published on the [Releases](https://github.com/INIT-D43M0N/Yaku/releases) page are kept in `dist/`.

---

## Contributing

1. Fork the repository.
2. Create a feature branch.
3. Make your changes.
4. Run the tests and the linters.
5. Open a pull request.

---

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) for details.

---

## Acknowledgments

- [GTK](https://gtk.org/) and [Libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/) for the native Linux interface.
- [UDisks2](https://www.freedesktop.org/wiki/Software/udisks/) for safe, Polkit-integrated disk operations.
- [wimlib](https://wimlib.net/) for Windows image handling.
- [GRUB](https://www.gnu.org/software/grub/) for bootloader support.
- [Brunch](https://github.com/sebanc/brunch) for the ChromeOS-on-a-PC project, whose kernel and bootloader Yaku installs and whose partition layout it reproduces.
