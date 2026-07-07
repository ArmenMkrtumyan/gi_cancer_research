"""Standard-library logging setup shared by every service.

Adapted from the pricingai_ml `pep/utils/logging.py` pattern: colored console output
by level + the same log format + a `get_splitter` banner helper. Differences here:
no `click` dependency (plain ANSI, auto-disabled when not a TTY), and a simple optional
file path instead of the execution-id paths that project used.

Usage:
    from logging_setup import configure_logging, get_splitter
    configure_logging()                 # once, at program start (an entry script's __main__)
    import logging
    logger = logging.getLogger(__name__)  # in every module
"""

import logging
import os
import sys

# ANSI colors per level (same scheme as pricingai_ml).
_COLORS = {
    "DEBUG": "\033[35m",     # magenta
    "INFO": "\033[36m",      # cyan
    "WARNING": "\033[33m",   # yellow
    "ERROR": "\033[31m",     # red
    "CRITICAL": "\033[31m",  # red
}
_RESET = "\033[0m"

_FORMAT = "[%(asctime)s][%(levelname)s][%(filename)s][%(module)s.%(funcName)s:%(lineno)d] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class ColorFormatter(logging.Formatter):
    """Log formatter that wraps each line in an ANSI color by level (plain when disabled)."""

    def __init__(self, use_color=True, **kwargs):
        """Build the formatter.

        Args:
            use_color: Whether to apply ANSI colors (turn off when not a terminal).
            **kwargs: Passed through to logging.Formatter (fmt, datefmt, ...).
        """
        super().__init__(**kwargs)
        self.use_color = use_color

    def format(self, record):
        """Format one log record.

        Args:
            record: The logging.LogRecord to render.

        Returns:
            The formatted line, colored by level when colors are enabled.
        """
        text = super().format(record)
        color = self.use_color and _COLORS.get(record.levelname)
        return f"{color}{text}{_RESET}" if color else text


def configure_logging(level=None, log_file=None):
    """Configure root logging (colored console + optional file). Idempotent.

    Args:
        level: Log level; defaults to $LOG_LEVEL or "INFO".
        log_file: If given, also write uncolored logs to this path (dirs auto-created).

    Returns:
        The configured root logger.
    """
    level = level or os.environ.get("LOG_LEVEL", "INFO")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):  # avoid duplicate handlers on repeat calls
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ColorFormatter(use_color=sys.stdout.isatty(), fmt=_FORMAT, datefmt=_DATEFMT))
    root.addHandler(console)

    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        fh = logging.FileHandler(log_file, mode="w", encoding="utf8")
        fh.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        root.addHandler(fh)

    # Quiet noisy third-party loggers (boto3 chatter during slide uploads, etc.)
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return root


def get_splitter(message: str) -> str:
    """Banner line for section headers, e.g. '=========== Step 1 ==========='."""
    return f"{'=' * 31} {message} {'=' * 31}"
