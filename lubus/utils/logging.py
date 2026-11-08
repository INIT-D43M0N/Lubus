"""Logging configuration for Lubus."""
import logging
import sys
from pathlib import Path


def setup_logging(debug=False, log_file=None):
    """Configure application logging.

    Args:
        debug: Enable DEBUG level logging
        log_file: Optional path to log file
    """
    level = logging.DEBUG if debug else logging.INFO

    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers = [console_handler]

    # File handler if requested
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        root_logger.addHandler(file_handler)

    # Reduce noise from some libraries
    logging.getLogger("gi").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def get_logger(name):
    """Get a logger instance for a module."""
    return logging.getLogger(name)