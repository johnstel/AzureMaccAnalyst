"""Centralised logging configuration for Azure MACC Analyst.

All modules should import their logger from here:

    from src.logging_config import get_logger
    logger = get_logger(__name__)

Logs are written to a ``logs/`` directory next to the running application.
When running inside a PyInstaller bundle the logs directory is created beside
the executable, *not* inside the temp extraction folder.

**Privacy rules:**
 - Bearer tokens are redacted to ``Bearer <REDACTED>``
 - Subscription IDs are shortened to first 8 characters + "…"
 - No raw API response bodies containing customer cost data are logged
 - File paths are logged (they help troubleshooting) but no file *content*
"""
from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Log directory resolution ───────────────────────────────────────────────────
# When frozen (PyInstaller) put logs next to the .exe; otherwise next to app.py.

def _log_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        # Find finops_app/ root: two levels up from src/logging_config.py
        base = Path(__file__).resolve().parent.parent
    log_path = base / "logs"
    log_path.mkdir(parents=True, exist_ok=True)
    return log_path


LOG_DIR = _log_dir()

# ── Redaction filter ───────────────────────────────────────────────────────────

_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
_TOKEN_RE = re.compile(r"(token['\"]?\s*[:=]\s*['\"]?)\S{20,}", re.IGNORECASE)
# Match full subscription GUIDs and keep only 8 chars
_SUB_RE = re.compile(r"([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


class _RedactingFilter(logging.Filter):
    """Strip sensitive data before it hits the log file."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._scrub(v) if isinstance(v, str) else v for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._scrub(a) if isinstance(a, str) else a for a in record.args)
        return True

    @staticmethod
    def _scrub(text: str) -> str:
        text = _BEARER_RE.sub(r"\1<REDACTED>", text)
        text = _TOKEN_RE.sub(r"\1<REDACTED>", text)
        text = _SUB_RE.sub(r"\1…", text)
        return text


# ── Formatter ──────────────────────────────────────────────────────────────────

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ── Logger factory ─────────────────────────────────────────────────────────────

_configured = False


def _configure_root() -> None:
    """One-time setup of the root ``macc`` logger hierarchy."""
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger("macc")
    root.setLevel(logging.DEBUG)

    # Rotating file handler — 5 MB × 5 backups = 25 MB cap
    fh = RotatingFileHandler(
        filename=str(LOG_DIR / "macc_analyst.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    fh.addFilter(_RedactingFilter())
    root.addHandler(fh)

    # Also emit WARNING+ to stderr (visible in the console window)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    sh.addFilter(_RedactingFilter())
    root.addHandler(sh)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``macc`` namespace.

    Usage::

        from src.logging_config import get_logger
        logger = get_logger(__name__)
    """
    _configure_root()
    # Map module names like "src.azure_clients" → "macc.azure_clients"
    short = name.replace("src.", "").replace("__main__", "app")
    return logging.getLogger(f"macc.{short}")
