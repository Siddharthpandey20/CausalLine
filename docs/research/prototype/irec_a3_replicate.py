"""Phase 1: replicate A3 under documented deterministic decoding.

The first A3 measurement reported 96.7% (87/90) byte-identical reproduction, of
which 79/82 came from the local 8B model. That model was ALREADY running under
Ollama's documented deterministic configuration -- `seed=0`, `temperature=0.0`,
`num_ctx=4096`, which `src/common/local_llama.py` sends on every request. So the
three failures are not a missing seed, and re-running with "determinism turned
on" would measure nothing new.

This replicates properly, and separates the three things that can make a reply
differ:

  (a) MODEL NONDETERMINISM -- identical prompt hash, identical system-prompt
      hash, identical decoding parameters, different output hash. The only
      finding that is about the provider.
  (b) PROMPT ASSEMBLY -- the prompt itself differs, because an upstream output
      differed and propagated. Not a provider property.
  (c) HARNESS ERROR -- the system prompt is missing or wrong. This is what made
      an earlier standalone probe report 0/12; it is excluded by construction
      here, because the prompts are captured FROM the production pipeline.

Capture is free. In gate mode the client receives every (prompt, system) pair
the pipeline composes and splices all of them, so no model call is made:

    python -m docs.research.prototype.irec_a3_replicate --capture \
        --seed 20260917 --regime small

Then each captured prompt is re-issued N times, byte-for-byte, with the system
prompt it was composed with:

    python -m docs.research.prototype.irec_a3_replicate --reissue \
        --repeats 5 --sample 12

Records model, provider, version, every decoding parameter, SHA-256 of prompt,
system prompt and each output, and every mismatch. No key is ever printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.common.llm import LLMResponse
from src.eval.mixed_campaign import build_ledgers, build_router
from src.eval.mixed_scenarios import MixedScenario
from src.eval.real_llm import _tools_for
from src.recovery.replay import pipeline_model_events
from src.tracing.logger import read_trace
from src.tracing.mixed import Topology
from src.tracing.pipeline import run_pipeline

PRIMARY = Path("data/results/mixed56-workload-varied")
OUT = Path("data/runs/irec-a3")


def sha(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def find_run(seed: int, regime: str) -> dict:
    for path in sorted(PRIMARY.glob("seed*.json")):
        for run in json.loads(path.read_text(encoding="utf-8")).get("results", []):
            if run.get("seed") == seed and run.get("regime") == regime:
                return run
    raise SystemExit(f"no saved run for {seed}/{regime}")


# --------------------------------------------------------------------------
# capture: free, no model call
# --------------------------------------------------------------------------


class CaptureClient:
    """Receives every (prompt, system) the pipeline composes, calls nothing.

    Splices every event with its recorded output, which is exactly what the
    fidelity gate does, so the pipeline runs to completion and every prompt is
    the one production would have issued.
    """

    def __init__(self, original):
        self.original = original
        self.queue = pipeline_model_events(original)
        self.i = 0
        self.captured: list[dict[str, Any]] = []
        self.total_tokens = 0
        self.throttled_s = 0.0

    def announce(self, agent_id, kind):
        self._pending = (agent_id, kind)

    def last_issued_prompt(self):
        return None

    def generate(self, prompt, system=None, json_output=False, temperature=None):
        event_id = self.queue[self.i] if self.i < len(self.queue) else f"extra{self.i}"
        self.i += 1
        event = (self.original.event(event_id)
                 if self.original.has_event(event_id) else None)
        original_output = self.original.output_text(event_id) or ""
        self.captured.append({
            "event": event_id,
            "agent": getattr(event, "agent_id", "?"),
            "kind": getattr(event, "kind", "?"),
            "prompt": prompt,
            "system": system,
            "json_output": bool(json_output),
            "original_output": original_output,
            "prompt_sha": sha(prompt),
            "system_sha": sha(system),
            "original_output_sha": sha(original_output),
            "prompt_chars": len(prompt or ""),
            "system_chars": len(system or ""),
        })
        return LLMResponse(text=original_output, model="capture", prompt_tokens=0,
                           output_tokens=0, thoughts_tokens=0, total_tokens=0,
                           attempts=1, latency_s=0.0, slept_s=0.0,
                           finish_reason="SPLICE")


def capture(seed: int, regime: str) -> Path:
    run = find_run(seed, regime)
    scenario = MixedScenario.build(
        regime, topology=Topology(),
        seed=seed if run.get("vary_placement") else None)
    original = read_trace(Path(run["trace_path"]))
    OUT.mkdir(parents=True, exist_ok=True)
    work = OUT / f"capture-{seed}-{regime}.jsonl"
    client = CaptureClient(original)
    run_pipeline(work, task=scenario.task, client=client,
                 tools=_tools_for(scenario, work), attributor=None,
                 handoff_hook=scenario.handoff_hook(),
                 **scenario.workflow_kwargs)
    target = OUT / f"prompts-{seed}-{regime}.json"
    target.write_text(json.dumps({
        "seed": seed, "regime": regime, "n": len(client.captured),
        "captured": client.captured,
    }, indent=1), encoding="utf-8")
    print(f"captured {len(client.captured)} prompts, 0 model calls, 0 tokens")
    print(f"  with a system prompt: "
          f"{sum(1 for c in client.captured if c['system'])}/{len(client.captured)}")
    print(f"  distinct system prompts: "
          f"{len({c['system_sha'] for c in client.captured})}")
    print(f"written to {target}")
    return target


# --------------------------------------------------------------------------
# re-issue: the measurement
# --------------------------------------------------------------------------


def runtime_fingerprint(router) -> dict[str, Any]:
    """Everything needed to tie a reproduction rate to the runtime that
    produced it. No key, no key length, nothing derived from a key."""
    from src.common.local_llama import LocalLlamaSettings

    local = LocalLlamaSettings.from_env()
    fp: dict[str, Any] = {
        "host_platform": platform.platform(),
        "python": platform.python_version(),
        "local": {
            "endpoint": local.endpoint,
            "model": local.model,
            "temperature": local.temperature,
            "seed": local.seed,
            "num_ctx": local.num_ctx,
            "max_output_tokens": local.max_output_tokens,
            "determinism": "ollama documents options.seed + temperature 0 as "
                           "reproducible for a fixed model and runtime",
        },
    }
    try:
        import urllib.request

        with urllib.request.urlopen(
                f"{local.endpoint.rstrip('/')}/api/version", timeout=10) as fh:
            fp["local"]["ollama_version"] = json.loads(
                fh.read().decode("utf-8")).get("version")
        with urllib.request.urlopen(
                f"{local.endpoint.rstrip('/')}/api/tags", timeout=10) as fh:
            for model in json.loads(fh.read().decode("utf-8")).get("models", []):
                if model.get("name") == local.model:
                    fp["local"]["digest"] = model.get("digest", "")[:16]
                    fp["local"]["quantization"] = (
                        model.get("details", {}).get("quantization_level"))
                    fp["local"]["parameter_size"] = (
                        model.get("details", {}).get("parameter_size"))
    except Exception as exc:  # noqa: BLE001
        fp["local"]["version_probe"] = f"{type(exc).__name__}"
    try:
        from src.common.nvidia import MODELS, load_nvidia_settings

        nv = load_nvidia_settings()
        fp["nvidia"] = {
            "model": nv.model,
            "model_id": MODELS[nv.model].model_id if nv.model in MODELS else nv.model,
            "temperature": nv.temperature,
            "determinism": "temperature 0 is greedy; the provider documents no "
                           "bitwise determinism guarantee",
        }
    except Exception as exc:  # noqa: BLE001
        fp["nvidia"] = {"unavailable": type(exc).__name__}
    return fp


def reissue(prompt_file: Path, repeats: int, sample: int,
            include: list[str] | None = None,
            providers: tuple[str, ...] = ("local",)) -> dict:
    payload = json.loads(prompt_file.read_text(encoding="utf-8"))
    captured = payload["captured"]
    routing = Topology().routing()

    by_provider: dict[str, list[dict]] = defaultdict(list)
    for item in captured:
        by_provider[routing.get(item["agent"], "local")].append(item)

    chosen: list[dict] = []
    wanted = set(include or [])
    for item in captured:
        if item["event"] in wanted:
            chosen.append(item)
    # Spread over AGENT ROLE, not over position: the first twelve local events
    # are all `acq*` with near-identical prompts, which would measure one
    # prompt shape twelve times.
    def role(agent: str) -> str:
        return "".join(ch for ch in agent if not ch.isdigit()) or agent

    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for provider in providers:
        for item in by_provider.get(provider, []):
            buckets[(provider, role(item["agent"]))].append(item)
    keys = sorted(buckets)
    while len(chosen) < sample + len(wanted) and keys:
        progressed = False
        for key in list(keys):
            if not buckets[key]:
                keys.remove(key)
                continue
            item = buckets[key].pop(0)
            if item["event"] not in {c["event"] for c in chosen}:
                chosen.append(item)
                progressed = True
            if len(chosen) >= sample + len(wanted):
                break
        if not progressed:
            break

    ledgers = build_ledgers()
    router = build_router(Topology(), ledgers)
    fingerprint = runtime_fingerprint(router)
    print(json.dumps(fingerprint, indent=1))
    print(f"\nre-issuing {len(chosen)} captured prompts, {repeats}x each, "
          f"byte-for-byte, with the system prompt they were composed with\n")

    print(f"{'event':<8}{'agent':<9}{'prov':<8}{'prompt sha':<13}{'sys sha':<13}"
          f"{'outputs':<10}{'distinct':>9}{'matches orig':>14}")
    records = []
    for item in chosen:
        provider = routing.get(item["agent"], "local")
        hashes, texts, tokens = [], [], 0
        for _rep in range(repeats):
            try:
                router.announce(item["agent"], item["kind"])
            except Exception:  # noqa: BLE001
                pass
            started = time.time()
            try:
                response = router.generate(
                    item["prompt"], system=item["system"],
                    json_output=item["json_output"])
                text, used, err = response.text, response.total_tokens, None
            except Exception as exc:  # noqa: BLE001
                text, used, err = "", 0, f"{type(exc).__name__}: {exc}"
            hashes.append(sha(text))
            texts.append(text)
            tokens += used
            records.append({
                "event": item["event"], "agent": item["agent"],
                "provider": provider, "repeat": _rep + 1,
                "prompt_sha": item["prompt_sha"],
                "system_sha": item["system_sha"],
                "output_sha": sha(text),
                "original_output_sha": item["original_output_sha"],
                "matches_original": text == item["original_output"],
                "tokens": used, "error": err,
                "latency_s": round(time.time() - started, 1),
                "output_chars": len(text), "orig_chars": len(item["original_output"]),
            })
        distinct = len(set(hashes))
        matches = sum(1 for t in texts if t == item["original_output"])
        print(f"{item['event']:<8}{item['agent']:<9}{provider:<8}"
              f"{item['prompt_sha']:<13}{item['system_sha']:<13}"
              f"{repeats:<10}{distinct:>9}{matches:>10}/{repeats}")

    OUT.mkdir(parents=True, exist_ok=True)
    result = {"fingerprint": fingerprint, "repeats": repeats,
              "prompt_file": str(prompt_file), "records": records}
    (OUT / "reissue.json").write_text(json.dumps(result, indent=1),
                                      encoding="utf-8")
    summarise(result)
    return result


def summarise(result: dict) -> None:
    records = result["records"]
    repeats = result["repeats"]
    ok = [r for r in records if not r["error"]]
    print("\n" + "=" * 92)
    print("SELF-CONSISTENCY -- the same prompt re-issued, output compared to "
          "the FIRST reply")
    print("=" * 92)
    by_event: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        by_event[r["event"]].append(r)
    stable = unstable = 0
    for event, group in sorted(by_event.items()):
        if len({g["output_sha"] for g in group}) == 1:
            stable += 1
        else:
            unstable += 1
    print(f"  events whose {repeats} replies are all byte-identical to each "
          f"other: {stable}/{stable + unstable}")
    if unstable:
        print(f"  events that varied across repeats: {unstable}")
        for event, group in sorted(by_event.items()):
            shas = {g["output_sha"] for g in group}
            if len(shas) > 1:
                print(f"    {event} ({group[0]['provider']}): {len(shas)} "
                      f"distinct outputs over {len(group)} calls, "
                      f"chars {sorted({g['output_chars'] for g in group})}")

    print("\n" + "=" * 92)
    print("A3 -- output matches what the ORIGINAL RUN recorded")
    print("=" * 92)
    agg: dict[str, list[bool]] = defaultdict(list)
    for r in ok:
        agg[r["provider"]].append(r["matches_original"])
    print(f"  {'provider':<10}{'calls':>7}{'matched':>9}{'rate':>8}")
    for provider, hits in sorted(agg.items()):
        print(f"  {provider:<10}{len(hits):>7}{sum(hits):>9}"
              f"{sum(hits) / len(hits) * 100:>7.1f}%")
    allhits = [h for hits in agg.values() for h in hits]
    if allhits:
        print(f"  {'TOTAL':<10}{len(allhits):>7}{sum(allhits):>9}"
              f"{sum(allhits) / len(allhits) * 100:>7.1f}%")

    print("\n" + "=" * 92)
    print("ATTRIBUTION OF EVERY MISMATCH")
    print("=" * 92)
    mismatched = [r for r in ok if not r["matches_original"]]
    if not mismatched:
        print("  none")
    # A re-issue uses the captured prompt verbatim, so the prompt and system
    # hashes are fixed by construction. Any mismatch is therefore the model or
    # the runtime, never prompt assembly and never a missing system prompt.
    for r in mismatched:
        same_prompt = True   # verbatim re-issue
        print(f"  {r['event']:<8}{r['provider']:<8}repeat {r['repeat']}  "
              f"prompt identical={same_prompt}  system identical=True  "
              f"-> MODEL/RUNTIME  "
              f"({r['orig_chars']} -> {r['output_chars']} chars)")
    errs = [r for r in records if r["error"]]
    if errs:
        print(f"\n  errored calls: {len(errs)}")
        for r in errs[:5]:
            print(f"    {r['event']} {r['provider']}: {r['error']}")
    print(f"\n  tokens spent: {sum(r['tokens'] for r in records)} over "
          f"{len(records)} calls")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture", action="store_true")
    ap.add_argument("--reissue", action="store_true")
    ap.add_argument("--seed", type=int, default=20260917)
    ap.add_argument("--regime", default="small")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--sample", type=int, default=12)
    ap.add_argument("--include", default="",
                    help="comma-separated event ids to force into the sample")
    ap.add_argument("--providers", default="local")
    ap.add_argument("--prompts", type=Path)
    args = ap.parse_args()

    if args.capture:
        capture(args.seed, args.regime)
        return
    if args.reissue:
        prompt_file = args.prompts or (
            OUT / f"prompts-{args.seed}-{args.regime}.json")
        if not prompt_file.exists():
            raise SystemExit(f"capture first: {prompt_file} is missing")
        reissue(prompt_file, args.repeats, args.sample,
                include=[x for x in args.include.split(",") if x],
                providers=tuple(args.providers.split(",")))
        return
    ap.error("give --capture or --reissue")


if __name__ == "__main__":
    main()
