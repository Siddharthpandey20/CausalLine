"""The industry-validation campaign: one runner, seven architectures, five methods.

    python -m experiments.industry_validation setup
    python -m experiments.industry_validation preflight --config configs/industry_validation.json
    python -m experiments.industry_validation run --config configs/industry_validation.json

CONFIGURATION IS JSON, NOT YAML, AND THAT IS DELIBERATE
--------------------------------------------------------
This repository has zero third-party dependencies in its core and the ground
rules say to ask before adding one. YAML needs PyYAML; JSON is in the standard
library. Nothing about the campaign needs YAML's expressiveness, so the config
is JSON and the college machine installs nothing extra.

WHAT IS PAIRED, AND HOW THAT IS ENFORCED
-----------------------------------------
One placement produces ONE original trace. All five recovery methods are then
scored against THAT trace, with THAT detector verdict and fresh copies of THAT
scenario's fixtures. The runner never generates a second original for a second
method -- `_recover_all` takes the trace as an argument, so there is no code
path that could.

RESUME IS PER (PLACEMENT, METHOD)
----------------------------------
Every completed unit appends one line to `checkpoints/units.jsonl`. On restart
the runner reads that ledger and skips what is in it. A unit is written only
after its result is on disk, so an interrupted unit is re-run rather than
assumed done -- and an interrupted write cannot leave a half-record that reads
as success, because the line is appended atomically after the result file is
flushed and a malformed trailing line is discarded with a warning.

Nothing is ever silently skipped: a placement that cannot be served is recorded
with `status: "deferred"` and the campaign pauses.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eval.arch_scenarios import LEVELS, ArchScenario
from src.eval.baselines import (
    b0_full_restart,
    b1_agent_taint,
    b2_topology_closure,
    discarded_events,
)
from src.eval.experiment import run_baseline_recovery
from src.eval.real_llm import ground_truth
from src.eval.routing import (
    AllProvidersFailed,
    CallLedger,
    FallbackRouter,
    ProviderLimits,
    ProviderState,
    prices_from_env,
)
from src.recovery.causalline import recover
from src.recovery.irec import recover_irec
from src.recovery.replay import pipeline_model_events
from src.tracing.architectures import architecture, names
from src.tracing.checkpoints import CheckpointStore, checkpoint_path_for
from src.tracing.logger import read_trace
from src.tracing.pipeline import run_pipeline
from src.tracing.tools import Tools

ARTIFACT_ROOT = Path("artifacts/industry_validation")
LATEST = ARTIFACT_ROOT / "LATEST"
DEFAULT_CONFIG = Path("configs/industry_validation.json")
METHODS = ("B0", "B1", "B2", "CausalLine", "IREC")


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "architectures": ["A", "B", "C", "D", "E", "F", "G"],
    "levels": list(LEVELS),
    "trials": 3,
    "seeds": [20260917, 20260918, 20260919, 20260920, 20260921],
    "methods": list(METHODS),
    "detector": "oracle",
    "stub": False,
    "provider_shares": {"local": 10, "gemini": 8, "nvidia": 2},
    "fallback_chain": ["local", "nvidia", "gemini"],
    "providers": {
        "gemini": {"rpm": 10, "rpd": 1500, "tpm": 250000, "attempts": 3,
                   "max_usd": 5.0,
                   "source": "CONFIGURE ME: read the real limits for this "
                             "project in AI Studio before launching"},
        # max_usd is NULL, not 0.0. A cap of 0.0 means "no spend allowed",
        # and because NVIDIA's price table is 0.00 the ledger never rises
        # above it -- so every call was refused and fell back to local. Null
        # means "no cap"; set a number to impose one.
        "nvidia": {"rpm": 40, "rpd": 10000, "attempts": 3, "max_usd": None,
                   "source": "CONFIGURE ME: check the account's actual limits"},
        "local": {"rpm": None, "rpd": None, "attempts": 2,
                  "source": "local inference; no provider quota"},
    },
    "max_total_usd": 10.0,
    "ollama_model": "llama3:latest",
}


def load_config(path: Path | None) -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULTS))
    if path and path.exists():
        user = json.loads(path.read_text(encoding="utf-8"))
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
    return config


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def git_dirty() -> bool:
    try:
        out = subprocess.run(["git", "status", "--porcelain"],
                             capture_output=True, text=True, timeout=10).stdout
        return bool(out.strip())
    except Exception:  # noqa: BLE001
        return True


# --------------------------------------------------------------------------
# the run directory
# --------------------------------------------------------------------------


@dataclass
class RunDir:
    """Everything one campaign writes, in one place."""

    root: Path

    def __post_init__(self) -> None:
        for sub in ("config", "traces", "recovered", "results", "checkpoints",
                    "logs", "providers", "reports"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    @property
    def units(self) -> Path:
        return self.root / "checkpoints" / "units.jsonl"

    @property
    def errors(self) -> Path:
        return self.root / "logs" / "errors.jsonl"

    @property
    def calls(self) -> Path:
        return self.root / "providers" / "calls.jsonl"

    def result(self, placement: str, method: str) -> Path:
        return self.root / "results" / f"{placement}--{method}.json"

    def append(self, path: Path, record: dict) -> None:
        """Append one JSON line, flushed, so an interrupted process leaves at
        worst a truncated trailing line that the reader discards."""
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def write_atomic(self, path: Path, payload: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(path)

    def completed(self) -> set[tuple[str, str]]:
        """Units already finished, from the ledger.

        A malformed trailing line is discarded with a warning rather than
        crashing the resume: a process killed mid-append is the normal case
        this has to survive.
        """
        done: set[tuple[str, str]] = set()
        if not self.units.exists():
            return done
        bad = 0
        for line in self.units.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if record.get("status") == "ok":
                # Only a unit whose result file still exists counts as done,
                # so a deleted artifact is re-run rather than assumed.
                if self.result(record["placement"], record["method"]).exists():
                    done.add((record["placement"], record["method"]))
        if bad:
            print(f"  [resume] discarded {bad} incomplete checkpoint line(s)")
        return done


# --------------------------------------------------------------------------
# progress
# --------------------------------------------------------------------------


@dataclass
class Progress:
    """Live terminal progress, computed from completed records only."""

    total_placements: int
    total_units: int
    done_units: int = 0
    started: float = field(default_factory=time.time)
    architecture: str = ""
    level: str = ""
    trial: str = ""
    placement: str = ""
    agents: str = ""
    method: str = ""
    method_state: dict[str, str] = field(default_factory=dict)
    placements_done: int = 0

    def render(self, router: FallbackRouter | None, config: dict) -> str:
        elapsed = time.time() - self.started
        rate = (self.done_units / elapsed) if elapsed > 0 and self.done_units else 0
        remaining = (self.total_units - self.done_units)
        eta = (remaining / rate / 3600) if rate > 0 else float("nan")
        lines = ["", "=" * 68, "INDUSTRY VALIDATION", "=" * 68,
                 f"Architecture : {self.architecture}",
                 f"Contamination: {self.level}",
                 f"Trial        : {self.trial}",
                 f"Placement    : {self.placement}",
                 f"Active agents: {self.agents}", "", "Recovery:"]
        for method in config["methods"]:
            lines.append(f"  {method:<12}{self.method_state.get(method, 'Pending')}")
        lines += ["",
                  f"Placements   : {self.placements_done}/{self.total_placements}",
                  f"Recovery runs: {self.done_units}/{self.total_units}"]
        if router is not None:
            lines.append("")
            lines.append("Provider usage (assigned -> actual):")
            for name, state in sorted(router.states.items()):
                data = state.to_dict()
                lines.append(
                    f"  {name:<8}{data['requests']:>6} calls "
                    f"{data['total_tokens']:>9} tok  ${data['usd']:>8.4f}"
                    f"  fail {data['failures']:>3}  refused {data['refusals']:>3}"
                    f"  fallback in/out {data['fallbacks_in']}/{data['fallbacks_out']}"
                    + ("  [CIRCUIT OPEN]" if data["circuit_open"] else ""))
            totals = router.ledger.totals()
            cap = config.get("max_total_usd")
            lines += [
                f"  attempts {totals['total_attempts']}, "
                f"fallbacks {totals['fallbacks']}, "
                f"failed {totals['failed']}, deferred {totals['deferred']}",
                f"  spend ${totals['usd']:.4f}"
                + (f" of ${cap:.2f} cap (${cap - totals['usd']:.4f} left)"
                   if cap else ""),
            ]
        lines.append(f"Elapsed {elapsed/3600:.2f} h; "
                     + (f"estimated remaining {eta:.2f} h" if rate > 0
                        else "estimated remaining n/a"))
        lines.append("=" * 68)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# provider wiring
# --------------------------------------------------------------------------


def build_router(config: dict, scenario: ArchScenario, run: RunDir,
                 key_prefix: str, ledger: CallLedger) -> FallbackRouter:
    """A router whose assigned route follows the architecture's own spec."""
    clients: dict[str, Any] = {}
    if config.get("stub"):
        from src.eval.arch_stub import DepotStub

        for name in ("local", "gemini", "nvidia"):
            clients[name] = DepotStub()
    else:
        from src.common.local_llama import LocalLlamaClient, LocalLlamaSettings

        clients["local"] = LocalLlamaClient(LocalLlamaSettings.from_env(
            model=config.get("ollama_model") or "llama3:latest",
            max_output_tokens=300))
        try:
            from src.eval.provider_budget import (
                BudgetLedger, ProviderLimit, gemini_client, nvidia_client)

            gl = BudgetLedger(limit=ProviderLimit(name="gemini"))
            clients["gemini"] = gemini_client("gemini-3.8-flash", gl)
            nl = BudgetLedger(limit=ProviderLimit(name="nvidia"))
            clients["nvidia"] = nvidia_client("nemotron", nl)
        except Exception as exc:  # noqa: BLE001
            # A missing key must never stop a local-only run, and it must never
            # be papered over either: it is recorded in the run's manifest.
            run.append(run.errors, {
                "stage": "router", "error": f"{type(exc).__name__}: {exc}",
                "note": "a hosted provider could not be constructed; its "
                        "share will fall back"})

    route = {agent.id: agent.provider for agent in scenario.arch.agents}
    router = FallbackRouter(
        clients=clients, route=route, default="local",
        chain=tuple(config.get("fallback_chain") or ("local", "nvidia", "gemini")),
        ledger=ledger, prices=prices_from_env(), key_prefix=key_prefix,
        total_usd_cap=config.get("max_total_usd"))
    for name, limits in (config.get("providers") or {}).items():
        if name in router.states:
            router.states[name] = ProviderState(
                limits=ProviderLimits.from_dict(name, limits))
    return router


# --------------------------------------------------------------------------
# one placement
# --------------------------------------------------------------------------


def run_placement(config: dict, run: RunDir, arch_key: str, level: str,
                  seed: int, progress: Progress, ledger: CallLedger,
                  done: set[tuple[str, str]]) -> dict:
    scenario = ArchScenario.build(arch_key, level, seed=seed)
    problems = scenario.validate()
    placement_id = f"{scenario.arch.name}--{level}--{seed}"
    progress.architecture = scenario.arch.name
    progress.level = level
    progress.placement = placement_id
    progress.agents = f"0/{scenario.arch.agent_count}"
    progress.method_state = {m: "Pending" for m in config["methods"]}

    if problems:
        run.append(run.errors, {"placement": placement_id,
                                "stage": "scenario", "problems": problems})
        raise SystemExit(f"{placement_id} is not runnable: {problems}")

    trace_path = run.root / "traces" / f"{placement_id}.jsonl"
    manifest = run.root / "traces" / f"{placement_id}.scenario.json"

    # --- the ORIGINAL execution, once per placement ----------------------
    router = build_router(config, scenario, run, f"{placement_id}/original/",
                          ledger)
    if trace_path.exists() and manifest.exists():
        original = read_trace(trace_path)
        original_result = json.loads(manifest.read_text(encoding="utf-8"))
    else:
        tools = scenario.apply(Tools.from_fixtures(
            memory_path=run.root / "traces" / f"{placement_id}.memory.json"))
        result = run_pipeline(trace_path, task=scenario.task, client=router,
                              tools=tools, attributor=None,
                              handoff_hook=scenario.handoff_hook(),
                              **scenario.workflow_kwargs)
        original = read_trace(trace_path)
        original.validate()
        original_result = {
            "placement": placement_id,
            "architecture": scenario.arch.name,
            "source_id": scenario.arch.source_id,
            "level": level, "seed": seed,
            "task_success": bool(result.task_success),
            "events": len(original.events),
            "model_events": len(pipeline_model_events(original)),
            "agents_configured": result.detail["agents_configured"],
            "agents_executed": result.detail["agents_executed"],
            # Counted from the trace, never from the spec.
            "agents_in_trace": len({e.agent_id for e in original.events}
                                   - {"executor"}),
            "scenario": scenario.to_dict(),
            "channel_note": scenario.channel_note(),
            "routing_assigned": {a.id: a.provider for a in scenario.arch.agents},
            "routing_actual": dict(router.per_provider_calls),
            "degraded": list(router.degraded),
            "providers": router.budget_state(),
            "stub": bool(config.get("stub")),
        }
        run.write_atomic(manifest, original_result)
    progress.agents = (f"{original_result['agents_executed']}"
                       f"/{original_result['agents_configured']}")

    flagged = sorted({s.id for s in original.sources if s.malicious})
    if not flagged:
        from src.eval.attacks import label_malicious
        flagged = sorted(label_malicious(trace_path, scenario.marker))
    truth_events, truth = ground_truth(original, flagged, scenario.token)
    closure = set(b2_topology_closure(original, flagged))

    # --- all five methods, on THAT trace ---------------------------------
    for method in config["methods"]:
        if (placement_id, method) in done:
            progress.method_state[method] = "Complete (resumed)"
            continue
        progress.method = method
        progress.method_state[method] = "Running"
        print(progress.render(router, config), flush=True)
        started = time.time()
        try:
            record = _recover_one(config, run, scenario, original, flagged,
                                  truth_events, truth, closure, method,
                                  placement_id, ledger)
            record["wall_clock_s"] = round(time.time() - started, 2)
            run.write_atomic(run.result(placement_id, method), record)
            run.append(run.units, {"placement": placement_id, "method": method,
                                   "status": "ok",
                                   "at": datetime.now(timezone.utc).isoformat()})
            progress.method_state[method] = "Complete"
            progress.done_units += 1
        except AllProvidersFailed as exc:
            run.append(run.units, {"placement": placement_id, "method": method,
                                   "status": "deferred", "reason": str(exc)[:400],
                                   "at": datetime.now(timezone.utc).isoformat()})
            run.append(run.errors, {"placement": placement_id, "method": method,
                                    "stage": "recovery", "deferred": str(exc)[:600],
                                    "record": exc.record.to_dict()})
            progress.method_state[method] = "DEFERRED"
            raise
        except Exception as exc:  # noqa: BLE001
            run.append(run.units, {"placement": placement_id, "method": method,
                                   "status": "failed",
                                   "error": f"{type(exc).__name__}: {exc}"[:400],
                                   "at": datetime.now(timezone.utc).isoformat()})
            run.append(run.errors, {"placement": placement_id, "method": method,
                                    "stage": "recovery",
                                    "error": f"{type(exc).__name__}: {exc}",
                                    "traceback": traceback.format_exc()[-2000:]})
            progress.method_state[method] = f"FAILED ({type(exc).__name__})"
    progress.placements_done += 1
    return original_result


def _recover_one(config: dict, run: RunDir, scenario: ArchScenario, original,
                 flagged: list[str], truth_events: set[str], truth: Any,
                 closure: set[str], method: str, placement_id: str,
                 ledger: CallLedger) -> dict:
    """One method, on the given original trace and verdict."""
    out = run.root / "recovered" / f"{placement_id}--{method}.jsonl"
    router = build_router(config, scenario, run,
                          f"{placement_id}/{method}/", ledger)
    tools = scenario.apply(Tools.from_fixtures(
        memory_path=run.root / "recovered" / f"{placement_id}--{method}.mem.json"))
    base = {"placement": placement_id, "method": method,
            "architecture": scenario.arch.name, "level": scenario.level,
            "seed": scenario.seed, "truth_events": len(truth_events),
            "payload_landed": truth.payload_landed,
            "events": len(original.events)}

    if method in ("B0", "B1", "B2"):
        discard = {"B0": b0_full_restart, "B1": b1_agent_taint,
                   "B2": b2_topology_closure}[method](original, flagged)
        label = {"B0": "B0 full restart", "B1": "B1 agent taint",
                 "B2": "B2 topology closure"}[method]
        result, report = run_baseline_recovery(
            label, discard, original, router, list(flagged), None, out,
            tools=tools, handoff_hook=scenario.handoff_hook())
        discarded = set(discarded_events(report, discard))
        base.update({"replay_tokens": report.replay_tokens,
                     "analysis_tokens": 0,
                     "task_success": bool(result.task_success),
                     "verified": None, "escalations": 0,
                     "replayed": len(report.replayed),
                     "spliced": len(report.spliced)})
    elif method == "CausalLine":
        result = recover(original, flagged, router, out, tools=tools,
                         handoff_hook=scenario.handoff_hook(),
                         checkpoints=CheckpointStore.load(
                             checkpoint_path_for(original.path)))
        discarded = set(discarded_events(result.report, result.invalidated))
        base.update({"replay_tokens": result.replay_tokens,
                     "analysis_tokens": result.analysis_tokens,
                     "task_success": bool(result.task_success),
                     "verified": bool(result.verification.ok),
                     "verify_reasons": list(result.verification.reasons),
                     "escalations": result.escalations,
                     "scope": result.scope, "notes": list(result.notes)})
    elif method == "IREC":
        result = recover_irec(original, flagged, router, out,
                              task=scenario.task, tools=tools,
                              handoff_hook=scenario.handoff_hook(),
                              workflow_kwargs=scenario.workflow_kwargs)
        # OUTCOME-based discard set. `discarded_events` returns IREC's
        # re-executed MODEL events, which is not comparable with B2's full
        # closure and reports false unsafe preservations -- see
        # docs/research/IREC_CAMPAIGN56.md section 2.
        recovered = read_trace(Path(result.recovered_path))
        payloads = [s.content for s in original.sources if s.id in set(flagged)]
        discarded = set()
        for event in original.events:
            text = original.output_text(event.id) or ""
            if recovered.has_event(event.id):
                if text != (recovered.output_text(event.id) or ""):
                    discarded.add(event.id)
            else:
                discarded.add(event.id)
            if any(p and len(p) >= 12 and p in text for p in payloads):
                discarded.add(event.id)
        base.update({"replay_tokens": result.replay_tokens,
                     "analysis_tokens": 0,
                     "task_success": bool(result.task_success),
                     "verified": bool(result.verification.ok),
                     "verify_reasons": list(result.verification.reasons),
                     "escalations": 0, "scope": result.scope,
                     "spliced": result.spliced, "reexecuted": result.calls,
                     "cutoff_kept": result.unchanged,
                     "notes": list(result.notes)})
    else:
        raise ValueError(f"unknown method {method!r}")

    base.update({
        "discarded": len(discarded),
        "work_preserved_num": len(original.events) - len(discarded),
        "work_preserved_den": len(original.events),
        "unsafe_num": len(truth_events - discarded),
        "unsafe_den": len(truth_events),
        "recall_num": len(truth_events & discarded),
        "recall_den": len(truth_events),
        "escapes_num": len(truth_events - closure),
        "escapes_den": len(truth_events),
        "routing_actual": dict(router.per_provider_calls),
        "degraded": list(router.degraded),
        "providers": router.budget_state(),
        "usd": round(sum(s.usd for s in router.states.values()), 6),
    })
    for record in ledger.records:
        if record.key.startswith(f"{placement_id}/{method}/"):
            run.append(run.calls, record.to_dict())
    return base


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    DEFAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    if DEFAULT_CONFIG.exists() and not args.force:
        print(f"{DEFAULT_CONFIG} exists; pass --force to overwrite")
    else:
        DEFAULT_CONFIG.write_text(json.dumps(DEFAULTS, indent=2) + "\n",
                                  encoding="utf-8")
        print(f"wrote {DEFAULT_CONFIG}")
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"artifact root ready: {ARTIFACT_ROOT}")
    print("\nNext:")
    print("  1. cp .env.example .env   and fill in the keys you have")
    print("  2. EDIT configs/industry_validation.json -- the provider limits "
          "say CONFIGURE ME and must be set to this project's real quotas")
    print("  3. python -m experiments.industry_validation preflight "
          "--config configs/industry_validation.json")
    return 0


def plan(config: dict) -> list[tuple[str, str, int]]:
    seeds = config["seeds"][:config["trials"]]
    if len(seeds) < config["trials"]:
        raise SystemExit(f"config asks for {config['trials']} trials but lists "
                         f"{len(config['seeds'])} seeds")
    return [(a, level, seed) for a in config["architectures"]
            for level in config["levels"] for seed in seeds]


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.stub:
        config["stub"] = True
    if args.trials:
        config["trials"] = args.trials
    if args.architectures:
        config["architectures"] = list(args.architectures)
    if args.levels:
        config["levels"] = list(args.levels)

    for key in config["architectures"]:
        problems = architecture(key).problems()
        if problems:
            raise SystemExit(f"architecture {key} is not runnable: {problems}")

    placements = plan(config)
    run_id = args.run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-" + git_commit()[:8] + ("-stub" if config.get("stub") else ""))
    run = RunDir(ARTIFACT_ROOT / run_id)
    run.write_atomic(run.root / "config" / "config.json", config)
    run.write_atomic(run.root / "config" / "environment.json", {
        "run_id": run_id,
        "git_commit": git_commit(),
        "git_dirty": git_dirty(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stub": bool(config.get("stub")),
        "architectures": {k: architecture(k).to_dict()
                          for k in config["architectures"]},
        "note": ("STUB RUN -- infrastructure exercise only, not a measurement"
                 if config.get("stub") else "live providers"),
    })
    shutil.copy("docs/industry_validation/SOURCES.md",
                run.root / "config" / "SOURCES.md")
    LATEST.write_text(run_id + "\n", encoding="utf-8")

    done = run.completed()
    total_units = len(placements) * len(config["methods"])
    progress = Progress(total_placements=len(placements),
                        total_units=total_units, done_units=len(done))
    print(f"run id    : {run_id}")
    print(f"artifacts : {run.root}")
    print(f"placements: {len(placements)}   units: {total_units}   "
          f"already done: {len(done)}")
    if config.get("stub"):
        print("MODE      : STUB -- infrastructure exercise, NOT a measurement")

    ledger = CallLedger()
    paused = None
    for index, (arch_key, level, seed) in enumerate(placements, start=1):
        progress.trial = f"{index}/{len(placements)}"
        try:
            run_placement(config, run, arch_key, level, seed, progress,
                          ledger, done)
        except AllProvidersFailed as exc:
            paused = str(exc)
            break
        except KeyboardInterrupt:
            paused = "interrupted by the operator"
            break

    summary = aggregate(run, config, ledger, paused)
    run.write_atomic(run.root / "reports" / "summary.json", summary)
    print(render_summary(summary))
    if paused:
        print(f"\nCAMPAIGN PAUSED: {paused}")
        print(f"Resume with the same command and --run-id {run_id}")
        return 2
    return 0 if summary["complete"] else 3


def aggregate(run: RunDir, config: dict, ledger: CallLedger,
              paused: str | None) -> dict:
    results = []
    for path in sorted((run.root / "results").glob("*.json")):
        try:
            results.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    expected = len(plan(config)) * len(config["methods"])
    by_method: dict[str, dict[str, Any]] = {}
    for record in results:
        slot = by_method.setdefault(record["method"], {
            "n": 0, "unsafe": 0, "truth": 0, "recall": 0, "escapes": 0,
            "task_ok": 0, "tokens": 0, "usd": 0.0, "kept": 0, "events": 0})
        slot["n"] += 1
        slot["unsafe"] += record.get("unsafe_num", 0)
        slot["truth"] += record.get("unsafe_den", 0)
        slot["recall"] += record.get("recall_num", 0)
        slot["escapes"] += record.get("escapes_num", 0)
        slot["task_ok"] += 1 if record.get("task_success") else 0
        slot["tokens"] += (record.get("replay_tokens", 0)
                           + record.get("analysis_tokens", 0))
        slot["usd"] += record.get("usd", 0.0)
        slot["kept"] += record.get("work_preserved_num", 0)
        slot["events"] += record.get("work_preserved_den", 0)
    return {
        "run_id": run.root.name,
        "complete": len(results) == expected and not paused,
        "status": ("INCOMPLETE" if (len(results) != expected or paused)
                   else "COMPLETE"),
        "paused": paused,
        "units_expected": expected,
        "units_present": len(results),
        "stub": bool(config.get("stub")),
        "by_method": by_method,
        "providers": ledger.totals(),
    }


def render_summary(summary: dict) -> str:
    out = ["", "=" * 76,
           f"CAMPAIGN {summary['status']}  --  run {summary['run_id']}",
           "=" * 76,
           f"units {summary['units_present']}/{summary['units_expected']}"]
    if summary["stub"]:
        out.append("STUB RUN -- infrastructure exercise, NOT a measurement")
    out.append(f"{'method':<12}{'n':>4}{'unsafe':>10}{'recall':>12}"
               f"{'escapes':>9}{'task':>8}{'tokens':>10}{'work kept':>14}")
    for method, slot in sorted(summary["by_method"].items()):
        out.append(
            f"{method:<12}{slot['n']:>4}"
            f"{slot['unsafe']:>5}/{slot['truth']:<4}"
            f"{slot['recall']:>7}/{slot['truth']:<4}"
            f"{slot['escapes']:>4}/{slot['truth']:<4}"
            f"{slot['task_ok']:>5}/{slot['n']:<2}"
            f"{slot['tokens']:>10}"
            f"{slot['kept']:>8}/{slot['events']:<5}")
    totals = summary["providers"]
    out += ["", f"invocations {totals['invocations']}, attempts "
                f"{totals['total_attempts']}, fallbacks {totals['fallbacks']}, "
                f"failed {totals['failed']}, deferred {totals['deferred']}",
            f"spend ${totals['usd']:.4f}"]
    for provider, slot in totals["by_provider"].items():
        out.append(f"  {provider:<8}{slot['attempts']:>6} attempts "
                   f"{slot['ok']:>6} ok  {slot['prompt_tokens']:>9} in "
                   f"{slot['output_tokens']:>8} out  ${slot['usd']:>9.4f}")
    out.append("=" * 76)
    return "\n".join(out)


def cmd_status(args: argparse.Namespace) -> int:
    run_id = args.run_id
    if not run_id and LATEST.exists():
        run_id = LATEST.read_text(encoding="utf-8").strip()
    if not run_id:
        print("no run id and no LATEST pointer")
        return 1
    run = RunDir(ARTIFACT_ROOT / run_id)
    report = run.root / "reports" / "summary.json"
    if report.exists():
        print(render_summary(json.loads(report.read_text(encoding="utf-8"))))
    done = run.completed()
    print(f"\ncompleted units on disk: {len(done)}")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    from experiments.preflight import main as preflight_main

    return preflight_main(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m experiments.industry_validation",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="write the default config")
    setup.add_argument("--force", action="store_true")
    setup.set_defaults(func=cmd_setup)

    pre = sub.add_parser("preflight", help="check this machine")
    pre.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    pre.add_argument("--probe", action="store_true",
                     help="send one real request to each provider")
    pre.add_argument("--json", action="store_true")
    pre.set_defaults(func=cmd_preflight)

    run = sub.add_parser("run", help="run the campaign")
    run.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    run.add_argument("--run-id", help="resume an existing run")
    run.add_argument("--stub", action="store_true",
                     help="deterministic stand-in agents; NOT a measurement")
    run.add_argument("--trials", type=int)
    run.add_argument("--architectures", nargs="*")
    run.add_argument("--levels", nargs="*", choices=list(LEVELS))
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="report a run's progress")
    status.add_argument("--run-id")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
