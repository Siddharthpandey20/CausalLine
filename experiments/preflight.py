"""Does the college machine have what the campaign needs?

    python -m experiments.industry_validation preflight --config configs/industry_validation.json
    python -m experiments.industry_validation preflight --probe     # one real call each

Extends `startup_check.py` rather than replacing it: that script answers "can
this machine run the research pipeline at all", and this one answers "can it run
THIS campaign, at this configured scale, inside these quotas".

It FAILS rather than warns on anything that would stop the campaign, because
the point is to refuse before the GPU time is spent, not after. A detected GPU
is not sufficient: the configured model has to load and answer.

No key is ever printed. Presence and character count only.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

# What the campaign needs. Stated here so a failure can say what was expected.
MIN_PYTHON = (3, 11)
MIN_RAM_GB = 12.0
MIN_DISK_GB = 20.0
MIN_VRAM_GB = 6.0          # llama3 8B at Q4 needs ~5.5 GB resident


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, level: str, name: str, detail: str, fix: str = "") -> None:
        self.rows.append((level, name, detail, fix))

    @property
    def failures(self) -> list[tuple[str, str, str, str]]:
        return [r for r in self.rows if r[0] == FAIL]

    def render(self) -> str:
        width = max((len(r[1]) for r in self.rows), default=10) + 2
        out = ["", "=" * 74, "INDUSTRY VALIDATION PREFLIGHT", "=" * 74]
        for level, name, detail, fix in self.rows:
            out.append(f"  {level:<5}{name:<{width}}{detail}")
            if fix and level == FAIL:
                out.append(f"       {'':<{width}}-> {fix}")
        counts = {k: sum(1 for r in self.rows if r[0] == k)
                  for k in (PASS, WARN, FAIL)}
        out += ["=" * 74,
                f"  {counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail"]
        if self.failures:
            out += ["", "  BLOCKING:"]
            for _l, name, detail, fix in self.failures:
                out.append(f"    - {name}: {detail}")
                if fix:
                    out.append(f"      {fix}")
            out.append("")
            out.append("  The campaign will not start until these are fixed.")
        else:
            out.append("  Ready to launch.")
        return "\n".join(out)

    def to_dict(self) -> dict[str, Any]:
        return {"rows": [{"level": l, "check": n, "detail": d, "fix": f}
                         for l, n, d, f in self.rows],
                "ok": not self.failures}


# --------------------------------------------------------------------------
# hardware
# --------------------------------------------------------------------------


def _ram_gb() -> float | None:
    try:
        if platform.system() == "Windows":
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
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = Status()
            status.dwLength = ctypes.sizeof(Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return status.ullTotalPhys / 1024 ** 3
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 ** 2
    except Exception:  # noqa: BLE001
        return None
    return None


def check_hardware(report: Report) -> None:
    cores = os.cpu_count() or 0
    ram = _ram_gb()
    if ram is None:
        report.add(WARN, "system RAM", "could not be determined")
    elif ram < MIN_RAM_GB:
        report.add(FAIL, "system RAM", f"{ram:.1f} GB, campaign needs "
                                       f"{MIN_RAM_GB:.0f} GB",
                   "close other work, or reduce the configured concurrency")
    else:
        report.add(PASS, "system RAM", f"{ram:.1f} GB, {cores} logical cores")

    usage = shutil.disk_usage(Path.cwd())
    free = usage.free / 1024 ** 3
    # Each placement writes an original trace plus five recovered traces, with
    # content and checkpoint sidecars: measured at roughly 2.5 MB per
    # placement for the 56-agent workload.
    if free < MIN_DISK_GB:
        report.add(FAIL, "free disk", f"{free:.1f} GB on {Path.cwd().anchor}, "
                                      f"campaign needs {MIN_DISK_GB:.0f} GB",
                   "free space, or point the artifact root at another volume")
    else:
        report.add(PASS, "free disk", f"{free:.1f} GB available")


def check_gpu(report: Report, model: str) -> dict[str, Any]:
    """GPU model, VRAM, CUDA and driver. A detected GPU is not sufficient."""
    info: dict[str, Any] = {}
    smi = shutil.which("nvidia-smi")
    if not smi:
        report.add(WARN, "gpu", "no nvidia-smi; inference will run on CPU",
                   "install the NVIDIA driver if this machine has a GPU")
        return info
    try:
        out = subprocess.run(
            [smi, "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30)
        line = (out.stdout or "").strip().splitlines()
        if not line:
            report.add(WARN, "gpu", "nvidia-smi reported no device")
            return info
        parts = [x.strip() for x in line[0].split(",")]
        if len(parts) < 3 or not parts[1].replace(".", "", 1).isdigit():
            # nvidia-smi present but reporting no usable device: the header
            # line comes back without numbers. Seen on a machine whose GPU
            # discovery times out.
            report.add(WARN, "gpu", f"nvidia-smi reported no usable device "
                                    f"({line[0][:60]!r}); CPU inference",
                       "check the driver; the campaign will run but the local "
                       "share will be slow")
            return info
        name, vram_mb, driver = parts[0], parts[1], parts[2]
        vram = float(vram_mb) / 1024
        info = {"name": name, "vram_gb": round(vram, 1), "driver": driver}
        report.add(PASS, "gpu", f"{name}, driver {driver}")
        if vram < MIN_VRAM_GB:
            report.add(FAIL, "gpu VRAM",
                       f"{vram:.1f} GB, {model} needs about "
                       f"{MIN_VRAM_GB:.1f} GB resident",
                       f"use a smaller local model (set ollama_model in the "
                       f"config, e.g. llama3.2:3b) or a larger GPU")
        else:
            report.add(PASS, "gpu VRAM", f"{vram:.1f} GB")
    except Exception as exc:  # noqa: BLE001
        report.add(WARN, "gpu", f"nvidia-smi failed: {type(exc).__name__}")
        return info

    cuda = shutil.which("nvcc")
    try:
        smi_out = subprocess.run([smi], capture_output=True, text=True,
                                 timeout=30).stdout
        version = ""
        for token in smi_out.split():
            if token.count(".") == 1 and token[0].isdigit() and len(token) <= 5:
                version = token
        report.add(PASS, "cuda", f"driver reports CUDA {version or 'unknown'}"
                                 + (f"; nvcc at {cuda}" if cuda else
                                    "; no nvcc (not needed for Ollama)"))
        info["cuda"] = version
    except Exception:  # noqa: BLE001
        report.add(WARN, "cuda", "could not read the CUDA version")
    return info


# --------------------------------------------------------------------------
# software and providers
# --------------------------------------------------------------------------


def check_python(report: Report) -> None:
    if sys.version_info[:2] < MIN_PYTHON:
        report.add(FAIL, "python",
                   f"{platform.python_version()}, need "
                   f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}+",
                   "install a newer Python and recreate the virtualenv")
    else:
        report.add(PASS, "python", platform.python_version())
    try:
        import src.recovery.irec  # noqa: F401
        import src.tracing.archpipeline  # noqa: F401

        report.add(PASS, "core imports", "tracing, provenance, recovery, "
                                         "architectures, IREC all import")
    except Exception as exc:  # noqa: BLE001
        report.add(FAIL, "core imports", f"{type(exc).__name__}: {exc}",
                   "run from the repository root inside its virtualenv")
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10)
        report.add(PASS if not dirty.stdout.strip() else WARN,
                   "repository",
                   f"{branch.stdout.strip()} @ {head.stdout.strip()}"
                   + (" (uncommitted changes)" if dirty.stdout.strip() else ""))
    except Exception:  # noqa: BLE001
        report.add(WARN, "repository", "git not available")


def check_architectures(report: Report, config: dict) -> None:
    from src.eval.arch_scenarios import ArchScenario
    from src.tracing.architectures import architecture

    problems: list[str] = []
    for key in config["architectures"]:
        spec = architecture(key)
        problems += [f"{key}: {p}" for p in spec.problems()]
        for level in config["levels"]:
            problems += [f"{key}/{level}: {p}"
                         for p in ArchScenario.build(key, level,
                                                     config["seeds"][0]).validate()]
    if problems:
        report.add(FAIL, "architectures", "; ".join(problems[:3]),
                   "fix the specs before launching")
    else:
        total = sum(architecture(k).agent_count for k in config["architectures"])
        report.add(PASS, "architectures",
                   f"{len(config['architectures'])} runnable, "
                   f"{total} agents configured in total")


def check_ollama(report: Report, config: dict, probe: bool) -> None:
    from src.common.local_llama import LocalLlamaSettings

    settings = LocalLlamaSettings.from_env(
        model=config.get("ollama_model") or "llama3:latest")
    base = settings.endpoint.rstrip("/")
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=20) as handle:
            tags = json.loads(handle.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        report.add(FAIL, "ollama service", f"{base} unreachable: "
                                           f"{type(exc).__name__}",
                   "start it with: ollama serve   (or set OLLAMA_HOST)")
        return
    models = [m.get("name") for m in tags.get("models", [])]
    report.add(PASS, "ollama service", f"{base}, {len(models)} model(s)")
    if settings.model not in models:
        report.add(FAIL, "ollama model", f"{settings.model} not installed",
                   f"ollama pull {settings.model}")
        return
    report.add(PASS, "ollama model", settings.model)

    if not probe:
        report.add(WARN, "local model load",
                   "not probed; a detected GPU does not prove the model loads",
                   "re-run preflight with --probe")
        return
    # THE CHECK THAT MATTERS: does the configured model actually load and
    # answer? A GPU that exists but cannot hold the model fails here.
    from src.common.local_llama import LocalLlamaClient

    started = time.time()
    try:
        reply = LocalLlamaClient(settings).generate(
            "Reply with exactly: READY", system="Answer in one word.")
        took = time.time() - started
        report.add(PASS, "local model load",
                   f"{settings.model} answered in {took:.1f}s "
                   f"({reply.total_tokens} tokens)")
        if took > 25:
            report.add(WARN, "local speed",
                       f"{took:.1f}s for one short call suggests CPU "
                       f"inference; the campaign's local share will be slow")
    except Exception as exc:  # noqa: BLE001
        report.add(FAIL, "local model load",
                   f"{settings.model} did not answer: {type(exc).__name__}: "
                   f"{str(exc)[:160]}",
                   "check VRAM, or set a smaller ollama_model in the config")


def check_keys(report: Report) -> dict[str, bool]:
    from src.eval.provider_budget import load_env

    load_env()
    present: dict[str, bool] = {}
    for label, names in (("gemini", ("GEMINI_API_KEY",)),
                         ("nvidia", ("NVIDIA_API_KEY_1", "NVIDIA_API_KEY",))):
        found = [n for n in names
                 if os.environ.get(n, "").strip()
                 and "your-key-here" not in os.environ.get(n, "")]
        present[label] = bool(found)
        if found:
            # Presence and LENGTH only. Never the value.
            report.add(PASS, f"key: {label}",
                       f"{found[0]} set ({len(os.environ[found[0]].strip())} chars)")
        else:
            report.add(WARN, f"key: {label}",
                       f"not set; {label}'s share will fall back to another "
                       f"provider and every fallback is recorded")
    return present


def check_quota(report: Report, config: dict) -> None:
    """Can the configured quota fund the configured campaign?

    This is the check that matters most, because the campaign is quota-bound
    rather than cost-bound: 11,474 Gemini calls at three trials is 574 days of
    a 20-RPD free tier and 7.6 days at 1,500 RPD.
    """
    from src.eval.routing import PRICES
    from src.tracing.architectures import architecture

    calls_per_original = sum(architecture(k).model_call_count
                             for k in config["architectures"])
    # Measured per-method multipliers on the 56-agent workload.
    multiplier = {"B0": 1.00, "B1": 0.58, "B2": 0.58,
                  "CausalLine": 2.15, "IREC": 0.42}
    units = len(config["levels"]) * config["trials"]
    total = calls_per_original * units          # the originals
    for method in config["methods"]:
        total += calls_per_original * units * multiplier.get(method, 1.0)

    shares = config["provider_shares"]
    width = sum(shares.values()) or 1
    report.add(PASS, "campaign size",
               f"{len(plan_size(config))} placements, "
               f"{int(total):,} model calls estimated")
    blocking = False
    for provider, weight in shares.items():
        calls = total * weight / width
        limits = (config.get("providers") or {}).get(provider) or {}
        rpd = limits.get("rpd")
        tokens = calls * 197                     # measured tokens per call
        rate_in, rate_out = PRICES.get(provider, (0.0, 0.0))
        usd = (tokens * 0.9 / 1e6) * rate_in + (tokens * 0.1 / 1e6) * rate_out
        detail = (f"{calls:,.0f} calls, {tokens:,.0f} tokens, "
                  f"about ${usd:.2f}")
        if rpd:
            days = calls / rpd
            detail += f"; {days:.1f} day(s) of the configured {rpd} RPD"
            if days > 1.0:
                report.add(FAIL, f"quota: {provider}", detail,
                           f"raise {provider}'s real quota, reduce "
                           f"provider_shares.{provider}, or lower trials -- "
                           f"the campaign must not be silently reduced")
                blocking = True
                continue
        cap = limits.get("max_usd")
        if cap is not None and usd > cap:
            report.add(FAIL, f"budget: {provider}",
                       f"{detail}, over the configured ${cap:.2f} cap",
                       f"raise providers.{provider}.max_usd or reduce the share")
            blocking = True
            continue
        report.add(PASS, f"quota: {provider}", detail)
        if limits.get("source", "").startswith("CONFIGURE ME"):
            report.add(WARN, f"quota source: {provider}",
                       "the configured limit is still the placeholder; verify "
                       "the real project limits before launching")
    total_usd_cap = config.get("max_total_usd")
    if total_usd_cap is not None:
        report.add(PASS, "campaign spend cap", f"${total_usd_cap:.2f}")
    if not blocking:
        report.add(PASS, "quota headroom",
                   "the configured distribution fits the configured limits")


def plan_size(config: dict) -> list:
    return [(a, level, seed) for a in config["architectures"]
            for level in config["levels"]
            for seed in config["seeds"][:config["trials"]]]


def check_providers_live(report: Report, present: dict[str, bool],
                         probe: bool) -> None:
    if not probe:
        report.add(WARN, "provider connectivity",
                   "not probed", "re-run preflight with --probe")
        return
    if present.get("nvidia"):
        try:
            from src.common.nvidia import available_models

            models = available_models()
            report.add(PASS, "nvidia api", f"{len(models)} model(s) reachable")
        except Exception as exc:  # noqa: BLE001
            report.add(FAIL, "nvidia api", f"{type(exc).__name__}: "
                                           f"{str(exc)[:140]}",
                       "check NVIDIA_API_KEY_1 and network access")
    if present.get("gemini"):
        # Deliberately NOT spent on a probe by default: a free-tier project
        # has about 20 requests a day and the campaign needs them.
        report.add(WARN, "gemini api",
                   "key present; not probed to preserve the daily quota. "
                   "Verify the project's real RPM/RPD/TPM in AI Studio and "
                   "put them in the config")


def main(args: Any) -> int:
    from experiments.industry_validation import load_config

    config = load_config(getattr(args, "config", None))
    report = Report()
    check_python(report)
    check_hardware(report)
    gpu = check_gpu(report, config.get("ollama_model") or "llama3:latest")
    check_architectures(report, config)
    check_ollama(report, config, bool(getattr(args, "probe", False)))
    present = check_keys(report)
    check_providers_live(report, present, bool(getattr(args, "probe", False)))
    check_quota(report, config)

    if getattr(args, "json", False):
        print(json.dumps({**report.to_dict(), "gpu": gpu}, indent=1))
    else:
        print(report.render())
    return 1 if report.failures else 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path,
                        default=Path("configs/industry_validation.json"))
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--json", action="store_true")
    raise SystemExit(main(parser.parse_args()))
