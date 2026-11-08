"""Linux ISO catalog, search and dynamic mirror resolution.

The catalog is a local JSON file (installed with Lubus) with an optional
remote copy.  Each edition describes either a direct URL or a mirror
directory + checksum file + filename pattern.  The resolver downloads the
official checksum list / directory listing, selects the current filename,
and returns the direct URL together with its SHA-256.
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from lubus.config import APP_CONFIG
from lubus.utils.logging import get_logger
from lubus.utils.resources import data_dir

log = get_logger(__name__)

USER_AGENT = "Lubus/5.0 (+https://github.com/yourusername/lubus)"
_TEXT_CACHE = {}
_SHA_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")
_BSD_SHA_RE = re.compile(r"^SHA256\s*\((.+)\)\s*=\s*([0-9a-fA-F]{64})\s*$")
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)


class CatalogError(Exception):
    pass


@dataclass
class ResolvedIso:
    distro: str
    branch: str
    edition: str
    filename: str
    url: str
    sha256: str
    checksum_url: str = ""
    arch: str = "x86_64"
    desktop: str = ""
    status: str = ""
    firmware: str = "free"
    notes: str = ""
    size: int = 0


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "lubus"
    return Path.home() / ".cache" / "lubus"


def fetch_text(url: str, timeout: int = 25, max_bytes: int = 8 * 1024**2) -> str:
    """Fetch a small text resource with a bounded size and in-memory cache."""
    if url in _TEXT_CACHE:
        return _TEXT_CACHE[url]
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise CatalogError(f"Resource is too large: {url}")
    text = data.decode("utf-8", errors="replace")
    _TEXT_CACHE[url] = text
    return text


def _parse_checksums(text: str, pattern: Optional[str], filename_hint: str = ""):
    """Return (filename, sha256) from SHA256SUMS / CHECKSUM / raw hash text."""
    candidates = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _SHA_RE.match(line)
        if m:
            candidates.append((m.group(2).strip(), m.group(1).lower()))
            continue
        m = _BSD_SHA_RE.match(line)
        if m:
            candidates.append((m.group(1).strip(), m.group(2).lower()))
            continue
    if pattern:
        rx = re.compile(pattern)
        for name, digest in candidates:
            if rx.search(name):
                return name, digest
        # Some mirrors use different capitalization or prefixes; try a
        # relaxed filename check before failing.
        for name, digest in candidates:
            if filename_hint and filename_hint == name:
                return name, digest
        available = ", ".join(n for n, _ in candidates[:5])
        raise CatalogError(
            f"No checksum entry matched {pattern!r}. Available: {available}"
        )
    if filename_hint:
        for name, digest in candidates:
            if name == filename_hint:
                return name, digest
    if candidates:
        return candidates[0]
    raw = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if raw:
        return filename_hint, raw.group(1).lower()
    raise CatalogError("No SHA-256 checksum found in the checksum file")


def _guess_base_url(checksum_url: str) -> str:
    return checksum_url.rsplit("/", 1)[0] + "/"


def _resolve_from_listing(entry: dict) -> tuple[str, str]:
    listing_url = entry["listing_url"]
    pattern = entry.get("filename_pattern", r"\.(iso|img)$")
    html = fetch_text(listing_url)
    rx = re.compile(pattern)
    names = []
    for href in _HREF_RE.findall(html):
        name = urllib.parse.unquote(href.split("?", 1)[0].rsplit("/", 1)[-1])
        if not name:
            continue
        if rx.search(name):
            names.append(name)
    if not names:
        raise CatalogError(f"No image matching {pattern!r} found at {listing_url}")
    names.sort()
    filename = names[-1]
    return filename, urllib.parse.urljoin(listing_url, filename)


def _resolve_hash(entry: dict, filename: str, url: str) -> str:
    if entry.get("sha256"):
        return entry["sha256"].lower()

    sha_url = entry.get("sha256_url")
    if sha_url:
        try:
            text = fetch_text(sha_url)
            digest = re.search(r"\b([0-9a-fA-F]{64})\b", text)
            if digest:
                return digest.group(1).lower()
        except Exception as exc:  # noqa: BLE001
            log.warning("Checksum URL failed (%s): %s", sha_url, exc)

    checksum_url = entry.get("checksum_url")
    if checksum_url:
        text = fetch_text(checksum_url)
        name, digest = _parse_checksums(
            text, entry.get("filename_pattern"), filename
        )
        return digest

    suffix = entry.get("checksum_suffix")
    if suffix:
        text = fetch_text(url + suffix)
        digest = re.search(r"\b([0-9a-fA-F]{64})\b", text)
        if digest:
            return digest.group(1).lower()

    raise CatalogError(
        f"No official SHA-256 checksum is available for {filename}. "
        "Refusing an unverified download."
    )


def resolve_edition(distro: dict, branch: dict, edition: dict) -> ResolvedIso:
    """Resolve a catalog edition to a direct URL and SHA-256."""
    entry = dict(edition)

    if entry.get("checksum_url") and entry.get("filename_pattern"):
        text = fetch_text(entry["checksum_url"])
        filename, digest = _parse_checksums(
            text, entry["filename_pattern"]
        )
        base = entry.get("base_url") or _guess_base_url(entry["checksum_url"])
        url = urllib.parse.urljoin(base, filename)
    elif entry.get("listing_url"):
        filename, url = _resolve_from_listing(entry)
        digest = _resolve_hash(entry, filename, url)
    elif entry.get("base_url") and entry.get("filename_pattern"):
        # A base directory with an HTML listing but no SHA256SUMS file
        # (e.g. Alpine); the checksum suffix is fetched from the image URL.
        listing = dict(entry, listing_url=entry["base_url"])
        filename, url = _resolve_from_listing(listing)
        digest = _resolve_hash(entry, filename, url)
    elif entry.get("url"):
        url = entry["url"]
        filename = os.path.basename(urllib.parse.urlparse(url).path) or "image.iso"
        digest = _resolve_hash(entry, filename, url)
    else:
        raise CatalogError("The catalog entry has no URL or mirror information")

    return ResolvedIso(
        distro=distro.get("name", distro.get("id", "")),
        branch=branch.get("name", branch.get("id", "")),
        edition=edition.get("name", edition.get("id", "")),
        filename=filename,
        url=url,
        sha256=digest,
        checksum_url=entry.get("checksum_url", ""),
        arch=edition.get("arch", "x86_64"),
        desktop=edition.get("desktop", ""),
        status=edition.get("status", branch.get("status", "")),
        firmware=edition.get("firmware", "free"),
        notes=edition.get("notes", ""),
        size=int(edition.get("size", 0) or 0),
    )


class IsoCatalog:
    """Load, search and resolve the ISO catalog."""

    def __init__(self, distros: list[dict], source: str = "local"):
        self.distros = distros
        self.source = source

    @classmethod
    def load(cls, remote_url: str = "", use_cache: bool = True,
             force: bool = False) -> "IsoCatalog":
        local_path = data_dir() / "iso_catalog.json"
        cache_path = _cache_dir() / "iso_catalog.json"

        remote = remote_url or os.environ.get("LUBUS_ISO_CATALOG_URL") or APP_CONFIG.get(
            "iso_catalog_url", ""
        )
        if remote:
            try:
                if (use_cache and not force and cache_path.is_file()
                        and time.time() - cache_path.stat().st_mtime < 86400):
                    data = json.loads(cache_path.read_text(encoding="utf-8"))
                    return cls(data.get("distros", []), source=str(cache_path))
                if force:
                    _TEXT_CACHE.pop(remote, None)
                text = fetch_text(remote, timeout=20)
                data = json.loads(text)
                if data.get("schema_version") != 1:
                    raise CatalogError("Unsupported catalog schema version")
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(text, encoding="utf-8")
                return cls(data.get("distros", []), source=remote)
            except Exception as exc:  # noqa: BLE001 - fall back to local
                log.warning("Remote ISO catalog unavailable: %s", exc)

        try:
            data = json.loads(local_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CatalogError(f"Local ISO catalog unavailable: {exc}") from exc
        return cls(data.get("distros", []), source=str(local_path))

    def search(self, query: str = "") -> list[dict]:
        query = (query or "").strip().lower()
        if not query:
            return list(self.distros)
        matches = []
        for distro in self.distros:
            haystack = [
                distro.get("name", ""),
                distro.get("category", ""),
                distro.get("description", ""),
                " ".join(distro.get("tags", [])),
            ]
            for branch in distro.get("branches", []):
                haystack.append(branch.get("name", ""))
                haystack.append(branch.get("status", ""))
                for edition in branch.get("editions", []):
                    haystack.extend([
                        edition.get("name", ""),
                        edition.get("desktop", ""),
                        edition.get("arch", ""),
                        edition.get("status", ""),
                        edition.get("firmware", ""),
                    ])
            if query in " ".join(haystack).lower():
                matches.append(distro)
        return matches

    def resolve(self, distro: dict, branch: dict, edition: dict) -> ResolvedIso:
        return resolve_edition(distro, branch, edition)
