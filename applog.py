"""
applog.py — one place that sets up logging for every scoop-scouter script.
═══════════════════════════════════════════════════════════════════════════════
Why this exists
---------------
The crawlers print a lot of friendly output to the screen. That's great to watch
live, but the moment you look away it's gone — and when something crashes there's
no record of *why*. This module fixes that without rewriting all the existing
print() calls:

  • Every run is mirrored to a file in  logs/run-YYYYMMDD-HHMMSS.log
    (we "tee" stdout/stderr — i.e. write to the screen AND the file at once).
  • A real `logging` logger is available for structured messages and, crucially,
    for FULL error tracebacks (log.exception / log.error(..., exc_info=True)).
  • Any uncaught crash is written to the log with its complete traceback, via a
    global "excepthook" — so even a surprise KeyError leaves a paper trail.
  • Old run-logs are pruned so the folder doesn't grow forever.

How to use it
-------------
At the very start of a program (e.g. in main()):

    import applog
    log = applog.install("monitor")      # call ONCE per process

Then anywhere:

    log.debug("fetched %d listings", n)  # detail — file only
    log.info("watch loop started")       # normal milestone
    log.warning("FB returned 0 results") # something looks off but we continue
    try:
        risky()
    except Exception:
        log.exception("scan failed")     # ERROR + full traceback to the log

Levels in one line: DEBUG < INFO < WARNING < ERROR < CRITICAL. You pick a
threshold per destination; messages below it are ignored there.
"""

import logging
import logging.handlers
import os
import sys
from datetime import datetime

LOG_DIR = "logs"
KEEP_RUNS = 30          # how many run-*.log files to keep before deleting the oldest
LOGGER_NAME = "scoop"

_installed = False
_run_file = None        # path of the current run's log file (for messages)


class _Tee:
    """A stand-in for sys.stdout / sys.stderr that forwards everything to the real
    stream (so you still see it on screen) AND to a file (so it's saved). This is
    what lets us capture every existing print() with zero changes to that code."""

    def __init__(self, real_stream, file_handle):
        self._real = real_stream
        self._file = file_handle

    def write(self, data):
        try:
            self._real.write(data)
        except Exception:
            pass
        try:
            self._file.write(data)
            self._file.flush()
        except Exception:
            pass

    def flush(self):
        for s in (self._real, self._file):
            try:
                s.flush()
            except Exception:
                pass

    # Libraries sometimes probe these on the stream object — delegate to the real one.
    def isatty(self):
        return getattr(self._real, "isatty", lambda: False)()

    def fileno(self):
        return self._real.fileno()

    def __getattr__(self, name):
        # Anything we don't implement explicitly (e.g. reconfigure(), encoding,
        # buffer) is delegated to the real stream, so code that probes/configures
        # sys.stdout after we've wrapped it doesn't crash. `_real`/`_file` live in
        # __dict__, so this only fires for genuinely missing attributes.
        return getattr(self._real, name)


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    """Fetch the shared logger (use after install())."""
    return logging.getLogger(name)


def _prune_old_runs():
    """Keep only the newest KEEP_RUNS run-*.log files; delete the rest."""
    try:
        runs = sorted(
            (os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR)
             if f.startswith("run-") and f.endswith(".log")),
            key=os.path.getmtime,
        )
        for old in runs[:-KEEP_RUNS]:
            try:
                os.remove(old)
            except OSError:
                pass
    except FileNotFoundError:
        pass


def install(name: str = LOGGER_NAME, *,
            console_level: int = logging.INFO,
            file_level: int = logging.DEBUG) -> logging.Logger:
    """Set up logging for this process. Idempotent — safe to call more than once;
    only the first call does anything. Returns the shared logger.

    console_level: minimum level shown on screen (INFO by default).
    file_level:    minimum level written to the log file (DEBUG = capture everything).
    """
    global _installed, _run_file
    logger = logging.getLogger(name)
    if _installed:
        return logger

    os.makedirs(LOG_DIR, exist_ok=True)
    _prune_old_runs()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    _run_file = os.path.join(LOG_DIR, f"run-{ts}.log")
    # Line-buffered, utf-8 so Greek text is stored correctly regardless of the
    # Windows console code page.
    fh = open(_run_file, "a", encoding="utf-8", buffering=1)

    # Make sure the console itself is utf-8 (older Windows consoles default to
    # cp1252 and choke on Greek/accented characters).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    # 1) Mirror everything printed to the screen into the run file.
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)

    # 2) Send structured logging records (with timestamps + level + line number)
    #    into the SAME file, so prints and logs sit together in chronological order.
    logger.setLevel(min(console_level, file_level))
    logger.propagate = False
    if not logger.handlers:
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s:%(lineno)d  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler = logging.StreamHandler(fh)
        file_handler.setLevel(file_level)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    # 3) Catch any crash that isn't handled anywhere else and log it with a full
    #    traceback before the program dies (KeyboardInterrupt is left alone so
    #    Ctrl+C stays clean).
    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("UNCAUGHT EXCEPTION — crashing",
                        exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _excepthook

    _installed = True
    logger.info("=" * 70)
    logger.info("logging started → %s", os.path.abspath(_run_file))
    return logger


def run_file() -> str | None:
    """Path of the current run's log file (None before install())."""
    return _run_file
