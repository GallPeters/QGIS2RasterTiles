"""System-wide CPU utilisation and free RAM, without third-party packages.

Used by the job scheduler to keep the machine busy: it starts another
rendering process while the CPU is below the target and enough RAM is free.
"""

from __future__ import annotations

import os
import sys
from typing import Optional, Tuple


def available_memory_gb() -> Optional[float]:
    """Available physical memory in GB, or None if it cannot be determined."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return stat.ullAvailPhys / 1024 ** 3
            return None
        if os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / 1024 ** 2
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024 ** 3
    except (OSError, ValueError, AttributeError):
        return None


def _cpu_times() -> Optional[Tuple[int, int]]:
    """(busy, total) CPU time counters since boot, or None if unavailable."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                                         ctypes.byref(user)):
                return None

            def v(ft) -> int:
                return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

            total = v(kernel) + v(user)  # kernel time includes idle time
            return total - v(idle), total
        if os.path.exists("/proc/stat"):
            with open("/proc/stat") as fh:
                vals = [int(x) for x in fh.readline().split()[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            total = sum(vals[:8])
            return total - idle, total
    except (OSError, ValueError, AttributeError):
        return None
    return None


class CpuMonitor:
    """Average system-wide CPU utilisation (0-100) between consecutive calls to :meth:`sample`."""

    def __init__(self) -> None:
        self._last = _cpu_times()

    @property
    def available(self) -> bool:
        return self._last is not None

    def sample(self) -> Optional[float]:
        now = _cpu_times()
        if now is None or self._last is None:
            return None
        busy = now[0] - self._last[0]
        total = now[1] - self._last[1]
        self._last = now
        if total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * busy / total))

    @staticmethod
    def free_ram_gb() -> Optional[float]:
        return available_memory_gb()
