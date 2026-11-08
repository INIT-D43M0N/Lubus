#!/bin/bash
# Build DEB package for Lubus
# Run this script on Debian/Ubuntu to create the .deb

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${PROJECT_DIR}/debbuild"
VERSION="5.0.0"
PACKAGE_NAME="lubus"

echo "=== Lubus DEB Builder ==="
echo "Project: ${PROJECT_DIR}"
echo "Version: ${VERSION}"

# Check for required tools
for cmd in dpkg-deb meson ninja; do
    if ! command -v "$cmd" &> /dev/null; then
        echo "Error: '$cmd' not found. Install with:"
        echo "  sudo apt install dpkg-dev meson ninja-build"
        exit 1
    fi
done

# Create build directory
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}/${PACKAGE_NAME}-${VERSION}"

# Copy source files
echo "Copying source files..."
cd "${PROJECT_DIR}"
rsync -a --exclude='.git' --exclude='builddir' --exclude='debbuild' \
      --exclude='rpmbuild' --exclude='__pycache__' --exclude='*.pyc' \
      --exclude='*.deb' --exclude='*.rpm' \
      . "${BUILD_DIR}/${PACKAGE_NAME}-${VERSION}/"

# Create debian directory
mkdir -p "${BUILD_DIR}/${PACKAGE_NAME}-${VERSION}/debian"

# Create control file
cat > "${BUILD_DIR}/${PACKAGE_NAME}-${VERSION}/debian/control" << EOF
Source: ${PACKAGE_NAME}
Section: utils
Priority: optional
Maintainer: Lubus Contributors <lubus@github.com>
Build-Depends: debhelper (>= 13), python3, python3-setuptools, meson (>= 1.0), ninja-build, pkg-config, libgtk-4-dev, libadwaita-1-dev, libgirepository1.0-dev, gettext, desktop-file-utils, libappstream-glib
Standards-Version: 4.6.2
Homepage: https://github.com/yourusername/lubus
Vcs-Git: https://github.com/yourusername/lubus.git
Vcs-Browser: https://github.com/yourusername/lubus

Package: ${PACKAGE_NAME}
Architecture: all
Depends: \${misc:Depends}, \${python3:Depends}, python3-gobject, gir1.2-gtk-4.0, gir1.2-adw-1, udisks2, wimlib, grub-common, grub-pc-bin, dosfstools, ntfs-3g, exfatprogs, e2fsprogs, btrfs-progs, hicolor-icon-theme
Description: Create bootable USB drives on Linux (Rufus alternative)
 Lubus is a modern, native Linux application for creating bootable USB drives,
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
  * Windows installer media creation
  * Windows To Go support
  * Safety checks and confirmation dialogs
EOF

# Create rules file
cat > "${BUILD_DIR}/${PACKAGE_NAME}-${VERSION}/debian/rules" << 'EOF'
#!/usr/bin/make -f
%:
	dh $@ --buildsystem=meson

override_dh_auto_install:
	dh_auto_install --destdir=debian/lubus
	# Install desktop file
	install -Dm644 data/com.github.yourusername.lubus.desktop \
		debian/lubus/usr/share/applications/com.github.yourusername.lubus.desktop
	# Install metainfo
	install -Dm644 data/com.github.yourusername.lubus.metainfo.xml \
		debian/lubus/usr/share/metainfo/com.github.yourusername.lubus.metainfo.xml
	# Install icon
	install -Dm644 data/icons/hicolor/scalable/apps/lubus.svg \
		debian/lubus/usr/share/icons/hicolor/scalable/apps/com.github.yourusername.lubus.svg

override_dh_auto_test:
	desktop-file-validate debian/lubus/usr/share/applications/com.github.yourusername.lubus.desktop
	appstream-util validate-relax --nonet debian/lubus/usr/share/metainfo/com.github.yourusername.lubus.metainfo.xml
EOF

chmod +x "${BUILD_DIR}/${PACKAGE_NAME}-${VERSION}/debian/rules"

# Create compat file
echo "13" > "${BUILD_DIR}/${PACKAGE_NAME}-${VERSION}/debian/compat"

# Create changelog
cat > "${BUILD_DIR}/${PACKAGE_NAME}-${VERSION}/debian/changelog" << EOF
${PACKAGE_NAME} (${VERSION}-1) unstable; urgency=medium

  * Initial release

 -- Lubus Contributors <lubus@github.com>  $(date -R)
EOF

# Create copyright
cat > "${BUILD_DIR}/${PACKAGE_NAME}-${VERSION}/debian/copyright" << EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: lubus
Upstream-Contact: Lubus Contributors <lubus@github.com>
Source: https://github.com/yourusername/lubus

Files: *
Copyright: 2026 Lubus Contributors
License: GPL-3.0-or-later

License: GPL-3.0-or-later
 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.
 .
 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.
 .
 You should have received a copy of the GNU General Public License
 along with this program.  If not, see <https://www.gnu.org/licenses/>.
 .
 On Debian systems, the complete text of the GNU General Public License
 version 3 can be found in "/usr/share/common-licenses/GPL-3".
EOF

# Create install file
cat > "${BUILD_DIR}/${PACKAGE_NAME}-${VERSION}/debian/lubus.install" << EOF
usr/bin/lubus
usr/share/applications/com.github.yourusername.lubus.desktop
usr/share/metainfo/com.github.yourusername.lubus.metainfo.xml
usr/share/icons/hicolor/scalable/apps/com.github.yourusername.lubus.svg
usr/lib/python3/dist-packages/lubus/
EOF

# Build the package
echo "Building DEB package..."
cd "${BUILD_DIR}/${PACKAGE_NAME}-${VERSION}"
dpkg-buildpackage -us -uc -b

# Find and show the built DEB
DEB_FILE=$(find "${BUILD_DIR}" -maxdepth 1 -name "*.deb" | head -1)
if [[ -n "${DEB_FILE}" ]]; then
    echo ""
    echo "=== Build Successful! ==="
    echo "DEB created: ${DEB_FILE}"
    echo ""
    echo "To install:"
    echo "  sudo apt install ./${DEB_FILE}"
    echo ""
    echo "To copy to Downloads:"
    echo "  cp ${DEB_FILE} ~/Downloads/"
else
    echo "Error: DEB not found in ${BUILD_DIR}"
    exit 1
fi