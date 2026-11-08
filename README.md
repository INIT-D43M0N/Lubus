# Lubus

A modern, native Linux application for creating bootable USB drives — functionally identical to Rufus, built specifically for the Linux ecosystem using **GTK4**, **Libadwaita**, and **Python** with GObject introspection.

[![License](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://python.org)
[![GTK4](https://img.shields.io/badge/GTK-4.0+-orange.svg)](https://gtk.org)
[![Libadwaita](https://img.shields.io/badge/Libadwaita-1.0+-green.svg)](https://gnome.pages.gitlab.gnome.org/libadwaita/)

## Features

- **Device Detection**: Automatically lists removable USB drives, SD cards, and block devices with accurate sizing, volume labels, and bus types (filters out internal system drives)
- **ISO/Image Selection**: Browse and select ISO, IMG, and other bootable disk images with automatic type detection (Windows installers, Linux live ISOs, hybrid images, dd-images)
- **Partition Scheme & Target System**: Choose between GPT and MBR, target UEFI vs BIOS/CSM
- **Advanced Formatting Options**: Custom volume label, file system selection (FAT32, NTFS, exFAT, ext4, btrfs), cluster size configuration
- **Writing Modes**: DD mode (block-level copy) vs ISO Image mode (file extraction)
- **Execution & Progress Tracking**: Real-time progress bar, status text (speed, elapsed time, remaining time), safe unmounting
- **Safety & Confirmation**: Clear warning dialogs before wiping any target drive
- **Windows Support**: Windows installer media creation (UEFI/GPT + FAT32, Legacy BIOS/MBR + NTFS)
- **Windows To Go**: Deploy runnable Windows to USB
- **Linux/ISOhybrid Support**: Direct block copy via `dd` for guaranteed bootability
- **Verify After Write**: Optional byte-for-byte verification

## Architecture

```
lubus/
├── __init__.py              # Package init
├── main.py                  # Application entry point
├── window.py                # Main window (Adw.ApplicationWindow)
├── pages/                   # Wizard pages
│   ├── __init__.py
│   ├── drive_page.py        # Step 1: Select target drive
│   ├── image_page.py        # Step 2: Select ISO/image
│   ├── options_page.py      # Step 3: Advanced options
│   ├── summary_page.py      # Step 4: Confirmation summary
│   └── flash_page.py        # Step 5: Progress & execution
├── core/                    # Core logic (no GTK dependencies)
│   ├── __init__.py
│   ├── devices.py           # Block device detection via UDisks2
│   ├── images.py            # ISO/image analysis & detection
│   ├── partitions.py        # Partition scheme logic (GPT/MBR)
│   ├── filesystems.py       # Filesystem operations (mkfs, etc.)
│   ├── writer.py            # Write operations (DD, ISO extract, Windows)
│   ├── windows.py           # Windows installer logic
│   ├── windows_togo.py      # Windows To Go logic
│   └── verify.py            # Verification logic
├── udisks/                  # UDisks2 D-Bus wrapper
│   ├── __init__.py
│   ├── client.py            # D-Bus connection & calls
│   ├── drives.py            # Drive enumeration
│   ├── partitions.py        # Partition operations
│   ├── filesystems.py       # Filesystem operations
│   └── loop.py              # Loop device management
├── utils/                   # Utilities
│   ├── __init__.py
│   ├── config.py            # Configuration management
│   ├── i18n.py              # Internationalization
│   ├── logging.py           # Logging setup
│   └── threading.py         # Thread-safe async helpers
└── resources/               # UI files, icons, etc.
    ├── lubus.ui             # Main UI template
    ├── icons/
    └── styles.css
```

## Requirements

### Runtime Dependencies
- `python3` (3.10+)
- `python3-gobject` (PyGObject)
- `gtk4` (4.10+)
- `libadwaita-1` (1.3+)
- `udisks2` (for disk operations via D-Bus)
- `dosfstools` (mkfs.vfat for FAT32)
- `ntfs-3g` (mkfs.ntfs for NTFS)
- `exfatprogs` (mkfs.exfat for exFAT)
- `e2fsprogs` (mkfs.ext4 for ext4)
- `btrfs-progs` (mkfs.btrfs for btrfs)
- `wimlib` (for Windows image handling)
- `grub2` (grub-mkimage, grub-bios-setup for MBR/BIOS boot)
- `polkit` (for privilege escalation via udisks)

### Build Dependencies (for packaging)
- `meson` (0.60+)
- `ninja`
- `gettext` (for translations)
- `appstream-util` (for metadata validation)
- `desktop-file-validate`

## Installation

### From Source (Development)
```bash
git clone https://github.com/yourusername/lubus.git
cd lubus
pip install -e .
lubus
```

### Flatpak (Recommended)
```bash
flatpak install flathub com.github.yourusername.lubus
```

### Arch Linux (AUR)
```bash
yay -S lubus-git
```

### Fedora
```bash
dnf install lubus
```

### Ubuntu/Debian
```bash
sudo apt install lubus
```

## Usage

1. **Launch Lubus** from your application menu or terminal
2. **Select Target Drive** (Step 1) - Choose your USB drive/SD card
3. **Select Image** (Step 2) - Browse and select your ISO/IMG file
4. **Configure Options** (Step 3) - Choose partition scheme, filesystem, label, etc.
5. **Review Summary** (Step 4) - Confirm all settings
6. **Flash** (Step 5) - Click "Start Flashing" and authenticate via polkit

## Safety Features

- **Internal Drive Protection**: Automatically filters out internal system drives (NVMe, SATA SSD/HDD)
- **Double Confirmation**: Two warning dialogs before any destructive operation
- **Drive Identity Verification**: Checks drive serial/WWN before writing to prevent target swap
- **Polkit Authentication**: All privileged operations go through udisks2 + polkit (no root process)
- **Safe Unmounting**: Properly unmounts all partitions before and after operations
- **Verification**: Optional byte-for-byte verification after write

## Development

### Running Tests
```bash
python -m pytest tests/
```

### Building with Meson
```bash
meson setup builddir
meson compile -C builddir
meson install -C builddir
```

### Code Style
```bash
ruff check lubus/
ruff format lubus/
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests and linting
5. Submit a pull request

## License

GPL-3.0-or-later - See [LICENSE](LICENSE) for details.

## Acknowledgments

- Inspired by [Rufus](https://rufus.ie/) - The gold standard for bootable USB creation
- Built on the shoulders of [Lufux](https://github.com/Advnirr/lufux) - GTK4/Libadwaita reference implementation
- Uses [UDisks2](https://www.freedesktop.org/wiki/Software/udisks/) for safe, policykit-integrated disk operations
- Uses [wimlib](https://wimlib.net/) for Windows image handling

## Screenshots

*Coming soon...*