"""Logging to the Processing feedback object and to a log file in OUTPUT_DIR.

``QgsProcessingFeedback`` must only be used from the algorithm's own thread.
Records emitted by other threads (the job pool) are queued and pushed the next
time the owning thread logs or calls :meth:`FeedbackHandler.drain`.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from typing import Optional, Tuple

LOGGER_NAME = "qgis2rastertiles"
LOG_FILE_NAME = "qgis2rastertiles.log"


class FeedbackHandler(logging.Handler):
    """Forwards log records to a QgsProcessingFeedback (thread-aware)."""

    def __init__(self, feedback) -> None:
        super().__init__(logging.INFO)
        self.feedback = feedback
        self.owner = threading.get_ident()
        self.pending: "queue.Queue[logging.LogRecord]" = queue.Queue()

    def emit(self, record: logging.LogRecord) -> None:
        if threading.get_ident() != self.owner:
            self.pending.put(record)
            return
        self.drain()
        self._push(record)

    def drain(self) -> None:
        while True:
            try:
                self._push(self.pending.get_nowait())
            except queue.Empty:
                return

    def _push(self, record: logging.LogRecord) -> None:
        if self.feedback is None:
            return
        msg = self.format(record)
        try:
            if record.levelno >= logging.ERROR:
                self.feedback.reportError(msg, False)
            elif record.levelno >= logging.WARNING:
                self.feedback.pushWarning(msg)
            else:
                self.feedback.pushInfo(msg)
        except RuntimeError:  # feedback object already deleted
            pass


def setup_logging(output_dir: str, feedback) -> Tuple[logging.Logger, FeedbackHandler, str]:
    """Configure the package logger; returns (logger, feedback handler, log path)."""
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, LOG_FILE_NAME)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s"))
    logger.addHandler(fh)
    fb = FeedbackHandler(feedback)
    fb.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(fb)
    return logger, fb, log_path


def close_logging(logger: Optional[logging.Logger] = None) -> None:
    logger = logger or logging.getLogger(LOGGER_NAME)
    for h in list(logger.handlers):
        if isinstance(h, FeedbackHandler):
            h.drain()
        logger.removeHandler(h)
        h.close()
