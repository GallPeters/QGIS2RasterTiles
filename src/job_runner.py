"""Run many qgis_process jobs in parallel (the "Parallel workers" method).

Each job is one qgis_process command line, started at the same priority as the
single built-in run (see :mod:`qgis_process`). A thread per running job only
waits on its process. With an adaptive :class:`Concurrency` the number of
processes follows the measured CPU load. Failed jobs are retried; on cancel all
process trees are killed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import qgis_process

log = logging.getLogger("qgis2rastertiles.job_runner")


class JobFailed(RuntimeError):
    pass


class JobsCanceled(RuntimeError):
    pass


@dataclass
class JobSpec:
    job_id: str
    args: List[str]      # full qgis_process command line
    output: str          # the job's GeoPackage
    workload: float = 1.0


@dataclass
class JobResult:
    job_id: str
    output: str
    seconds: float
    attempts: int


@dataclass
class Concurrency:
    """How many processes run at once.

    Adaptive: start ``initial``; every ``interval_s`` sample system CPU; below
    ``cpu_target - 8`` (and enough RAM, jobs waiting) start one more (two when far
    below), up to ``maximum``; above ``cpu_target + 3`` let the count shrink as jobs
    finish (running processes are never killed).
    """

    initial: int
    maximum: int
    adaptive: bool = False
    cpu_target: float = 95.0
    ram_per_job_gb: float = 2.0
    interval_s: float = 5.0
    monitor: Any = None  # object with sample() -> Optional[float] and free_ram_gb() -> Optional[float]


def run_one(spec: JobSpec, cancel: threading.Event, log_dir: str, retries: int = 1) -> JobResult:
    """Run one job (with retries); raises JobFailed / JobsCanceled."""
    t0 = time.monotonic()
    last = ""
    for attempt in range(1, retries + 2):
        if os.path.exists(spec.output):
            os.remove(spec.output)
        log_path = os.path.join(log_dir, f"{spec.job_id}.attempt{attempt}.log")
        with open(log_path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write("command: " + " ".join(spec.args) + "\n\n")
            fh.flush()
            proc = subprocess.Popen(spec.args, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    **qgis_process.popen_options())
            while True:
                try:
                    rc = proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if cancel.is_set():
                        qgis_process.kill(proc)
                        raise JobsCanceled(spec.job_id)
        if rc == 0 and os.path.isfile(spec.output):
            return JobResult(spec.job_id, spec.output, time.monotonic() - t0, attempt)
        last = f"job {spec.job_id}: exit code {rc} (log: {log_path})"
        log.warning("%s (attempt %d of %d)", last, attempt, retries + 1)
    raise JobFailed(last)


def run_jobs(specs: Sequence[JobSpec], concurrency, log_dir: str, is_canceled: Callable[[], bool],
             on_progress: Callable[[float], None], retries: int = 1,
             stats: Optional[Dict[str, Any]] = None, run=None) -> List[JobResult]:
    """Run jobs largest first on parallel processes. Callbacks run in the calling thread.

    ``run`` (default :func:`run_one`) runs one job; tests pass a fake.
    """
    run = run or run_one
    cc = concurrency if isinstance(concurrency, Concurrency) else Concurrency(int(concurrency), int(concurrency))
    monitor = cc.monitor
    if cc.adaptive and monitor is None:
        from .sysload import CpuMonitor
        monitor = CpuMonitor()
    adaptive = cc.adaptive and monitor is not None and getattr(monitor, "available", True)
    limit = max(1, min(cc.initial, cc.maximum))
    total = sum(s.workload for s in specs) or 1.0
    done_load, peak, samples = 0.0, 0, []
    last_tick = last_change = time.monotonic()
    waiting = sorted(specs, key=lambda s: s.workload, reverse=True)
    results: List[JobResult] = []
    failures: List[str] = []
    cancel = threading.Event()
    pool = ThreadPoolExecutor(max_workers=max(1, cc.maximum), thread_name_prefix="qgis2rastertiles-job")
    running: Dict[Future, JobSpec] = {}
    try:
        while waiting or running:
            if not cancel.is_set():
                while waiting and len(running) < limit:
                    s = waiting.pop(0)
                    running[pool.submit(run, s, cancel, log_dir, retries)] = s
                peak = max(peak, len(running))
            if not running:
                break
            finished, _ = wait(set(running), timeout=0.5, return_when=FIRST_COMPLETED)
            for f in finished:
                spec = running.pop(f)
                try:
                    results.append(f.result())
                    done_load += spec.workload
                except JobsCanceled:
                    pass
                except Exception as e:  # JobFailed or unexpected
                    failures.append(f"{spec.job_id}: {e}")
            on_progress(done_load / total)
            if is_canceled() and not cancel.is_set():
                cancel.set()
                waiting.clear()
            now = time.monotonic()
            if adaptive and not cancel.is_set() and now - last_tick >= cc.interval_s:
                last_tick = now
                u = monitor.sample()
                if u is not None:
                    samples.append(u)
                    settled = now - last_change >= 2 * cc.interval_s
                    if settled and u < cc.cpu_target - 8 and waiting and limit < cc.maximum:
                        ram = monitor.free_ram_gb()
                        if ram is None or ram >= cc.ram_per_job_gb:
                            limit = min(cc.maximum, limit + (2 if u < cc.cpu_target - 30 else 1))
                            last_change = now
                            log.debug("CPU %.0f%%: up to %d parallel processes", u, limit)
                    elif settled and u > cc.cpu_target + 3 and limit > 1:
                        limit -= 1
                        last_change = now
    finally:
        if is_canceled():
            cancel.set()
        pool.shutdown(wait=True, cancel_futures=True)
    if stats is not None:
        stats.update(peak_processes=peak, avg_cpu=(sum(samples) / len(samples)) if samples else None)
    if is_canceled():
        raise JobsCanceled("canceled")
    if failures:
        raise JobFailed(f"{len(failures)} job(s) failed:\n  " + "\n  ".join(failures))
    return results
