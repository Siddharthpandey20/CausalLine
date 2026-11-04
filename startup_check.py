#!/usr/bin/env python3
"""Can this machine run CausalLine? One command, ten answers, no tokens spent.

    python startup_check.py                 # everything
    python startup_check.py --local-only    # skip the external-API probes
    python startup_check.py --probe         # also send one real local prompt
    python startup_check.py --json          # machine-readable

PASS  the thing works.
WARN  the thing is missing or unusable, and something still runs without it.
FAIL  something you probably wanted will not run; the line says what to type.

A FAIL is never fatal to the whole report -- every check runs, so one command
tells you everything that needs fixing rather than the first thing.

NEVER PRINTS A KEY
------------------
Keys are reported as present/absent and by length only, and the external probes
report reachability, not content. That is enforced by `tests/test_no_secrets.py`
and it is the reason this file does its own `.env` reading rather than echoing
any settings object -- a dataclass repr is how a key leaked into a terminal
once already.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
MIN_PYTHON = (3, 11)
# Written to by the evaluation runners. Relative, so a clone anywhere works.
WRITE_DIRS = ("data/runs", "data/results", "data/generated")


class Report:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, status: str, name: str, detail: str, fix: str = "") -> None:
        self.rows.append({"status": status, "check": name,
                          "detail": detail, "fix": fix})

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows if r["status"] == status)

    def render(self) -> str:
        width = max(len(r["check"]) for r in self.rows) + 2
        out = []
        for r in self.rows:
            out.append(f"  {r['status']:<5} {r['check']:<{width}} {r['detail']}")
            if r["fix"]:
                out.append(f"        {'':<{width}} -> {r['fix']}")
        return "\n".join(out)


# --- helpers ----------------------------------------------------------------

def read_env_file(path: str = ".env") -> dict[str, str]:
    """Load `.env` into the process environment without logging any value."""
    found: dict[str, str] = {}
    env = Path(path)
    if not env.exists():
        return found
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        found[key] = value
        os.environ.setdefault(key, value)
    return found


def ollama_base() -> str:
    host = (os.environ.get("OLLAMA_ENDPOINT", "")
            or os.environ.get("OLLAMA_HOST", "")).strip()
    if not host:
        return "http://localhost:11434"
    return host if "://" in host else "http://" + host


def http_json(url: str, payload: dict | None = None,
              timeout: float = 10.0) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


# --- the checks -------------------------------------------------------------

def check_python(report: Report) -> None:
    v = sys.version_info
    actual = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= MIN_PYTHON:
        report.add(PASS, "python", f"{actual} on {platform.system()} "
                                   f"{platform.machine()}")
    else:
        report.add(FAIL, "python", f"{actual}; need >= 3.11",
                   "install Python 3.11+ and recreate the venv: "
                   "python3.11 -m venv .venv")


def check_core_imports(report: Report) -> None:
    """The core has no third-party dependencies. If this fails, the clone is
    broken or you are not running from the repository root."""
    try:
        import src.provenance.contamination  # noqa: F401
        import src.recovery.causalline  # noqa: F401
        import src.tracing.logger  # noqa: F401
        report.add(PASS, "core imports",
                   "tracing + provenance + recovery import, zero third-party deps")
    except Exception as exc:  # noqa: BLE001
        report.add(FAIL, "core imports", f"{type(exc).__name__}: {exc}",
                   "run from the repository root (the directory holding "
                   "pyproject.toml), or: pip install -e .")


def check_optional_deps(report: Report) -> None:
    for module, what, command in (
        ("numpy", "the Lasso fallback in budget_attribution",
         "pip install -r requirements.txt"),
        ("matplotlib", "the two required figures from src.eval.economics",
         "pip install -r requirements.txt"),
        ("pytest", "the test suite", "pip install pytest"),
    ):
        if importlib.util.find_spec(module) is not None:
            report.add(PASS, f"dep: {module}", "available")
        else:
            report.add(WARN, f"dep: {module}",
                       f"missing; {what} will not run", command)


def check_ollama_service(report: Report) -> str | None:
    """Is the daemon there? Returns the base URL when it is."""
    base = ollama_base()
    where = "default localhost:11434" if base.endswith(":11434") and (
        "localhost" in base) else f"OLLAMA_HOST={base}"
    if shutil.which("ollama") is None and "localhost" in base:
        report.add(WARN, "ollama binary", "not on PATH",
                   "install from https://ollama.com/download (the daemon may "
                   "still be reachable over HTTP, checked next)")
    try:
        http_json(f"{base}/api/tags", timeout=5.0)
        report.add(PASS, "ollama service", f"reachable at {base} ({where})")
        return base
    except urllib.error.URLError as exc:
        report.add(FAIL, "ollama service", f"{base} unreachable: {exc.reason}",
                   "start it with: ollama serve   (or set OLLAMA_HOST if it "
                   "runs on another machine)")
    except (TimeoutError, socket.timeout):
        report.add(FAIL, "ollama service", f"{base} timed out",
                   "start it with: ollama serve")
    except Exception as exc:  # noqa: BLE001
        report.add(FAIL, "ollama service", f"{base}: {type(exc).__name__}",
                   "start it with: ollama serve")
    return None


def check_ollama_model(report: Report, base: str | None) -> str | None:
    """Is a usable model pulled? Prefers the one the results were produced on."""
    from src.eval.mixed_campaign import LOCAL_MODEL

    wanted = os.environ.get("OLLAMA_MODEL", "").strip() or LOCAL_MODEL
    if base is None:
        report.add(FAIL, "ollama model", "not checked: service unreachable",
                   f"after starting the daemon: ollama pull {wanted}")
        return None
    try:
        tags = http_json(f"{base}/api/tags", timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        report.add(FAIL, "ollama model", f"could not list models: "
                                         f"{type(exc).__name__}",
                   f"ollama pull {wanted}")
        return None
    names = [m.get("name", "") for m in tags.get("models", [])]
    if not names:
        report.add(FAIL, "ollama model", "no models pulled",
                   f"ollama pull {wanted}")
        return None
    # `llama3` and `llama3:latest` are the same model under two spellings.
    stem = wanted.split(":")[0]
    exact = [n for n in names if n == wanted]
    family = [n for n in names if n.split(":")[0] == stem]
    if exact:
        report.add(PASS, "ollama model", f"{wanted} present")
        return wanted
    if family:
        report.add(PASS, "ollama model",
                   f"{family[0]} present (asked for {wanted}; same family)")
        return family[0]
    report.add(WARN, "ollama model",
               f"{wanted} not pulled; have {', '.join(names[:4])}",
               f"ollama pull {wanted}   (or set OLLAMA_MODEL to one you have; "
               "a smaller model may fail the 56-agent task on capability -- "
               "see docs/real_mixed_model_60_agent_experiment.md)")
    return names[0]


def check_local_inference(report: Report, base: str | None, model: str | None,
                          probe: bool) -> None:
    """One real generate call. Off by default because on CPU it can take a
    minute, which is a slow answer rather than a broken one."""
    if not probe:
        report.add(WARN, "local inference", "not probed",
                   "re-run with --probe to send one real prompt")
        return
    if base is None or model is None:
        report.add(FAIL, "local inference", "not probed: no service or model",
                   "fix the two checks above first")
        return
    started = time.time()
    try:
        answer = http_json(f"{base}/api/generate", {
            "model": model,
            "prompt": "Reply with the single word: ready",
            "stream": False,
            "options": {"temperature": 0, "num_predict": 8},
        }, timeout=300.0)
        text = (answer.get("response") or "").strip()
        took = time.time() - started
        if text:
            report.add(PASS, "local inference",
                       f'{model} answered in {took:.1f}s: "{text[:40]}"')
        else:
            report.add(FAIL, "local inference", f"{model} returned no text",
                       f"check the model runs at all: ollama run {model}")
    except Exception as exc:  # noqa: BLE001
        report.add(FAIL, "local inference",
                   f"{type(exc).__name__} after {time.time() - started:.0f}s",
                   f"check the model runs at all: ollama run {model}")


def check_hardware(report: Report) -> None:
    cores = os.cpu_count() or 0
    ram_gb = _total_ram_gb()
    detail = f"{cores} logical cores"
    detail += f", {ram_gb:.1f} GB RAM" if ram_gb else ", RAM unknown"
    # An 8B model at 4-bit needs ~6 GB plus the OS. Below 8 GB the 56-agent
    # run is not impossible, it is slow and at risk of an OOM kill -- which
    # happened on the development machine and is why the driver is resumable.
    if ram_gb and ram_gb < 8:
        report.add(WARN, "hardware", detail + "; under 8 GB",
                   "use --mode production_network --num-agents 12 rather than "
                   "the 56-agent mode, or a smaller model")
    elif cores and cores < 4:
        report.add(WARN, "hardware", detail + "; fewer than 4 cores",
                   "expect slow local inference; keep --max-concurrency 1")
    else:
        report.add(PASS, "hardware", detail)


def _total_ram_gb() -> float:
    try:  # Linux / WSL
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / (1024 ** 2)
    except Exception:  # noqa: BLE001
        pass
    try:  # Windows
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullExtendedVirtual", ctypes.c_ulonglong)]

        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys / (1024 ** 3)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def check_gpu(report: Report) -> None:
    """Optional everywhere. Ollama runs on CPU without one, just slowly."""
    smi = shutil.which("nvidia-smi")
    if smi is None:
        report.add(WARN, "nvidia gpu", "no nvidia-smi; CPU inference only",
                   "nothing to fix -- local runs work on CPU at roughly "
                   "4 tokens/s (docs/local_llm_frontier/01)")
        return
    import subprocess
    try:
        out = subprocess.run(
            [smi, "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20)
        line = (out.stdout or "").strip().splitlines()
        if out.returncode == 0 and line:
            report.add(PASS, "nvidia gpu", "; ".join(x.strip() for x in line))
        else:
            report.add(WARN, "nvidia gpu", "nvidia-smi present but reported "
                                           "no device", "CPU inference will be used")
    except Exception as exc:  # noqa: BLE001
        report.add(WARN, "nvidia gpu", f"nvidia-smi failed: "
                                       f"{type(exc).__name__}",
                   "CPU inference will be used")


def check_env_vars(report: Report, from_file: dict[str, str]) -> dict[str, bool]:
    """Presence and length only. Never the value."""
    present: dict[str, bool] = {}
    groups = {
        "GEMINI_API_KEY": ("gemini", ["GEMINI_API_KEY"]),
        "NVIDIA_API_KEY": ("nvidia", [f"NVIDIA_API_KEY_{i}" for i in (1, 2, 3)]),
    }
    placeholders = ("your-key-here", "your-first-nvidia-key",
                    "your-second-nvidia-key", "your-third-nvidia-key")
    for label, names in groups.values():
        real = []
        for name in names:
            value = os.environ.get(name, "").strip()
            if value and value not in placeholders:
                real.append(name)
        present[label] = bool(real)
        if real:
            lengths = ", ".join(f"{n}({len(os.environ[n].strip())} chars)"
                                for n in real)
            report.add(PASS, f"env: {label}", f"set -- {lengths}")
        else:
            report.add(WARN, f"env: {label}",
                       "not set; local-only execution is unaffected",
                       "optional: copy .env.example to .env and fill it in")
    if not Path(".env").exists():
        report.add(WARN, "env: .env file", "absent; using the real environment",
                   "optional: cp .env.example .env")
    else:
        report.add(PASS, "env: .env file",
                   f"present, {len(from_file)} keys read (values never printed)")
    return present


def check_external_apis(report: Report, present: dict[str, bool],
                        local_only: bool) -> None:
    if local_only:
        report.add(WARN, "external apis", "skipped (--local-only)",
                   "drop --local-only to probe Gemini and NVIDIA")
        return
    if not present.get("nvidia"):
        report.add(WARN, "nvidia api", "no key; mixed-provider mode unavailable",
                   "local-only mode needs no key: "
                   "python -m src.eval.college_runner --mode baseline56 --local-only")
    else:
        try:
            from src.common.nvidia import available_models

            models = available_models()
            report.add(PASS if models else WARN, "nvidia api",
                       f"{len(models)} model(s) reachable"
                       if models else "key set but no model answered",
                       "" if models else "check the key is still valid: "
                                         "python -m src.common.nvidia --models")
        except Exception as exc:  # noqa: BLE001
            report.add(WARN, "nvidia api", f"probe failed: "
                                           f"{type(exc).__name__}",
                       "python -m src.common.nvidia --models")
    if not present.get("gemini"):
        report.add(WARN, "gemini api", "no key; the live Gemini pipeline is "
                                       "unavailable",
                   "optional; nothing in the offline evaluation needs it")
    else:
        report.add(PASS, "gemini api", "key present (not spent on a probe: the "
                                       "free tier is 20 requests/day)")


def check_writable(report: Report) -> None:
    bad = []
    for name in WRITE_DIRS:
        d = Path(name)
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".startup_check_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{name} ({type(exc).__name__})")
    if bad:
        report.add(FAIL, "writable dirs", "cannot write: " + ", ".join(bad),
                   "run from a directory you own, or point the runner "
                   "elsewhere with --out-dir")
    else:
        report.add(PASS, "writable dirs", ", ".join(WRITE_DIRS) + " all writable")


def check_offline_evaluation(report: Report) -> None:
    """The scripted evaluation needs no model at all. If this works, the
    research pipeline is runnable on this machine whatever else failed."""
    try:
        from src.eval.experiment import run_cell

        rows = run_cell("A", True, workdir=Path("data/runs/startup-check"))
        ours = [r for r in rows if r.method == "CausalLine"]
        if ours and ours[0].unsafe_preservations == 0:
            report.add(PASS, "offline evaluation",
                       f"scripted matrix cell ran: {len(rows)} method rows, "
                       f"work preserved {ours[0].work_preserved:.1%}, "
                       f"unsafe {ours[0].unsafe_preservations}")
        else:
            report.add(FAIL, "offline evaluation",
                       "cell ran but produced no CausalLine row",
                       "python -m pytest tests/ -x")
    except Exception as exc:  # noqa: BLE001
        report.add(FAIL, "offline evaluation", f"{type(exc).__name__}: {exc}",
                   "python -m pytest tests/ -x")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local-only", action="store_true",
                        help="skip the Gemini/NVIDIA probes")
    parser.add_argument("--probe", action="store_true",
                        help="send one real prompt to the local model")
    parser.add_argument("--skip-offline", action="store_true",
                        help="skip the scripted-matrix smoke test (~5s)")
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON")
    args = parser.parse_args()

    from_file = read_env_file()
    report = Report()

    check_python(report)
    check_core_imports(report)
    check_optional_deps(report)
    base = check_ollama_service(report)
    model = check_ollama_model(report, base)
    check_local_inference(report, base, model, args.probe)
    check_hardware(report)
    check_gpu(report)
    present = check_env_vars(report, from_file)
    check_external_apis(report, present, args.local_only)
    check_writable(report)
    if not args.skip_offline:
        check_offline_evaluation(report)

    if args.json:
        print(json.dumps({"rows": report.rows,
                          "pass": report.count(PASS),
                          "warn": report.count(WARN),
                          "fail": report.count(FAIL)}, indent=2))
    else:
        print("CausalLine startup check")
        print("=" * 74)
        print(report.render())
        print("=" * 74)
        print(f"  {report.count(PASS)} pass, {report.count(WARN)} warn, "
              f"{report.count(FAIL)} fail")
        fails = [r for r in report.rows if r["status"] == FAIL]
        if not fails:
            print("\n  Nothing blocking. Next:")
            print("    python -m src.eval.college_runner --mode baseline56 "
                  "--local-only --seeds 1 --smoke")
        else:
            print("\n  Blocking:")
            for r in fails:
                print(f"    - {r['check']}: {r['detail']}")
                if r["fix"]:
                    print(f"      {r['fix']}")
            print("\n  The scripted evaluation needs no model and no key; if "
                  "'offline evaluation' passed,\n  the research pipeline runs "
                  "here regardless of the failures above.")
    return 1 if report.count(FAIL) else 0


if __name__ == "__main__":
    sys.exit(main())
