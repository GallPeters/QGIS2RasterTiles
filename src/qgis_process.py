"""Run the built-in algorithm in qgis_process at normal priority.

Used by both tile generation methods: one process ("Built-in tool") or many
("Parallel workers", see :mod:`job_runner`). Every process starts at NORMAL
priority on Windows (no window); on Linux the priority is left unchanged.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000
NORMAL_PRIORITY_CLASS = 0x00000020


def popen_options() -> Dict[str, Any]:
    """subprocess options: normal priority, no console window (Windows)."""
    return {"creationflags": CREATE_NO_WINDOW | NORMAL_PRIORITY_CLASS} if IS_WINDOWS else {}


def find_qgis_process(prefix_path: str) -> str:
    """The qgis_process executable of the running QGIS (same build as the GUI)."""
    prefix = Path(prefix_path)
    candidates = [prefix / "bin" / ("qgis_process.exe" if IS_WINDOWS else "qgis_process")]
    if IS_WINDOWS:  # OSGeo4W / standalone launchers: <root>/bin/qgis_process-<build>.bat
        candidates.append(prefix.parent.parent / "bin" / f"qgis_process-{prefix.name}.bat")
    for c in candidates:
        if c.is_file():
            return str(c)
    raise FileNotFoundError("qgis_process was not found next to this QGIS. Tried: "
                            + ", ".join(str(c) for c in candidates))


def profile_arguments(settings_dir: str) -> List[str]:
    """--profile/--profiles-path of the running QGIS user profile (same global settings)."""
    p = Path(settings_dir.rstrip("/\\"))
    if p.parent.name.lower() == "profiles" and p.name:
        return ["--profile", p.name, "--profiles-path", str(p.parent.parent)]
    return []


def last_percent(text: str) -> Optional[int]:
    """Latest progress value in qgis_process output ('0...10...20...')."""
    found = re.findall(r"(\d{1,3})\.\.\.", text)
    return int(found[-1]) if found else None


def kill(proc: subprocess.Popen) -> None:
    """Kill a process and its children."""
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
    try:
        proc.kill()
    except OSError:
        pass
    proc.wait()


def _set_cpu_limit(proc: subprocess.Popen, cpu_percent: int) -> None:
    """Restrict the process to the first N logical processors (N = cpu_count x cpu_percent)."""
    cpus = os.cpu_count() or 1
    n = max(1, int(cpus * cpu_percent / 100))
    if n >= cpus:
        return
    try:
        if IS_WINDOWS:
            import ctypes
            ctypes.windll.kernel32.SetProcessAffinityMask(int(proc._handle), (1 << n) - 1)
        elif hasattr(os, "sched_setaffinity"):
            os.sched_setaffinity(proc.pid, set(range(n)))
    except (OSError, AttributeError, ValueError):
        pass


def run(args: List[str], log_path: str, feedback, cpu_percent: int = 100) -> None:
    """Run one qgis_process (built-in method); progress and cancel via ``feedback``.

    Output goes to ``log_path``. Raises RuntimeError on failure or cancel.
    """
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        log.write("command: " + " ".join(args) + "\n\n")
        log.flush()
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                **popen_options())
        _set_cpu_limit(proc, cpu_percent)
        while proc.poll() is None:
            if feedback.isCanceled():
                kill(proc)
                raise RuntimeError("canceled")
            time.sleep(1)
            try:
                with open(log_path, encoding="utf-8", errors="replace") as fh:
                    pct = last_percent(fh.read()[-4000:])
                if pct is not None:
                    feedback.setProgress(pct)
            except OSError:
                pass
    if proc.returncode != 0:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-1500:]
        raise RuntimeError(f"qgis_process failed (exit code {proc.returncode}). Log: {log_path}\n{tail}")
