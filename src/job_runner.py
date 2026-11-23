"""Running the built-in algorithm in parallel ``qgis_process`` processes.

* :func:`find_qgis_process` locates the executable that belongs to the
  running QGIS build (Windows .bat naming variants, .exe, Linux binary).
* :class:`QgisProcessRunner` runs one job: parameters are sent as JSON on
  stdin (``qgis_process run ALG -``), which avoids all cmd.exe quoting issues
  with commas, spaces and brackets in extents and paths.
* :func:`run_jobs` executes many jobs on a thread pool (each thread only waits
  on its child process), retries failures, records finished jobs in a
  manifest for resuming and kills child process trees on cancellation.

No QGIS dependency.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

log = logging.getLogger("qgis2rastertiles." + __name__.rsplit(".", 1)[-1])

IS_WINDOWS = sys.platform == "win32"


class QgisProcessNotFound(RuntimeError):
    pass


class JobFailed(RuntimeError):
    pass


class JobsCanceled(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Locating qgis_process
# ---------------------------------------------------------------------------
def qgis_process_candidates(prefix_path: str, platform: str = sys.platform) -> List[Path]:
    """Candidate qgis_process executables for a QGIS prefix path, best first.

    Windows (OSGeo4W and standalone installers): the prefix is
    ``<root>/apps/<build>`` and launchers live in ``<root>/bin`` as
    ``qgis_process-<build>.bat`` (e.g. ``qgis_process-qgis.bat``,
    ``qgis_process-qgis-dev.bat``, ``qgis_process-qgis-ltr-qt6.bat``). The
    build's own ``qgis_process.exe`` in ``<prefix>/bin`` is the last resort
    (it needs the environment of the running QGIS, which child processes inherit).
    """
    prefix = Path(prefix_path)
    out: List[Path] = []
    if platform == "win32":
        build = prefix.name  # e.g. qgis, qgis-dev, qgis-ltr-qt6
        root_bin = prefix.parent.parent / "bin"
        out.append(root_bin / f"qgis_process-{build}.bat")
        if root_bin.is_dir():
            others = sorted(p for p in root_bin.glob("qgis_process*.bat") if p not in out)
            # launchers mentioning this build name first, generic ones afterwards
            others.sort(key=lambda p: (build not in p.stem, len(p.stem)))
            out.extend(others)
        out.append(root_bin / "qgis_process.exe")
        out.append(prefix / "bin" / "qgis_process.exe")
    else:
        out.append(prefix / "bin" / "qgis_process")
        out.append(prefix.parent / "bin" / "qgis_process")
        out.append(prefix / "MacOS" / "bin" / "qgis_process")  # macOS bundle variants
        w = shutil.which("qgis_process")
        if w:
            out.append(Path(w))
    seen, unique = set(), []
    for p in out:
        k = str(p).lower() if platform == "win32" else str(p)
        if k not in seen:
            seen.add(k)
            unique.append(p)
    return unique


CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def profile_arguments(settings_dir: str) -> List[str]:
    """``--profile``/``--profiles-path`` for the running QGIS user profile.

    QGIS stores a profile in ``<base>/profiles/<name>/``. Passing it makes the
    jobs read the same global settings as the QGIS session (OpenCL/GPU, user
    fonts, SVG paths, max threads, ...). Empty if the layout is not recognised.
    """
    p = Path(settings_dir.rstrip("/\\"))
    if p.parent.name.lower() == "profiles" and p.name:
        return ["--profile", p.name, "--profiles-path", str(p.parent.parent)]
    return []


def _creationflags(below_normal: bool = False) -> int:
    """No console window; optionally below-normal priority so the QGIS
    interface stays responsive while the rendering jobs saturate the CPU."""
    if not IS_WINDOWS:
        return 0
    return CREATE_NO_WINDOW | (BELOW_NORMAL_PRIORITY_CLASS if below_normal else 0)


def probe_version(exe: Path, timeout: float = 180) -> Optional[str]:
    """First line of ``qgis_process --version`` (e.g. "QGIS 4.3.0-Master ..."), or None."""
    try:
        cp = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=timeout,
                            creationflags=_creationflags(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (cp.stdout or "").splitlines():
        if line.startswith("QGIS "):
            return line.strip()
    return None


def find_qgis_process(prefix_path: str, expected_version: Optional[str] = None,
                      explicit: Optional[str] = None) -> Path:
    """Return the qgis_process matching the running QGIS.

    :param expected_version: ``Qgis.version()`` of the running QGIS, e.g.
        "4.3.0-Master". Candidates reporting another version are skipped so a
        stable build is not used by a dev build (or vice versa).
    """
    if explicit and explicit != "auto":
        p = Path(explicit)
        if not p.is_file():
            raise QgisProcessNotFound(f"qgis_process_path from defaults.json does not exist: {p}")
        return p
    tried = []
    for cand in qgis_process_candidates(prefix_path):
        if not cand.is_file():
            continue
        ver = probe_version(cand)
        tried.append(f"{cand} -> {ver or 'no version output'}")
        if ver is None:
            continue
        if expected_version and expected_version not in ver:
            continue
        return cand
    raise QgisProcessNotFound(
        "Could not find a qgis_process executable for this QGIS "
        f"(prefix {prefix_path}, version {expected_version}). Tried:\n  " + "\n  ".join(tried or ["(no candidates exist)"])
        + "\nSet 'qgis_process_path' in defaults.json to override.")


# ---------------------------------------------------------------------------
# Manifest (resume support)
# ---------------------------------------------------------------------------
class Manifest:
    """JSON file listing completed job ids; written atomically after every job."""

    def __init__(self, path: Path, plan_key: str) -> None:
        self.path = Path(path)
        self.plan_key = plan_key
        self._lock = threading.Lock()
        self.completed: Dict[str, Dict[str, Any]] = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("plan_key") == plan_key:
                    self.completed = data.get("completed", {})
            except (OSError, ValueError):
                log.warning("Ignoring unreadable manifest %s", self.path)

    def is_done(self, job_id: str, validate: Optional[Callable[[str], bool]] = None) -> bool:
        rec = self.completed.get(job_id)
        if not rec:
            return False
        return validate(rec["output"]) if validate else os.path.isfile(rec["output"])

    def mark_done(self, job_id: str, output: str, seconds: float) -> None:
        with self._lock:
            self.completed[job_id] = {"output": output, "seconds": round(seconds, 3)}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"plan_key": self.plan_key, "completed": self.completed}, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# Running one job
# ---------------------------------------------------------------------------
@dataclass
class JobSpec:
    job_id: str
    inputs: Dict[str, Any]  # parameter name -> JSON value, including the output file
    output: str
    workload: float = 1.0


@dataclass
class JobResult:
    job_id: str
    output: str
    seconds: float
    attempts: int
    skipped: bool = False
    results: Dict[str, Any] = field(default_factory=dict)


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a child and all of its descendants (cmd.exe -> qgis_process.exe on Windows)."""
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=_creationflags())
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


class QgisProcessRunner:
    """Runs the built-in algorithm in a qgis_process child process."""

    def __init__(self, qgis_process: Path, algorithm_id: str, project_path: str, log_dir: Path,
                 ellipsoid: Optional[str] = None, skip_plugins: bool = True,
                 env: Optional[Dict[str, str]] = None, profile_args: Sequence[str] = ()) -> None:
        self.qgis_process = Path(qgis_process)
        self.algorithm_id = algorithm_id
        self.project_path = project_path
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.ellipsoid = ellipsoid
        self.skip_plugins = skip_plugins
        self.env = env
        self.profile_args = list(profile_args)

    def base_command(self, skip_plugins: Optional[bool] = None) -> List[str]:
        """qgis_process with the global options (priority, plugins, user profile)."""
        cmd = []
        if not IS_WINDOWS and shutil.which("nice"):
            cmd += ["nice", "-n", "5"]  # lower priority, like BELOW_NORMAL on Windows
        cmd.append(str(self.qgis_process))
        if self.skip_plugins if skip_plugins is None else skip_plugins:
            cmd.append("--skip-loading-plugins")
        cmd += self.profile_args
        return cmd

    def command(self) -> List[str]:
        return self.base_command() + ["run", self.algorithm_id, "-"]

    def payload(self, inputs: Dict[str, Any]) -> str:
        p: Dict[str, Any] = {"inputs": inputs, "project_path": self.project_path}
        if self.ellipsoid:
            p["ellipsoid"] = self.ellipsoid
        return json.dumps(p)

    def run_once(self, spec: JobSpec, attempt: int, cancel: threading.Event) -> Dict[str, Any]:
        """Run one attempt; raises JobFailed / JobsCanceled."""
        out_log = self.log_dir / f"{spec.job_id}.attempt{attempt}.stdout.log"
        err_log = self.log_dir / f"{spec.job_id}.attempt{attempt}.stderr.log"
        for suffix in ("", "-wal", "-shm", "-journal"):
            if os.path.exists(spec.output + suffix):
                os.remove(spec.output + suffix)
        popen_kw: Dict[str, Any] = ({"creationflags": _creationflags(below_normal=True)} if IS_WINDOWS
                                    else {"start_new_session": True})
        with open(out_log, "w", encoding="utf-8", errors="replace") as fo, \
                open(err_log, "w", encoding="utf-8", errors="replace") as fe:
            fe.write("command: " + " ".join(self.command()) + "\n")
            fe.write("stdin: " + self.payload(spec.inputs) + "\n\n")
            fe.flush()
            proc = subprocess.Popen(self.command(), stdin=subprocess.PIPE, stdout=fo, stderr=fe,
                                    env=self.env, **popen_kw)
            try:
                proc.stdin.write(self.payload(spec.inputs).encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass
            while True:
                try:
                    rc = proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if cancel.is_set():
                        kill_process_tree(proc)
                        proc.wait()
                        raise JobsCanceled(spec.job_id)
        text = out_log.read_text(encoding="utf-8", errors="replace")
        result = _parse_json_output(text)
        if rc != 0:
            raise JobFailed(f"job {spec.job_id}: qgis_process exited with code {rc} (see {out_log} / {err_log})")
        if not os.path.isfile(spec.output):
            raise JobFailed(f"job {spec.job_id}: output {spec.output} was not created (see {err_log})")
        return result

    def run(self, spec: JobSpec, cancel: threading.Event, retries: int = 1) -> JobResult:
        t0 = time.monotonic()
        last: Optional[Exception] = None
        for attempt in range(1, retries + 2):
            if cancel.is_set():
                raise JobsCanceled(spec.job_id)
            try:
                res = self.run_once(spec, attempt, cancel)
                return JobResult(spec.job_id, spec.output, time.monotonic() - t0, attempt, results=res)
            except JobFailed as e:
                last = e
                log.warning("%s (attempt %d of %d)", e, attempt, retries + 1)
        raise JobFailed(str(last))


def _parse_json_output(text: str) -> Dict[str, Any]:
    i = text.find("{")
    if i < 0:
        return {}
    try:
        return json.loads(text[i:])
    except ValueError:
        return {}


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------
def run_jobs(specs: Sequence[JobSpec], runner: QgisProcessRunner, max_workers: int, manifest: Manifest,
             is_canceled: Callable[[], bool], on_progress: Callable[[float], None],
             on_message: Callable[[str], None], retries: int = 1,
             validate_output: Optional[Callable[[str], bool]] = None) -> List[JobResult]:
    """Run jobs largest-workload-first on ``max_workers`` processes.

    Callbacks are only invoked from the calling thread. Finished jobs found in
    the manifest are skipped. Raises :class:`JobsCanceled` or
    :class:`JobFailed` (after the other running jobs finished).
    """
    total = sum(s.workload for s in specs) or 1.0
    done_load = 0.0
    results: List[JobResult] = []
    todo: List[JobSpec] = []
    for s in specs:
        if manifest.is_done(s.job_id, validate_output):
            results.append(JobResult(s.job_id, manifest.completed[s.job_id]["output"], 0.0, 0, skipped=True))
            done_load += s.workload
        else:
            todo.append(s)
    if results:
        on_message(f"Resuming: {len(results)} job(s) already completed, {len(todo)} to run")
    on_progress(done_load / total)
    todo.sort(key=lambda s: s.workload, reverse=True)

    cancel = threading.Event()
    msgs: "queue.Queue[str]" = queue.Queue()
    failures: List[str] = []

    def work(spec: JobSpec) -> JobResult:
        log.debug("Job %s started", spec.job_id)
        r = runner.run(spec, cancel, retries)
        manifest.mark_done(spec.job_id, spec.output, r.seconds)
        log.debug(f"Job {spec.job_id} finished in {r.seconds:.1f}s"
                 + (f" (after {r.attempts} attempts)" if r.attempts > 1 else ""))
        return r

    def drain() -> None:
        while True:
            try:
                on_message(msgs.get_nowait())
            except queue.Empty:
                return

    pool = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="pxyz-job")
    futures: Dict[Future, JobSpec] = {}
    try:
        for s in todo:  # submission order = largest first
            futures[pool.submit(work, s)] = s
        pending = set(futures)
        while pending:
            finished, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for f in finished:
                spec = futures[f]
                try:
                    results.append(f.result())
                    done_load += spec.workload
                except JobsCanceled:
                    pass
                except Exception as e:  # JobFailed or unexpected
                    failures.append(f"{spec.job_id}: {e}")
                    msgs.put(f"Job {spec.job_id} FAILED: {e}")
            drain()
            on_progress(done_load / total)
            if is_canceled() and not cancel.is_set():
                on_message("Cancel requested: stopping running qgis_process jobs ...")
                cancel.set()
                for f in pending:
                    f.cancel()
        drain()
    finally:
        if is_canceled():
            cancel.set()
        pool.shutdown(wait=True, cancel_futures=True)
    if is_canceled():
        raise JobsCanceled("canceled by user")
    if failures:
        raise JobFailed(f"{len(failures)} job(s) failed:\n  " + "\n  ".join(failures))
    return results
