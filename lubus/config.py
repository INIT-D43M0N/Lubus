"""Configuration management for Lubus."""
import json
import os
from pathlib import Path

# Flatpak points XDG_CONFIG_HOME into the sandbox
CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
) / "lubus"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "theme": 0,           # 0=system, 1=light, 2=dark
    "lang": "",           # Empty = system default
    "verify": True,       # Verify after write
    "last_image_dir": "", # Last directory used for image selection
    "last_drive": "",     # Last selected drive (for convenience)
    "iso_download_dir": str(Path.home() / "Downloads" / "Lubus ISOs"),
    "iso_catalog_url": "",  # Empty = local catalog only
}

def load_config():
    """Load configuration from disk, falling back to defaults."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULT_CONFIG)

    cfg = dict(DEFAULT_CONFIG)
    if not isinstance(data, dict):
        return cfg

    # Type-check each value
    if isinstance(data.get("theme"), int) and data["theme"] in (0, 1, 2):
        cfg["theme"] = int(data["theme"])
    if isinstance(data.get("lang"), str):
        cfg["lang"] = data["lang"]
    if isinstance(data.get("verify"), bool):
        cfg["verify"] = data["verify"]
    if isinstance(data.get("last_image_dir"), str):
        cfg["last_image_dir"] = data["last_image_dir"]
    if isinstance(data.get("last_drive"), str):
        cfg["last_drive"] = data["last_drive"]
    if isinstance(data.get("iso_download_dir"), str) and data["iso_download_dir"]:
        cfg["iso_download_dir"] = data["iso_download_dir"]
    if isinstance(data.get("iso_catalog_url"), str):
        cfg["iso_catalog_url"] = data["iso_catalog_url"]

    return cfg


def save_config(cfg):
    """Save configuration to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# Global config instance
APP_CONFIG = load_config()