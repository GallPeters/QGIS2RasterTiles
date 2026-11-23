"""Adaptive number of parallel processes (no QGIS, fake jobs and a fake CPU monitor)."""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.job_runner import Concurrency, JobResult, JobSpec, Manifest, run_jobs  # noqa: E402
from src.sysload import CpuMonitor  # noqa: E402


class FakeRunner:
    """Each job sleeps; records the highest number of jobs running at once."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.running = 0
        self.max_running = 0
        self.lock = threading.Lock()

    def run(self, spec, cancel, retries):
        with self.lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        time.sleep(self.seconds)
        Path(spec.output).write_bytes(b"x")
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
    def run_with(self, monitor, initial=2, maximum=8, n_jobs=60):
        with tempfile.TemporaryDirectory() as d:
            specs = [JobSpec(f"j{i}", {}, os.path.join(d, f"j{i}.gpkg"), 1.0) for i in range(n_jobs)]
            runner = FakeRunner(0.25)
            stats = {}
            cc = Concurrency(initial, maximum, adaptive=True, cpu_target=95, ram_per_job_gb=2,
                             interval_s=0.1, monitor=monitor)
            res = run_jobs(specs, runner, cc, Manifest(Path(d, "m.json"), "k"), lambda: False,
                           lambda f: None, lambda m: None, stats=stats)
            self.assertEqual(len(res), n_jobs)
            return runner.max_running, stats

    def test_idle_cpu_ramps_up_to_maximum(self):
        peak, stats = self.run_with(FakeMonitor(cpu=20))
        self.assertEqual(peak, 8)
        self.assertEqual(stats["peak_processes"], 8)
        self.assertAlmostEqual(stats["avg_cpu"], 20)

    def test_busy_cpu_stays_at_initial(self):
        peak, _ = self.run_with(FakeMonitor(cpu=93))
        self.assertEqual(peak, 2)

    def test_low_ram_blocks_more_processes(self):
        peak, _ = self.run_with(FakeMonitor(cpu=20, ram=1.0))
        self.assertEqual(peak, 2)

    def test_fixed_number(self):
        with tempfile.TemporaryDirectory() as d:
            specs = [JobSpec(f"j{i}", {}, os.path.join(d, f"j{i}.gpkg"), 1.0) for i in range(12)]
            runner = FakeRunner(0.05)
            run_jobs(specs, runner, 3, Manifest(Path(d, "m.json"), "k"), lambda: False, lambda f: None,
                     lambda m: None)
            self.assertEqual(runner.max_running, 3)

    def test_real_cpu_monitor(self):
        m = CpuMonitor()
        if not m.available:
            self.skipTest("CPU counters not available on this platform")
        time.sleep(0.2)
        u = m.sample()
        self.assertIsNotNone(u)
        self.assertTrue(0 <= u <= 100)


if __name__ == "__main__":
    unittest.main()
