"""defaults.json validation, job-count resolution, qgis_process discovery, manifest (no QGIS)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import ConfigError, load_config, resolve_max_parallel_jobs, validate_config  # noqa: E402
from src.job_runner import Manifest, qgis_process_candidates  # noqa: E402


class ConfigTest(unittest.TestCase):
    def test_shipped_defaults_are_valid(self):
        cfg = load_config()
        self.assertEqual(cfg.tile_format, "PNG")
        self.assertEqual(cfg.zoom0_extent, (-180.0, 180.0, -90.0, 90.0))
        self.assertEqual(cfg.background_color_string(), "rgba(0,0,0,0)")
        self.assertEqual(cfg.split_tile_threshold, 20000)  # advanced default

    def test_errors_are_collected(self):
        raw = json.loads(Path(__file__).parents[1].joinpath("defaults.json").read_text())
        raw.update({"tile_format": "TIFF", "quality": 0, "zoom0_extent": [1, 0, 0, 1], "typo_key": 1})
        with self.assertRaises(ConfigError) as cm:
            validate_config(raw)
        self.assertIn("typo_key", str(cm.exception))
        raw.pop("typo_key")
        with self.assertRaises(ConfigError) as cm:
            validate_config(raw)
        msg = str(cm.exception)
        for k in ("tile_format", "quality", "zoom0_extent"):
            self.assertIn(k, msg)

    def test_missing_key(self):
        raw = json.loads(Path(__file__).parents[1].joinpath("defaults.json").read_text())
        del raw["dpi"]
        with self.assertRaisesRegex(ConfigError, "missing required key 'dpi'"):
            validate_config(raw)

    def test_bad_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{ not json")
        try:
            with self.assertRaisesRegex(ConfigError, "not valid JSON"):
                load_config(fh.name)
        finally:
            os.remove(fh.name)

    def test_auto_jobs(self):
        cfg = load_config()
        self.assertEqual(resolve_max_parallel_jobs(cfg, cpu_count=20, mem_gb=64)[0], 10)
        self.assertEqual(resolve_max_parallel_jobs(cfg, cpu_count=20, mem_gb=9)[0], 4)
        self.assertEqual(resolve_max_parallel_jobs(cfg, cpu_count=1, mem_gb=64)[0], 1)
        # CPU usage limit
        self.assertEqual(resolve_max_parallel_jobs(cfg, cpu_count=20, mem_gb=64, cpu_percent=50)[0], 5)
        self.assertEqual(resolve_max_parallel_jobs(cfg, cpu_count=20, mem_gb=64, cpu_percent=1)[0], 1)
        explicit = validate_config({**cfg.to_dict(), "max_parallel_jobs": 12})
        self.assertEqual(resolve_max_parallel_jobs(explicit, cpu_count=20, mem_gb=64, cpu_percent=100)[0], 12)
        self.assertEqual(resolve_max_parallel_jobs(explicit, cpu_count=20, mem_gb=64, cpu_percent=25)[0], 5)


class LocatorTest(unittest.TestCase):
    def test_windows_osgeo4w_names(self):
        with tempfile.TemporaryDirectory() as root:
            bin_dir = Path(root, "bin")
            bin_dir.mkdir()
            for n in ("qgis_process-qgis.bat", "qgis_process-qgis-dev.bat", "qgis_process-qgis-ltr-qt6.bat"):
                (bin_dir / n).write_text("@echo off")
            prefix = Path(root, "apps", "qgis-dev")
            c = qgis_process_candidates(str(prefix), platform="win32")
            self.assertEqual(c[0].name, "qgis_process-qgis-dev.bat")
            self.assertIn(prefix / "bin" / "qgis_process.exe", c)
            c2 = qgis_process_candidates(str(Path(root, "apps", "qgis")), platform="win32")
            self.assertEqual(c2[0].name, "qgis_process-qgis.bat")

    def test_profile_arguments(self):
        from src.job_runner import profile_arguments
        a = profile_arguments("C:/Users/u/AppData/Roaming/QGIS/QGIS4/profiles/work/")
        self.assertEqual(a[:2], ["--profile", "work"])
        self.assertEqual(Path(a[3]), Path("C:/Users/u/AppData/Roaming/QGIS/QGIS4"))
        self.assertEqual(profile_arguments("/tmp/somewhere/"), [])

    def test_linux(self):
        c = qgis_process_candidates("/usr", platform="linux")
        self.assertEqual(str(c[0]).replace("\\", "/"), "/usr/bin/qgis_process")


class ManifestTest(unittest.TestCase):
    def test_resume_by_job_id_and_valid_output(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d, "part.gpkg")
            out.write_bytes(b"x")
            m = Manifest(Path(d, "manifest.json"), "plan-A")
            m.mark_done("job1", str(out), 1.5)
            self.assertTrue(Manifest(Path(d, "manifest.json"), "plan-A").is_done("job1"))
            # a re-planned run keeps finished jobs (ids name the exact tile range)
            self.assertTrue(Manifest(Path(d, "manifest.json"), "plan-B").is_done("job1"))
            self.assertEqual(Manifest(Path(d, "manifest.json"), "plan-B").done_ids(), {"job1"})
            out.unlink()
            self.assertFalse(Manifest(Path(d, "manifest.json"), "plan-A").is_done("job1"))


if __name__ == "__main__":
    unittest.main()
