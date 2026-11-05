"""Is the early-cutoff rule physically able to fire on these providers?

IREC has two mechanisms, and they rest on different assumptions:

  (a) SPLICE on a byte-identical *prompt*. Needs only A1, input completeness.
      Costs no model call at all.
  (b) CUTOFF on a byte-identical *output* after re-execution. Needs A3,
      faithful replay: an unaffected agent re-executed on its clean inputs
      must reproduce its original output.

Mechanism (b) is worthless if the provider cannot reproduce an output even when
the prompt is unchanged. That is not a property of the attack, the workflow or
the recovery algorithm -- it is a property of the provider, and it is measurable
on its own, with no intervention anywhere.

This re-issues ORIGINAL prompts, unmodified, and compares the reply with what
the original run recorded. Any difference is pure nondeterminism: same prompt,
same model, same decoding settings.

    python -m docs.research.prototype.irec_nondeterminism --seed 20260917 \
        --regime small --sample 6

Reports per provider, because they are different services with different
guarantees. No key is ever printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.eval.mixed_campaign import build_ledgers, build_router
from src.recovery.replay import pipeline_model_events
from src.tracing.logger import read_trace
from src.tracing.mixed import Topology


def find_run(seed: int, regime: str, directory: Path) -> dict:
    for path in sorted(directory.glob("seed*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for run in payload.get("results", []):
            if run.get("seed") == seed and run.get("regime") == regime:
                return run
    raise SystemExit(f"no saved run for {seed}/{regime}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=20260917)
    ap.add_argument("--regime", default="small")
    ap.add_argument("--sample", type=int, default=6,
                    help="model events to re-issue, spread over providers")
    ap.add_argument("--repeats", type=int, default=2,
                    help="times to re-issue each prompt")
    ap.add_argument("--out", type=Path,
                    default=Path("data/runs/irec-live/nondeterminism.json"))
    args = ap.parse_args()

    run = find_run(args.seed, args.regime,
                   Path("data/results/mixed56-workload-varied"))
    original = read_trace(Path(run["trace_path"]))
    events = pipeline_model_events(original)

    topo = Topology()
    routing = topo.routing()
    router = build_router(topo, build_ledgers())

    # Spread the sample over providers rather than taking the first N, which
    # would be all local.
    by_provider: dict[str, list[str]] = defaultdict(list)
    for eid in events:
        by_provider[routing.get(original.event(eid).agent_id, "local")].append(eid)
    chosen: list[str] = []
    while len(chosen) < args.sample:
        added = False
        for provider in sorted(by_provider):
            if by_provider[provider]:
                chosen.append(by_provider[provider].pop(0))
                added = True
                if len(chosen) >= args.sample:
                    break
        if not added:
            break

    print(f"re-issuing {len(chosen)} ORIGINAL prompts, {args.repeats}x each, "
          f"unmodified\n")
    print(f"{'event':<8}{'agent':<10}{'provider':<9}{'rep':>4}"
          f"{'identical':>11}{'orig chars':>11}{'new chars':>10}{'tokens':>8}")
    records = []
    for eid in chosen:
        ev = original.event(eid)
        provider = routing.get(ev.agent_id, "local")
        prompt = original.prompt_text(eid) or ""
        orig_out = original.output_text(eid) or ""
        for rep in range(1, args.repeats + 1):
            started = time.time()
            try:
                router.announce(ev.agent_id, ev.kind)
            except Exception:  # noqa: BLE001 -- not every client announces
                pass
            try:
                resp = router.generate(prompt)
                text, tokens = resp.text, resp.total_tokens
                err = None
            except Exception as exc:  # noqa: BLE001
                text, tokens, err = "", 0, f"{type(exc).__name__}: {exc}"
            identical = (text == orig_out)
            records.append({
                "event": eid, "agent": ev.agent_id, "provider": provider,
                "repeat": rep, "identical": identical,
                "orig_chars": len(orig_out), "new_chars": len(text),
                "tokens": tokens, "error": err,
                "latency_s": round(time.time() - started, 1),
            })
            print(f"{eid:<8}{ev.agent_id:<10}{provider:<9}{rep:>4}"
                  f"{('YES' if identical else 'no'):>11}{len(orig_out):>11}"
                  f"{len(text):>10}{tokens:>8}" + (f"  {err}" if err else ""))
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(records, indent=1), encoding="utf-8")

    print(f"\n{'provider':<10}{'re-issued':>10}{'identical':>11}{'rate':>8}")
    agg: dict[str, list[bool]] = defaultdict(list)
    for r in records:
        if not r["error"]:
            agg[r["provider"]].append(r["identical"])
    for provider, hits in sorted(agg.items()):
        print(f"{provider:<10}{len(hits):>10}{sum(hits):>11}"
              f"{sum(hits)/len(hits)*100 if hits else 0:>7.0f}%")
    allhits = [h for hits in agg.values() for h in hits]
    print(f"{'TOTAL':<10}{len(allhits):>10}{sum(allhits):>11}"
          f"{sum(allhits)/len(allhits)*100 if allhits else 0:>7.0f}%")
    print(f"\ntokens spent: {sum(r['tokens'] for r in records)}")
    print(f"written to {args.out}")
    print("""
Reading: this is the CEILING on IREC's early-cutoff yield. An unaffected agent
cannot reproduce its output more often than the provider reproduces an
unchanged prompt. A low rate does not make IREC unsafe -- a differing output is
treated as changed, which is the conservative direction -- but it caps how much
the cutoff mechanism can ever save, independently of the workload.""")


if __name__ == "__main__":
    main()
