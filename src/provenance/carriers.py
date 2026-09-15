"""
Resolving inherited verdicts against the evidence that arrived after them.

A **carrier** event produces no content of its own: a hand-off message, a tool
call whose arguments were lifted out of an earlier output, a tool response. Its
text is a function of one or more upstream events' outputs, so "did source S
influence this?" has exactly the same answer here as it did there
(`record_carrier` in src/provenance/attribution.py).

D-067 made the *record* honest: a carrier clearance carries the method and
confidence of the verdict it inherits rather than being stamped `structural`
whatever it inherited -- which is what let a false clean re-emerge one event
later wearing the strongest label the clearance policy has (docs/03 #17).

That fix created a timing problem, which is what this module is for.

THE TIMING PROBLEM
------------------
Carrier records are written **during the run**, by the pipeline, at the moment
the carrier event is logged. The verdicts they inherit are mostly written
**after** the run, by `refine_for_verdict`, once a detector has named a region
worth spending counterfactuals on. So at write time the honest inheritance is
usually "nothing recorded upstream" -> `assumed` -> not a clearance, and it
would stay that way forever even after the upstream pair has been examined and
cleared with evidence.

That is safe and it is badly wrong as a measurement: it leaves a hand-off event
contaminated on the strength of a question that has since been answered.

WHY THIS RESOLVES AT READ TIME AND WRITES NOTHING
-------------------------------------------------
The obvious fix -- append an updated record -- is not available, and the reason
is a good one. `Trace.validate()` refuses two `check` records for one pair,
because two verdicts on one pair means one of them is stale and nothing in the
file says which. That invariant is worth more than the convenience of appending.

So a carrier record is treated as what it always was: not a verdict, but a
**pointer** to one. `resolve()` follows the pointers over a finished trace and
hands back the table `CheckLedger` indexes. Nothing is written, no record is
superseded, and re-reading the same trace later with more evidence in it simply
resolves further.

DIRECTION OF THE CHANGE
-----------------------
A resolution may only move a verdict to what the upstream evidence actually
says. It can clear a pair the conservative fallback was holding, and it can
taint a pair a stale `clean` was holding. Neither is a relaxation: a pair whose
upstream is still unexamined resolves to `assumed`, which is never a clearance.
"""

from dataclasses import dataclass, field
from typing import Any

from src.common.models import CheckRecord
from src.provenance.attribution import CARRIER_FROM, CARRIER_NOTE

# Strength order for "the weakest verdict wins" when a carrier copies several
# upstream events at once. The same order the pipeline uses at write time.
_RANK = {"assumed": 0, "self_report": 1, "counterfactual": 2, "structural": 3}

MAX_PASSES = 16


@dataclass
class Resolution:
    """What following the pointers changed. Reported, not silent."""

    carrier_pairs: int = 0
    resolved: int = 0
    upgraded: int = 0     # `assumed` -> a real method: the pair becomes clearable
    downgraded: int = 0   # a clean that upstream evidence no longer supports
    passes: int = 0
    records: dict[tuple[str, str], CheckRecord] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"{self.carrier_pairs} carrier pair(s); {self.resolved} re-resolved "
            f"({self.upgraded} upgraded, {self.downgraded} downgraded) in "
            f"{self.passes} pass(es)"
        )


def is_carrier(record: Any) -> bool:
    return CARRIER_NOTE in (getattr(record, "notes", "") or "")


def upstream_of(record: Any) -> list[str]:
    """The event ids a carrier record inherits from, or [] if it is not one."""
    notes = getattr(record, "notes", "") or ""
    index = notes.find(CARRIER_FROM)
    if index < 0:
        return []
    tail = notes[index + len(CARRIER_FROM) :].split(" ")[0]
    return [part for part in tail.strip().rstrip(";,").split("|") if part]


def _resolve_one(
    trace: Any,
    table: dict[tuple[str, str], CheckRecord],
    source_id: str,
    upstream: list[str],
) -> tuple[str, str, float, str]:
    """(verdict, method, confidence, why) this carrier pair should carry now.

    Mirrors `GeminiPipeline._verdict_on` one layer down: a source that was not
    in an upstream event's context cannot have influenced what that event
    produced, so it contributes nothing and the clearance is a code-path fact;
    a source that *was* there and was never examined makes the whole
    inheritance `assumed`, which is not a clearance.
    """
    found: list[CheckRecord] = []
    for eid in upstream:
        if not trace.has_event(eid):
            return ("clean", "assumed", 0.0, f"upstream {eid} is not in this trace")
        if source_id not in trace.event(eid).exposures:
            continue
        record = table.get((source_id, eid))
        if record is None:
            return (
                "clean",
                "assumed",
                0.0,
                f"no verdict recorded for {source_id} on {eid}",
            )
        found.append(record)

    if not found:
        return (
            "clean",
            "structural",
            1.0,
            f"not in the context of {','.join(upstream)}",
        )
    tainted = [r for r in found if r.verdict == "tainted"]
    if tainted:
        strongest = max(tainted, key=lambda r: (_RANK.get(r.method, 0), r.confidence))
        return (
            "tainted",
            strongest.method,
            strongest.confidence,
            f"inherits the influence recorded on {strongest.target_event}",
        )
    weakest = min(found, key=lambda r: (_RANK.get(r.method, 0), r.confidence))
    return (
        "clean",
        weakest.method,
        weakest.confidence,
        f"inherits the {weakest.method} verdict on {weakest.target_event} "
        f"(confidence {weakest.confidence:.2f})",
    )


def resolve(trace: Any) -> Resolution:
    """Every check record in `trace`, with carrier verdicts followed to a fixed point.

    Iterated because a carrier can copy a carrier -- the hand-off to the
    Executor copies the Coder's output, which copies the decision. The pass
    bound is asserted rather than assumed: events form a DAG, so the chain
    terminates, and a run that does not terminate is a bug worth failing on.
    """
    out = Resolution()
    table: dict[tuple[str, str], CheckRecord] = {r.pair: r for r in trace.checks}
    carriers = {
        pair: upstream
        for pair, record in table.items()
        if is_carrier(record) and (upstream := upstream_of(record))
    }
    out.carrier_pairs = len(carriers)
    out.records = table
    if not carriers:
        return out

    for _pass in range(MAX_PASSES):
        out.passes += 1
        changed = 0
        for (sid, eid), upstream in carriers.items():
            current = table[(sid, eid)]
            verdict, method, confidence, why = _resolve_one(
                trace, table, sid, upstream
            )
            if (
                current.verdict == verdict
                and current.method == method
                and abs(current.confidence - confidence) < 1e-9
            ):
                continue
            if current.method == "assumed" and method != "assumed":
                out.upgraded += 1
            elif current.verdict == "clean" and verdict == "tainted":
                out.downgraded += 1
            table[(sid, eid)] = CheckRecord(
                source_id=sid,
                target_event=eid,
                verdict=verdict,
                method=method,
                confidence=confidence,
                notes=(
                    f"{CARRIER_NOTE} {','.join(upstream)}; resolved against "
                    f"upstream evidence: {why}; "
                    f"{CARRIER_FROM}{'|'.join(upstream)}"
                ),
                timestamp=current.timestamp,
            )
            changed += 1
            out.resolved += 1
        if not changed:
            return out

    raise AssertionError(
        f"carrier resolution did not reach a fixed point in {MAX_PASSES} passes; "
        "an inheritance chain is cyclic, which a DAG of events cannot be"
    )
