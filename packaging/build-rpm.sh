#!/bin/bash
# Build RPM package for Yaku
# Run this script on Fedora/RHEL/CentOS/OpenSUSE to create the RPM
#
# Options:
#   --mock       Build using mock (clean chroot, requires mock installed)
#   --local      Build locally (requires build dependencies installed)
#   --srpm       Only build source RPM

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${PROJECT_DIR}/rpmbuild"
SPEC_FILE="${PROJECT_DIR}/packaging/yaku.spec"
VERSION="6.1.0"

BUILD_MODE="auto"
BUILD_SRPM_ONLY=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --mock) BUILD_MODE="mock"; shift ;;
        --local) BUILD_MODE="local"; shift ;;
        --srpm) BUILD_SRPM_ONLY=true; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "=== Yaku RPM Builder ==="
echo "Project: ${PROJECT_DIR}"
echo "Version: ${VERSION}"
echo "Mode: ${BUILD_MODE}"

# Check for rpmbuild
if ! command -v rpmbuild &> /dev/null; then
    echo "Error: 'rpmbuild' not found. Install with:"
    echo "  sudo dnf install rpm-build"
    exit 1
fi

# Create RPM build directories
mkdir -p "${BUILD_DIR}"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}

# Create source tarball
echo "Creating source tarball..."
cd "${PROJECT_DIR}"
git archive --format=tar.gz --prefix="yaku-${VERSION}/" -o "${BUILD_DIR}/SOURCES/yaku-${VERSION}.tar.gz" HEAD 2>/dev/null || {
    # Fallback if not a git repo
    tar -czf "${BUILD_DIR}/SOURCES/yaku-${VERSION}.tar.gz" \
        --exclude='.git' --exclude='builddir' --exclude='rpmbuild' \
        --exclude='debbuild' --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='*.deb' --exclude='*.rpm' \
        --transform="s|^|yaku-${VERSION}/|" .
}

# Copy spec file
cp "${SPEC_FILE}" "${BUILD_DIR}/SPECS/"

# Build SRPM first
echo "Building Source RPM..."
rpmbuild --define "_topdir ${BUILD_DIR}" \
         --define "version ${VERSION}" \
         -bs "${BUILD_DIR}/SPECS/yaku.spec"

SRPM_FILE=$(find "${BUILD_DIR}/SRPMS" -name "*.src.rpm" | head -1)
echo "SRPM created: ${SRPM_FILE}"

if [[ "${BUILD_SRPM_ONLY}" == "true" ]]; then
    echo "Done (SRPM only)."
    exit 0
fi

# Determine build mode
if [[ "${BUILD_MODE}" == "auto" ]]; then
    if command -v mock &> /dev/null; then
        BUILD_MODE="mock"
    else
        BUILD_MODE="local"
    fi
fi

if [[ "${BUILD_MODE}" == "mock" ]]; then
    echo "Building with mock (clean chroot)..."
    # Find mock config
    MOCK_CONFIG="fedora-$(rpm -E %fedora)-x86_64"
    if [[ ! -f "/etc/mock/${MOCK_CONFIG}.cfg" ]]; then
        # Try to find any fedora config
        MOCK_CONFIG=$(ls /etc/mock/fedora-*-x86_64.cfg 2>/dev/null | head -1 | xargs basename | sed 's/\.cfg$//')
    fi
    
    if [[ -z "${MOCK_CONFIG}" ]]; then
        echo "Error: No mock config found. Install mock and run 'mock --init' first."
        exit 1
    fi
    
    echo "Using mock config: ${MOCK_CONFIG}"
    mock -r "${MOCK_CONFIG}" --resultdir="${BUILD_DIR}/RPMS" "${SRPM_FILE}"
    
elif [[ "${BUILD_MODE}" == "local" ]]; then
    echo "Building locally (requires build dependencies installed)..."
    
    # Check for required build dependencies
    MISSING_DEPS=()
    for pkg in gtk4-devel libadwaita-devel glib2-devel; do
        if ! rpm -q "${pkg}" &> /dev/null; then
            MISSING_DEPS+=("${pkg}")
        fi
    done
    
    if [[ ${#MISSING_DEPS[@]} -gt 0 ]]; then
        echo ""
        echo "Missing build dependencies: ${MISSING_DEPS[*]}"
        echo "Install them with:"
        echo "  sudo dnf install ${MISSING_DEPS[*]}"
        echo ""
        echo "Or build with mock instead:"
        echo "  ./packaging/build-rpm.sh --mock"
        echo ""
        read -p "Continue anyway? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
    
    # Build binary RPM
    rpmbuild --define "_topdir ${BUILD_DIR}" \
             --define "version ${VERSION}" \
             -bb "${BUILD_DIR}/SPECS/yaku.spec"
else
    echo "Unknown build mode: ${BUILD_MODE}"
    exit 1
fi

# Find and show the built RPM
RPM_FILES=$(find "${BUILD_DIR}/RPMS" -name "*.rpm" | grep -v src.rpm || true)
if [[ -n "${RPM_FILES}" ]]; then
    echo ""
    echo "=== Build Successful! ==="
    echo "${RPM_FILES}"
    echo ""
    echo "To install:"
    echo "  sudo dnf install ${RPM_FILES}"
    echo ""
    echo "To copy to Downloads:"
    echo "  cp ${RPM_FILES} ~/Downloads/"
else
    echo "Error: No binary RPM found in ${BUILD_DIR}/RPMS"
    exit 1
fi