"""
Reading `check` records: the clean / tainted / unchecked lookup.

`Trace.checked(event, source)` gives the raw verdict. This module adds the
thing the raw verdict is not: a **policy** for when a `clean` verdict is strong
enough to act on.

That distinction is the whole reason the record carries `method` and
`confidence` rather than just a verdict. Preserving work on the strength of a
`clean` record is the only way this project can cause an unsafe preservation,
so how much a `clean` is worth has to be an explicit, switchable decision that
an ablation can turn off -- not something buried in a comparison.

The default policy accepts `clean` from `counterfactual` and `structural`, and
refuses it from `self_report`:

    structural      the output was computed by our code and we can see which
                    inputs reached it. Fact, not estimate.
    counterfactual  we removed the source, re-ran, and the decision signature
                    did not move. Evidence, in the sense docs/03 issue #1 means.
    self_report     the agent said it did not use it. A claim, and docs/03
                    issue #1 says plainly that self-reported reasoning is
                    sometimes confidently wrong. Accepting it is available as
                    an ablation (`accept_self_report`) precisely so the unsafe
                    preservation it causes can be measured rather than argued
                    about.
    assumed         never clean by construction; `assumed` means we declined to
                    look and defaulted to contaminated.
"""

from dataclasses import dataclass, field
from typing import Iterable

from src.common.models import CheckRecord, CheckVerdict
from src.tracing.logger import Trace


@dataclass(frozen=True)
class ClearancePolicy:
    """When a `clean` record is allowed to stop contamination.

    `min_confidence` applies only to methods that carry a meaningful one.
    `structural` records are always 1.0 by construction, so the knob bites on
    counterfactual and self-report verdicts, which is where it should.
    """

    accept_counterfactual: bool = True
    accept_structural: bool = True
    accept_self_report: bool = False
    min_confidence: float = 0.5

    def accepts(self, record: CheckRecord) -> bool:
        if record.verdict != "clean":
            return False
        if record.confidence < self.min_confidence:
            return False
        if record.method == "structural":
            return self.accept_structural
        if record.method == "counterfactual":
            return self.accept_counterfactual
        if record.method == "self_report":
            return self.accept_self_report
        return False  # "assumed" is never a clearance

    def describe(self) -> str:
        accepted = [
            name
            for name, on in (
                ("structural", self.accept_structural),
                ("counterfactual", self.accept_counterfactual),
                ("self_report", self.accept_self_report),
            )
            if on
        ]
        return f"clean accepted from {accepted or ['nothing']} at confidence >= {self.min_confidence}"


# Refuse every clearance. Equivalent to having no check records at all, which
# is the state every trace in this repository was in before the record existed
# -- kept as a named condition because it is the control the analysed runs get
# compared against (D-025 records what it looks like).
TRUST_NOTHING = ClearancePolicy(
    accept_counterfactual=False, accept_structural=False, accept_self_report=False
)


@dataclass
class Coverage:
    """How much of a trace was actually examined.

    Reported next to every result. A work-preserved figure means one thing at
    100% coverage and something else entirely at 40%, where most of the answer
    came from the conservative fallback rather than from analysis.
    """

    pairs: int = 0
    examined: int = 0
    cleared: int = 0
    tainted: int = 0
    refused: int = 0  # examined, verdict clean, but the policy would not accept it
    by_method: dict[str, int] = field(default_factory=dict)

    @property
    def fraction(self) -> float:
        return self.examined / self.pairs if self.pairs else 1.0

    def summary(self) -> str:
        return (
            f"{self.examined}/{self.pairs} exposure pairs examined "
            f"({self.fraction:.0%}); {self.cleared} cleared, {self.tainted} "
            f"influenced, {self.refused} clean-but-not-accepted"
        )


class CheckLedger:
    """Every `check` record in a trace, indexed for the contamination walk."""

    def __init__(
        self,
        records: Iterable[CheckRecord] = (),
        policy: ClearancePolicy | None = None,
    ) -> None:
        self.policy = policy or ClearancePolicy()
        self._by_pair: dict[tuple[str, str], CheckRecord] = {}
        for record in records:
            self._by_pair[record.pair] = record

    @classmethod
    def from_trace(cls, trace: Trace, policy: ClearancePolicy | None = None) -> "CheckLedger":
        return cls(trace.checks, policy=policy)

    def verdict(self, event_id: str, source_id: str) -> CheckVerdict:
        record = self._by_pair.get((source_id, event_id))
        return record.verdict if record else "unchecked"

    def record(self, event_id: str, source_id: str) -> CheckRecord | None:
        return self._by_pair.get((source_id, event_id))

    def is_cleared(self, event_id: str, source_id: str) -> bool:
        """True only when a record exists, says clean, and the policy accepts it.

        Every other case -- no record, tainted, or a clean the policy refuses --
        is False, which the walk reads as "keep going". The three are different
        situations and they cost different amounts to resolve, which is why
        `verdict()` stays available alongside this.
        """
        record = self._by_pair.get((source_id, event_id))
        return record is not None and self.policy.accepts(record)

    def cleared_pairs(self) -> set[tuple[str, str]]:
        """(source, event) pairs the policy is willing to treat as clean.

        This is the set `contaminate(checked=...)` wants. It is not the same as
        "pairs that were examined": an examined pair whose clearance the policy
        refuses is deliberately absent, so turning the policy down tightens the
        walk rather than silently keeping the old answer.
        """
        return {pair for pair, rec in self._by_pair.items() if self.policy.accepts(rec)}

    def examined_pairs(self) -> set[tuple[str, str]]:
        return set(self._by_pair)

    def coverage(self, trace: Trace) -> Coverage:
        out = Coverage()
        for event in trace.events:
            for sid in event.exposures:
                out.pairs += 1
                record = self._by_pair.get((sid, event.id))
                if record is None:
                    continue
                out.examined += 1
                out.by_method[record.method] = out.by_method.get(record.method, 0) + 1
                if record.verdict == "tainted":
                    out.tainted += 1
                elif self.policy.accepts(record):
                    out.cleared += 1
                else:
                    out.refused += 1
        return out


if __name__ == "__main__":
    import sys

    from src.tracing.logger import read_trace

    path = sys.argv[1] if len(sys.argv) > 1 else "data/runs/fake.jsonl"
    trace = read_trace(path)
    trace.validate()
    ledger = CheckLedger.from_trace(trace)

    print(f"trace   {path}  ({len(trace.events)} events, {len(trace.checks)} checks)")
    print(f"policy  {ledger.policy.describe()}")
    print(f"        {ledger.coverage(trace).summary()}")
    print()
    unchecked = sorted(trace.unchecked_pairs())
    if unchecked:
        print(f"{len(unchecked)} exposure pairs were never examined; the")
        print("conservative fallback treats every one of them as influence:")
        for sid, eid in unchecked[:12]:
            print(f"  {eid}  <- {sid}")
        if len(unchecked) > 12:
            print(f"  ... and {len(unchecked) - 12} more")
    else:
        print("every exposure pair was examined. The conservative fallback")
        print("has nothing left to fire on, which is the state D-024 wanted.")
