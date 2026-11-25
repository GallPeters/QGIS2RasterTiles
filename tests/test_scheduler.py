"""Adaptive number of parallel processes (no QGIS: fake jobs and a fake CPU monitor)."""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.job_runner import Concurrency, JobResult, JobSpec, run_jobs  # noqa: E402
from src.sysload import CpuMonitor  # noqa: E402


class FakeRun:
    """Each job sleeps; records the highest number of jobs running at once."""

    def __init__(self, seconds: float) -> None:
        self.seconds, self.running, self.max_running = seconds, 0, 0
        self.lock = threading.Lock()

    def __call__(self, spec, cancel, log_dir, retries):
        with self.lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        time.sleep(self.seconds)
        with self.lock:
            self.running -= 1
        return JobResult(spec.job_id, spec.output, self.seconds, 1)


class FakeMonitor:
    available = True

    def __init__(self, cpu: float, ram: float = 64.0) -> None:
        self.cpu, self.ram = cpu, ram

    def sample(self):
        return self.cpu

    def free_ram_gb(self):
        return self.ram


class SchedulerTest(unittest.TestCase):
    def run_with(self, monitor, n_jobs=60):
        fake = FakeRun(0.25)
        stats = {}
        cc = Concurrency(2, 8, adaptive=True, cpu_target=95, ram_per_job_gb=2, interval_s=0.1, monitor=monitor)
        with tempfile.TemporaryDirectory() as d:
            specs = [JobSpec(f"j{i}", [], os.path.join(d, f"j{i}.gpkg")) for i in range(n_jobs)]
            res = run_jobs(specs, cc, d, lambda: False, lambda f: None, stats=stats, run=fake)
        self.assertEqual(len(res), n_jobs)
        return fake.max_running, stats

    def test_idle_cpu_ramps_up(self):
        peak, stats = self.run_with(FakeMonitor(cpu=20))
        self.assertEqual(peak, 8)
        self.assertEqual(stats["peak_processes"], 8)

    def test_busy_cpu_stays(self):
        self.assertEqual(self.run_with(FakeMonitor(cpu=93))[0], 2)

    def test_low_ram_blocks(self):
        self.assertEqual(self.run_with(FakeMonitor(cpu=20, ram=1.0))[0], 2)

    def test_real_cpu_monitor(self):
        m = CpuMonitor()
        if not m.available:
            self.skipTest("no CPU counters")
        time.sleep(0.2)
        u = m.sample()
        self.assertTrue(u is not None and 0 <= u <= 100)


if __name__ == "__main__":
    unittest.main()
