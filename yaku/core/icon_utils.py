"""Icon conversion utilities for Yaku."""
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from ..utils.logging import get_logger

log = get_logger(__name__)


def find_windowschan_png() -> Optional[Path]:
    """Find windowschan.png in the user's Downloads folder."""
    downloads = Path.home() / "Downloads"
    png_path = downloads / "windowschan.png"
    if png_path.exists():
        return png_path
    return None


def png_to_ico(png_path: Path, ico_path: Path, sizes: tuple = (16, 32, 48, 64, 128, 256)) -> bool:
    """
    Convert a PNG file to ICO format with multiple sizes.
    
    Args:
        png_path: Path to the source PNG file
        ico_path: Path where the ICO file should be saved
        sizes: Tuple of sizes to include in the ICO file
        
    Returns:
        True if conversion succeeded, False otherwise
    """
    try:
        # Try using ImageMagick first (most reliable)
        cmd = ["convert", str(png_path)]
        for size in sizes:
            cmd.extend(["-resize", f"{size}x{size}"])
        cmd.append(str(ico_path))
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and ico_path.exists():
            log.info(f"Successfully converted {png_path} to {ico_path} using ImageMagick")
            return True
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    
    # Try using Python PIL/Pillow as fallback
    try:
        from PIL import Image
        img = Image.open(png_path)
        images = []
        for size in sizes:
            resized = img.resize((size, size), Image.Resampling.LANCZOS)
            images.append(resized)
        images[0].save(ico_path, format='ICO', sizes=[(s, s) for s in sizes])
        if ico_path.exists():
            log.info(f"Successfully converted {png_path} to {ico_path} using Pillow")
            return True
    except ImportError:
        pass
    except Exception as e:
        log.warning(f"Pillow conversion failed: {e}")
    
    log.error("Failed to convert PNG to ICO: neither ImageMagick nor Pillow available")
    return False


def get_or_create_windows_icon() -> Optional[Path]:
    """
    Find windowschan.png in Downloads and convert to ICO if needed.
    Returns the path to the ICO file, or None if not available.
    """
    png_path = find_windowschan_png()
    if not png_path:
        log.info("No windowschan.png found in Downloads folder")
        return None
    
    # Create ICO in the same directory as the PNG
    ico_path = png_path.with_suffix(".ico")
    
    # If ICO already exists and is newer than PNG, use it
    if ico_path.exists() and ico_path.stat().st_mtime >= png_path.stat().st_mtime:
        log.info(f"Using existing ICO: {ico_path}")
        return ico_path
    
    # Convert PNG to ICO
    if png_to_ico(png_path, ico_path):
        return ico_path
    
    return None