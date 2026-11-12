#!/bin/bash
# Build a Debian/Ubuntu .deb package for Yaku without debhelper.
#
# The app is pure Python + data files, so a staged `meson install` is enough.
# This also makes the build work on Fedora and other non-Debian hosts, as long
# as `meson`, `ninja` and `dpkg-deb` are available.
#
# Usage:
#   ./packaging/build-deb.sh [--revision N]

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${PROJECT_DIR}/debbuild"
VERSION="6.1.2"
REVISION="1"
PACKAGE_NAME="yaku"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --revision) REVISION="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

echo "=== Yaku DEB Builder ==="
echo "Project:  ${PROJECT_DIR}"
echo "Version:  ${VERSION}-${REVISION}"

for cmd in dpkg-deb meson ninja; do
    if ! command -v "$cmd" &> /dev/null; then
        echo "Error: '$cmd' not found. Install meson, ninja and dpkg-dev." >&2
        exit 1
    fi
done

STAGE="${BUILD_DIR}/stage"
MESON_BUILD="${BUILD_DIR}/build"

rm -rf "${BUILD_DIR}"
mkdir -p "${STAGE}"

echo "Building and staging files..."
meson setup "${MESON_BUILD}" "${PROJECT_DIR}" \
    --prefix=/usr --buildtype=release >/dev/null
DESTDIR="${STAGE}" meson install -C "${MESON_BUILD}" >/dev/null

# Debian's Python sys.path uses /usr/lib/python3/dist-packages. Meson on a
# Fedora host installs into site-packages instead; move it so the wrapper's
# `python3 -m yaku.main` works on Debian/Ubuntu.
PY_PKG=$(find "${STAGE}/usr/lib" -type d -name yaku -path '*python*' | head -1)
if [[ -z "${PY_PKG}" ]]; then
    echo "Error: could not find the installed Python package" >&2
    exit 1
fi
if [[ "${PY_PKG}" != */dist-packages/yaku ]]; then
    mkdir -p "${STAGE}/usr/lib/python3/dist-packages"
    mv "${PY_PKG}" "${STAGE}/usr/lib/python3/dist-packages/yaku"
    rmdir "$(dirname "${PY_PKG}")" 2>/dev/null || true
fi

# Never ship bytecode compiled by the build host's Python.
find "${STAGE}" -type d -name __pycache__ -prune -exec rm -rf {} +
find "${STAGE}" -type f -name '*.pyc' -delete

INSTALLED_SIZE=$(du -sk "${STAGE}" | cut -f1)

mkdir -p "${STAGE}/DEBIAN"
cat > "${STAGE}/DEBIAN/control" << EOF
Package: ${PACKAGE_NAME}
Version: ${VERSION}-${REVISION}
Architecture: all
Maintainer: Yaku Contributors <yaku@github.com>
Installed-Size: ${INSTALLED_SIZE}
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-adw-1, udisks2, wimtools, grub-common, grub-pc-bin, dosfstools, ntfs-3g, exfatprogs, e2fsprogs, btrfs-progs, hicolor-icon-theme
Section: utils
Priority: optional
Homepage: https://github.com/INIT-D43M0N/Yaku
Description: Create bootable USB drives on Linux (Rufus alternative)
 Yaku is a modern, native Linux application for creating bootable USB drives,
 functionally identical to Rufus but built specifically for the Linux ecosystem
 using GTK4, Libadwaita, and Python.
 .
 Features:
  * Automatic detection of removable USB drives and SD cards
  * Support for ISO, IMG, and other bootable disk images
  * Automatic detection of image type (Windows, Linux, hybrid)
  * Partition scheme selection (GPT/MBR) and target system (UEFI/BIOS)
  * Filesystem selection (FAT32, NTFS, exFAT, ext4, btrfs)
  * DD mode (block copy) and ISO extract mode
  * Real-time progress with speed and ETA
  * Optional verification after write
  * Windows installer media creation and Windows To Go support
  * Storage-controller driver staging for Intel VMD/RST and AMD RAID
  * Direct install / portable mode with unattended provisioning
  * Safety checks and confirmation dialogs
EOF

(
    cd "${STAGE}"
    find . -type f ! -path './DEBIAN/*' -printf '%P\0' \
        | sort -z | xargs -0 -r md5sum > DEBIAN/md5sums
)
chmod 0644 "${STAGE}/DEBIAN/control" "${STAGE}/DEBIAN/md5sums"

DEB_FILE="${BUILD_DIR}/${PACKAGE_NAME}_${VERSION}-${REVISION}_all.deb"
dpkg-deb --root-owner-group --build "${STAGE}" "${DEB_FILE}" >/dev/null

echo ""
echo "=== Build Successful! ==="
echo "DEB created: ${DEB_FILE}"
echo ""
echo "To install:"
echo "  sudo apt install ${DEB_FILE}"
