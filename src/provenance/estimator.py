"""
The influence estimator: self-report, then a targeted counterfactual.

This is the one unsolved research problem in the design (docs/03 issue #1: we
cannot see inside an LLM call), and the two-stage answer is docs/02's:

    1. **self-report** -- ask the agent which inputs it used. One cheap call.
       A claim, not evidence.
    2. **targeted counterfactual** -- remove the suspect source, re-run that one
       event, and compare *decision signatures* (never text: D-026, and see
       src/provenance/signatures.py). Slow, and it is evidence.

WHICH CLAIMS GET A COUNTERFACTUAL SPENT ON THEM
-----------------------------------------------
Not all of them, because the check has to cost less than the rerun it avoids
(docs/03 issue #7) -- and not a fixed fraction either, because the two kinds of
claim are not equally dangerous:

    "I used S3"          if wrong, we invalidate work that was fine. Wasteful,
                         safe. Accepted at face value.
    "I did not use S3"   if wrong, we preserve work that was contaminated. An
                         unsafe preservation, the error CLAUDE.md names as the
                         dangerous one. Must be verified before it is acted on.

So every negative claim is a candidate for a counterfactual, and a positive
claim is not -- except for a random `audit_rate` sample of positives, which
exists to estimate how often self-report over-claims. Without that sample the
false-positive rate of the cheap stage is unmeasured, and the "self-report
only" ablation has nothing to compare against.

An unverified negative is left **unrecorded**, which leaves the pair
`unchecked`, which the contamination walk treats as contaminated. That is the
conservative fallback doing its job: running out of budget costs work
preserved, never safety.

WHAT THIS MODULE MUST NOT READ
------------------------------
`Source.malicious`, and the detector's verdict. `AttributionRequest` does not
carry either. During the original run there is no verdict yet; in the targeted
pass (`refine_for_verdict`) the flagged ids arrive as an argument and are used
only to decide *where to look*, never to decide what the answer is. An estimator
that could see the label would produce edges shaped by the answer, and every
accuracy number after that would be worthless.
"""

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from src.common.models import InfluenceEdge
from src.common.prompts import SourceNotInPrompt, redact_in_prompt
from src.provenance import selfreport
from src.provenance.attribution import (
    AttributionRequest,
    Sink,
    derived_links_for,
)
from src.common.models import SOURCE_KINDS as _SOURCE_KINDS
from src.provenance import carriers
from src.provenance import removability as _removability
from src.provenance.signatures import (
    Calibration,
    Comparator,
    Signature,
    compare,
    for_event,
    with_carryover,
)


def _content_in_block(block: str | None, source_id: str) -> str:
    """One source's content, as rendered into the prompt.

    Read back out of the block rather than off the `Source` record, because the
    block is what the model saw and what a redaction has to remove -- D-061's
    `defuse()` means the two can differ by an indent.
    """
    if not block:
        return ""
    from src.common.prompts import parse_sources

    for sid, _header, text in parse_sources(block):
        if sid == source_id:
            return text
    return ""

MODES = ("hybrid", "self_report", "counterfactual")

# Below this many candidates, group testing costs MORE than removing them one
# at a time, and that is arithmetic rather than tuning. Recursive halving pays
# two calls per split (one per half) and only wins when a whole half comes back
# clean; with two or three candidates there is no half big enough for that to
# save anything. Dorfman's bound is O(k log(n/k)) for k << n, and n=2 is not
# that regime.
#
# Measured, which is why the constant exists: with group testing applied
# unconditionally the targeted refinement path went from 3 calls to 4 on
# scenario A, and the economics report's analysis cost A rose from 300 to 400
# tokens -- the wiring made the number it was supposed to improve worse. The
# same wiring saves 15% on the inline counterfactual path, where events carry
# eight to eleven candidates.
MIN_GROUP_TEST_CANDIDATES = 4


@dataclass
class CheckBudget:
    """A cap on counterfactual calls, because they are the expensive half.

    One flagged source on one event is one extra call, times the repeat count,
    and the Researcher's events carry eight exposures each. Unbounded, the
    analysis cost exceeds the rerun cost it is meant to avoid -- which is
    exactly the collapse docs/03 issue #7 describes.

    Running out is safe and is recorded: unverified pairs stay `unchecked` and
    the conservative fallback contaminates them.
    """

    per_event: int | None = None
    total: int | None = None
    spent: int = 0
    spent_this_event: int = 0
    denied: int = 0

    def start_event(self) -> None:
        self.spent_this_event = 0

    def allows(self) -> bool:
        if self.total is not None and self.spent >= self.total:
            return False
        if self.per_event is not None and self.spent_this_event >= self.per_event:
            return False
        return True

    def charge(self, calls: int = 1) -> None:
        self.spent += calls
        self.spent_this_event += calls

    def deny(self) -> None:
        self.denied += 1


@dataclass
class CounterfactualResult:
    """One counterfactual check, with the evidence it rests on."""

    source_id: str
    event_id: str
    influenced: bool
    before: Signature
    after: Signature
    moved: list[str] = field(default_factory=list)
    repeats: int = 0
    calls: int = 0
    tokens: int = 0
    excluded: list[str] = field(default_factory=list)
    control_stable: bool | None = None
    error: str | None = None
    # The nested removability check (src/provenance/removability.py). Its own
    # evidence type, kept apart from `error` because "we could not remove it"
    # and "the call failed" are different reasons to refuse a clearance.
    removability: Any = None

    @property
    def verdict(self) -> str:
        return "tainted" if self.influenced else "clean"

    def notes(self) -> str:
        parts = [f"counterfactual, {self.repeats} repeat(s)"]
        if self.moved:
            parts.append(f"facets moved: {','.join(self.moved)}")
        else:
            parts.append("signature unchanged")
        if self.excluded:
            parts.append(f"facets excluded as noisy: {','.join(self.excluded)}")
        if self.removability is not None:
            parts.append(self.removability.note())
        if self.control_stable is False:
            parts.append("CONTROL UNSTABLE: unchanged re-run also moved")
        if self.error:
            parts.append(f"error: {self.error}")
        return "; ".join(parts)


def counterfactual(
    client: Any,
    request: AttributionRequest,
    source_id: str,
    comparator: Comparator | None = None,
    calibration: Calibration | None = None,
    repeats: int = 1,
    context: dict[str, Any] | None = None,
    control_run: bool = False,
) -> CounterfactualResult:
    """Re-run one event with one source removed, and compare decisions.

    `repeats` re-runs the redacted request more than once. The aggregation is
    **any-differs**: if any repeat's signature moves, the source is called
    influential. That is the safe direction -- a missed flip is an unsafe
    preservation, an extra flip is wasted recomputation -- and it is only
    affordable because the calibrated signature has a measured 0% floor. On an
    uncalibrated comparator the same rule would tend towards "everything is
    influential", which is the conservative fallback with extra steps.

    `control_run` spends one more call re-running the request **unchanged**.
    That turns each verdict into its own noise measurement rather than relying
    on the model-wide calibration: if the control moves, this event's signature
    is unstable and the verdict is not evidence, whatever the comparator's
    average floor says.

    TWO THINGS THIS DOES BEYOND COMPARING SIGNATURES (D-064, D-066)
    ---------------------------------------------------------------
    Both exist because a measured false clean showed that "the signature did not
    move" is not by itself sufficient grounds for `clean`.

    `carryover`  a removal-aware facet is added to both signatures: which
        distinctive spans of the removed source's content survive into the
        answer. A comparator reading the output alone cannot represent "the
        answer repeats the removed source", and that is influence by
        definition. See src/provenance/signatures.py.

    removability  before a `clean` verdict is trusted, the redacted prompt is
        checked for a *second route* by which the same material reached the
        model. Leave-one-out is only sound when every route is removable, and
        nothing enforced that before. A residual route forces the conservative
        verdict. Costs no calls -- see src/provenance/removability.py.
    """
    if not request.prompt or not request.source_block:
        return CounterfactualResult(
            source_id, request.event_id, influenced=True,
            before=Signature("none"), after=Signature("none"),
            error="no stored prompt or source block, so the request cannot be "
                  "reissued; defaulting to influenced",
        )

    comparator = comparator or for_event(request.kind, request.output)
    calibration = calibration or Calibration()
    excluded = calibration.exclude_for(comparator.name)

    try:
        redacted = redact_in_prompt(request.prompt, request.source_block, source_id)
    except (SourceNotInPrompt, KeyError) as exc:
        # A redaction that changed nothing would give an unchanged answer and
        # the verdict "no influence" -- a false clean produced by plumbing.
        return CounterfactualResult(
            source_id, request.event_id, influenced=True,
            before=Signature(comparator.name), after=Signature(comparator.name),
            error=f"could not redact {source_id}: {exc}",
        )

    # The content as it actually appears in the prompt, which is what a second
    # route would have to repeat. Falls back to empty, which makes the
    # carryover facet a constant and therefore inert -- never a false positive.
    removed_content = _content_in_block(request.source_block, source_id)
    removability = _removability.check(
        request.prompt, request.source_block, source_id, removed_content
    )

    # `redacted` is what the model sees after the removal, and it is what the
    # carryover facet measures uniqueness against (D-079): a span the answer
    # could still have got from the request is not evidence about the source
    # that left it.
    before = with_carryover(
        comparator.signature(request.output, context),
        request.output,
        removed_content,
        redacted,
    )
    result = CounterfactualResult(
        source_id, request.event_id, influenced=False,
        before=before, after=before, repeats=0,
        excluded=sorted(excluded),
        removability=removability,
    )

    for _ in range(max(1, repeats)):
        response = client.generate(redacted, system=request.system)
        result.calls += 1
        result.tokens += getattr(response, "total_tokens", 0)
        after = with_carryover(
            comparator.signature(response.text, context),
            response.text,
            removed_content,
            redacted,
        )
        result.after = after
        result.repeats += 1
        same, moved = compare(before, after, exclude=excluded)
        if not same:
            result.influenced = True
            result.moved = moved
            break

    # The call is made either way and its result is kept either way: a moved
    # signature is evidence of influence whether or not the removal was clean,
    # and discarding it would destroy a real positive edge. What a failed
    # removability check forbids is only the *other* verdict.
    if not result.influenced and not removability.verified:
        result.influenced = True
        result.error = (
            "the removal left the source's content reachable in the prompt, so "
            "an unchanged answer is not evidence of non-influence "
            f"({removability.note()})"
        )

    if control_run:
        response = client.generate(request.prompt, system=request.system)
        result.calls += 1
        result.tokens += getattr(response, "total_tokens", 0)
        # The SAME facets on both sides. `before` carries the removal-aware
        # facet, so a control signature without it differs on that facet
        # every single time and every event reads as unstable -- a control that
        # always fires is not a control, it is the conservative fallback with an
        # extra call. The control re-issues the *unchanged* prompt, so its
        # carryover is measured against the same removed content and is expected
        # to match.
        control = with_carryover(
            comparator.signature(response.text, context),
            response.text,
            removed_content,
            redacted,
        )
        same, _moved = compare(before, control, exclude=excluded)
        result.control_stable = same
        if not same:
            # The instrument moved with nothing removed, so this event's flip
            # carries no information. Fall back to contaminated.
            result.influenced = True
            result.error = (
                "control run (nothing removed) also changed the signature, so a "
                "flip on this event is not evidence"
            )
    return result


# --- the attributor the pipeline uses ----------------------------------------


@dataclass
class HybridAttributor:
    """Self-report, then a counterfactual on the claims that could hurt us.

    `mode` selects the three conditions docs/04 asks for as ablations, and they
    are one object with a switch rather than three code paths that have to be
    kept in step:

        hybrid          self-report, counterfactual on negatives + audit sample
        self_report     self-report only. Cheap. Cannot preserve any work unless
                        `trust_self_report_negatives` is on, in which case it
                        can preserve work it has no evidence for -- which is the
                        point of running the ablation.
        counterfactual  no self-report at all; counterfactual every exposure.
                        The most evidence per verdict and the highest cost, and
                        the condition that shows what the cheap stage buys.
    """

    client: Any
    mode: str = "hybrid"
    name: str = "hybrid"
    model: str = "unknown"
    calibration: Calibration = field(default_factory=Calibration)
    budget: CheckBudget = field(default_factory=CheckBudget)
    repeats: int = 1
    audit_rate: float = 0.0
    control_run: bool = False
    trust_self_report_negatives: bool = False
    confident_at: float = 0.7
    # Runs generated code, for the behavioural half of the code comparator.
    # None disables it and the comparator falls back to its AST facets.
    run_code: Callable[[str], dict[str, Any]] | None = None
    seed: int = 20260906
    # --- Phase 2 wiring ---------------------------------------------------
    # Group-test the sources that need evidence instead of removing them one
    # at a time (Dorfman; src/provenance/group_test.py), with the fixed-budget
    # Lasso fallback (src/provenance/budget_attribution.py) behind it for when
    # the sparsity assumption stops holding. Both were built and tested
    # standalone and reached no experiment until now. On by default; the
    # switch exists so the saving can be measured rather than asserted.
    use_group_testing: bool = True
    # Measured per-channel precision of positive self-reports
    # (src/provenance/calibration.py). A channel that clears its threshold has
    # its positives accepted without a verifying call. Never applied to a
    # negative: accepting a positive costs replay tokens, accepting a negative
    # costs safety.
    self_report_calibration: Any = None
    # Filled in as it goes, for the cost table and the ablation report.
    stats: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        self._rng = random.Random(self.seed)
        if self.name == "hybrid":
            self.name = self.mode

    def _bump(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def attribute(self, request: AttributionRequest, sink: Sink) -> None:
        if not request.exposures:
            return
        self.budget.start_event()
        comparator = for_event(request.kind, request.output)
        context = {"run": self.run_code} if self.run_code else None
        if not self.calibration.is_calibrated(comparator.name):
            self._bump(f"uncalibrated:{comparator.name}")

        report = None
        if self.mode != "counterfactual":
            report = selfreport.ask(
                self.client,
                request.event_id,
                request.kind,
                request.output,
                request.catalogue(),
            )
            sink.log_usage(
                "self_report",
                model=self.model,
                prompt_tokens=report.prompt_tokens,
                output_tokens=report.output_tokens,
                total_tokens=report.total_tokens,
                event_id_=request.event_id,
                agent_id=request.agent_id,
            )
            self._bump("self_report_calls")
            if report.parse_failed:
                self._bump("self_report_unparseable")

        # Order matters when the budget is tight: the least confident negative
        # claims are the ones most likely to be wrong, so they are checked first.
        # Nothing here can consult the detector or the ground-truth label, so
        # confidence is the only signal available for prioritising.
        order = sorted(
            request.exposures,
            key=lambda sid: report.claim(sid).confidence if report else 0.0,
        )

        # Sources that will need a counterfactual, collected before any is
        # spent so they can be removed as a group rather than one at a time.
        needs_evidence: list[str] = []

        for sid in order:
            claim = report.claim(sid) if report else None
            wants_evidence = claim is None or claim.needs_evidence
            audited = (
                claim is not None
                and claim.used
                and claim.reported
                and self.audit_rate > 0
                and self._rng.random() < self.audit_rate
            )

            # --- 2c: a calibrated channel's positive needs no verification ---
            if audited and self.self_report_calibration is not None:
                from src.risk.attack_model import channel_for

                kind = request.labels.get(sid, "").split(",")[0].strip()
                if kind and self.self_report_calibration.accepts_positive(
                    channel_for(kind) if kind in _SOURCE_KINDS else "unknown"
                ):
                    self._bump("calibration_skipped_audit")
                    audited = False

            if claim is not None and claim.used and not audited:
                self._record_self_report_positive(sink, request, sid, claim)
                continue

            if not (wants_evidence or audited):
                continue

            if self.mode == "self_report":
                # No counterfactual available in this condition.
                if self.trust_self_report_negatives and claim is not None and not claim.used:
                    sink.log_check(
                        sid, request.event_id, "clean", "self_report",
                        confidence=claim.confidence,
                        notes="agent reported not using this source; ABLATION: "
                              "accepted without evidence",
                    )
                    self._bump("self_report_negatives_trusted")
                else:
                    self._bump("left_unchecked")
                continue

            if not self.budget.allows():
                self.budget.deny()
                self._bump("budget_denied")
                continue

            # Defer: batched below so a group removal can settle several at
            # once. Ordering is preserved, so the least-confident negatives are
            # still the ones a tight budget reaches first.
            #
            # The size test is applied to the whole deferred set after the loop,
            # not here -- at this point we do not yet know how many will need
            # evidence.
            if self.use_group_testing:
                needs_evidence.append(sid)
                continue

            result = counterfactual(
                self.client, request, sid,
                comparator=comparator,
                calibration=self.calibration,
                repeats=self.repeats,
                context=context,
                control_run=self.control_run,
            )
            self.budget.charge(result.calls)
            self._bump("counterfactual_calls", result.calls)
            self._bump(f"counterfactual_{result.verdict}")
            if audited:
                self._bump("audited_positives")
                if not result.influenced:
                    # Self-report claimed a source it did not use. Counts the
                    # cheap stage's over-claiming, which is otherwise invisible.
                    self._bump("audit_found_overclaim")
            sink.log_usage(
                "counterfactual",
                model=self.model,
                prompt_tokens=0,
                output_tokens=0,
                total_tokens=result.tokens,
                event_id_=request.event_id,
                agent_id=request.agent_id,
            )
            if result.influenced:
                sink.log_influence(
                    InfluenceEdge(
                        sid, request.event_id, method="counterfactual",
                        confident=result.error is None,
                    )
                )
            sink.log_check(
                sid, request.event_id, result.verdict, "counterfactual",
                confidence=0.5 if result.error else 1.0,
                signature_before=result.before.value,
                signature_after=result.after.value,
                comparator=comparator.name,
                repeats=result.repeats,
                notes=result.notes(),
            )

        # ATOMIC UNITS BEFORE ANY REMOVAL TEST (D-051).
        # A source and the sources it was recorded as derived from are
        # individually unnecessary and jointly necessary, so testing them
        # apart clears both. Merge them first; every test below -- single or
        # recursive -- then operates on whole units and never splits one.
        from src.provenance.group_test import merge_derived_units

        units = merge_derived_units(needs_evidence, request.derived_links)
        merged = [u for u in units if len(u) > 1]
        if merged:
            self._bump("derived_units_merged", len(merged))
            self._bump(
                "derived_sources_merged", sum(len(u) for u in merged)
            )

        # The threshold counts units, not sources: units are what halving
        # actually splits, so three sources merged into one unit is one test,
        # not three.
        if len(units) >= MIN_GROUP_TEST_CANDIDATES:
            self._group_investigate(
                sink, request, units, comparator, context
            )
        else:
            # Too few to halve profitably. One call each, which is what group
            # testing would have cost more than.
            self._bump("group_testing_skipped_small", len(units))
            for unit in units:
                if len(unit) == 1:
                    self._single_investigate(
                        sink, request, unit[0], comparator, context
                    )
                else:
                    self._merged_investigate(
                        sink, request, unit, comparator, context
                    )

    def _merged_investigate(
        self,
        sink: Sink,
        request: AttributionRequest,
        unit: tuple[str, ...],
        comparator: Comparator,
        context: dict[str, Any] | None,
    ) -> None:
        """One atomic unit, one counterfactual, removing every member together.

        The small-candidate path still has to respect units, or the fix would
        apply only above `MIN_GROUP_TEST_CANDIDATES` and the exact case it
        exists for -- a summary beside its two inputs, three candidates -- would
        slip through the cheap path untouched.

        The verdict is recorded against **every** member. That is the honest
        reading of the evidence: the removal shows the unit mattered, and which
        member carried it is precisely what single-source testing cannot
        determine here. Calling one member influenced and the others clean
        would be inventing a distinction the measurement does not support, and
        it would be the unsafe direction.
        """
        from src.provenance.group_test import CounterfactualDecision

        decision = CounterfactualDecision(
            client=self.client,
            request=request,
            comparator=comparator,
            calibration=self.calibration,
            context=context,
            repeats=self.repeats,
            control_run=self.control_run,
        )
        influenced = bool(decision(list(unit)))
        self.budget.charge(decision.calls)
        self._bump("counterfactual_calls", decision.calls)
        self._bump("merged_unit_tests")
        self._bump(f"counterfactual_{'tainted' if influenced else 'clean'}")
        sink.log_usage(
            "counterfactual",
            model=self.model,
            prompt_tokens=0,
            output_tokens=0,
            total_tokens=decision.tokens,
            event_id_=request.event_id,
            agent_id=request.agent_id,
        )
        error = decision.errors[0] if decision.errors else None
        for sid in unit:
            if influenced:
                sink.log_influence(
                    InfluenceEdge(
                        sid, request.event_id, method="counterfactual",
                        confident=error is None,
                    )
                )
            sink.log_check(
                sid, request.event_id,
                "tainted" if influenced else "clean",
                "counterfactual",
                confidence=0.5 if error else 1.0,
                signature_before=decision._before.value,
                signature_after="",
                comparator=comparator.name,
                repeats=1,
                notes=(
                    f"removed as one atomic unit with {list(unit)} -- these "
                    "carry a recorded derived_from link, so testing them "
                    "separately clears both (D-051)"
                ) + (f"; error: {error}" if error else ""),
            )

    def _single_investigate(
        self,
        sink: Sink,
        request: AttributionRequest,
        sid: str,
        comparator: Comparator,
        context: dict[str, Any] | None,
    ) -> None:
        """One source, one counterfactual. The path a candidate set too small
        to halve profitably takes -- see MIN_GROUP_TEST_CANDIDATES."""
        result = counterfactual(
            self.client, request, sid,
            comparator=comparator,
            calibration=self.calibration,
            repeats=self.repeats,
            context=context,
            control_run=self.control_run,
        )
        self.budget.charge(result.calls)
        self._bump("counterfactual_calls", result.calls)
        self._bump(f"counterfactual_{result.verdict}")
        sink.log_usage(
            "counterfactual",
            model=self.model,
            prompt_tokens=0,
            output_tokens=0,
            total_tokens=result.tokens,
            event_id_=request.event_id,
            agent_id=request.agent_id,
        )
        if result.influenced:
            sink.log_influence(
                InfluenceEdge(
                    sid, request.event_id, method="counterfactual",
                    confident=result.error is None,
                )
            )
        sink.log_check(
            sid, request.event_id, result.verdict, "counterfactual",
            confidence=0.5 if result.error else 1.0,
            signature_before=result.before.value,
            signature_after=result.after.value,
            comparator=comparator.name,
            repeats=result.repeats,
            notes=result.notes(),
        )

    def _group_investigate(
        self,
        sink: Sink,
        request: AttributionRequest,
        units: list[tuple[str, ...]],
        comparator: Comparator,
        context: dict[str, Any] | None,
    ) -> None:
        """Settle several sources with group removals instead of one call each.

        This is where the cost actually is. A Researcher event in this pipeline
        carries eight exposures and the Coder's decision event eleven, so
        leave-one-out spends eight or eleven counterfactuals on one event --
        while the replay those calls exist to avoid may cost one. That ratio is
        docs/03 issue #7's collapse condition.

        A group that changes nothing when removed contains nothing that
        mattered, so a clean half of eight costs one call rather than eight.
        The blind spot is interaction effects, which is documented on
        `group_test()` and inherited from single-source leave-one-out rather
        than introduced here (D-030, D-042).

        Falls through to fixed-budget attribution when the group-test
        diagnostics say sparsity is failing -- the Context-Cite-style fallback
        the design specified and which nothing previously reached.
        """
        from src.provenance.budget_attribution import attribute as budget_attribute
        from src.provenance.group_test import (
            CounterfactualDecision,
            GroupTestDiagnostics,
            group_test,
        )

        decision = CounterfactualDecision(
            client=self.client,
            request=request,
            comparator=comparator,
            calibration=self.calibration,
            context=context,
            repeats=self.repeats,
            control_run=self.control_run,
        )
        diagnostics = GroupTestDiagnostics()

        # `group_test` halves a flat list, so it is handed one KEY per atomic
        # unit and the decision function expands a key back to every source in
        # that unit before removing it. The recursion therefore cannot split a
        # unit: it never sees the members, only the key.
        by_key = {unit[0]: unit for unit in units}
        keys = [unit[0] for unit in units]

        def unit_decision(group: list[str]) -> bool:
            return decision([sid for key in group for sid in by_key[key]])

        influential_keys = set(group_test(keys, unit_decision, diagnostics))
        influential = {sid for key in influential_keys for sid in by_key[key]}
        candidates = [sid for unit in units for sid in unit]

        self._bump("group_tests", diagnostics.groups_tested)
        self._bump("group_calls", decision.calls)
        # What leave-one-out would have spent on the same candidates.
        self._bump("group_calls_saved", len(candidates) - decision.calls)
        if diagnostics.interaction_suspected:
            self._bump("interaction_suspected", diagnostics.interaction_suspected)

        if diagnostics.sparsity_failing():
            self._bump("sparsity_failing")
            fitted = budget_attribute(
                keys, unit_decision, budget=max(16, len(keys) + 2)
            )
            self._bump("budget_fallback_calls", fitted.calls)
            self._bump("budget_fallback_invocations")
            if not fitted.degenerate:
                # Fitted over unit keys, so expand before reporting -- the
                # fallback must respect atomic units for the same reason the
                # halving does.
                influential = {
                    sid for key in fitted.influential for sid in by_key[key]
                }

        self.budget.charge(decision.calls)
        self._bump("counterfactual_calls", decision.calls)
        sink.log_usage(
            "counterfactual",
            model=self.model,
            prompt_tokens=0,
            output_tokens=0,
            total_tokens=decision.tokens,
            event_id_=request.event_id,
            agent_id=request.agent_id,
        )

        error = decision.errors[0] if decision.errors else None
        for sid in candidates:
            hit = sid in influential
            self._bump(f"counterfactual_{'tainted' if hit else 'clean'}")
            if hit:
                sink.log_influence(
                    InfluenceEdge(
                        sid, request.event_id, method="counterfactual",
                        confident=error is None,
                    )
                )
            sink.log_check(
                sid, request.event_id, "tainted" if hit else "clean",
                "counterfactual",
                confidence=0.5 if error else 1.0,
                signature_before=decision._before.value,
                signature_after=decision._before.value if not hit else "",
                comparator=comparator.name,
                repeats=1,
                notes=(
                    f"group-tested over {len(candidates)} candidates in "
                    f"{decision.calls} calls ({diagnostics.line()})"
                ) + (f"; error: {error}" if error else ""),
            )

    def _record_self_report_positive(
        self, sink: Sink, request: AttributionRequest, sid: str, claim: Any
    ) -> None:
        sink.log_influence(
            InfluenceEdge(
                sid, request.event_id, method="self_report",
                confident=claim.confidence >= self.confident_at,
            )
        )
        sink.log_check(
            sid, request.event_id, "tainted", "self_report",
            confidence=claim.confidence,
            notes="agent reported using this source; accepted without evidence "
                  "because a wrong positive costs work, not safety",
        )
        self._bump("self_report_positives")

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in sorted(self.stats.items())]
        return f"{self.name}: " + (", ".join(parts) if parts else "nothing recorded")


# --- the targeted pass, run after a detector has spoken ----------------------


@dataclass
class RefineResult:
    examined: int = 0
    cleared: int = 0
    tainted: int = 0
    calls: int = 0
    tokens: int = 0
    denied: int = 0
    order: list[tuple[str, str]] = field(default_factory=list)
    # --- Phase 2 instrumentation ------------------------------------------
    # What the wired-in cost reductions actually did on this run. Reported
    # rather than assumed: each of these was built and tested standalone, and
    # a standalone measurement does not transfer until it is measured here.
    groups_tested: int = 0
    group_calls_saved: int = 0
    sprt_decision: str = ""
    sprt_checks: int = 0
    sprt_log_lr: float = 0.0
    sprt_trajectory: list[float] = field(default_factory=list)
    aborted_early: bool = False
    calibration_skips: int = 0
    fallback_invocations: int = 0
    # How often a summary and its own recorded inputs were removed together
    # instead of separately (D-051), and how many sources that covered.
    derived_units_merged: int = 0
    derived_sources_merged: int = 0
    # Inherited (carrier) verdicts re-resolved against the evidence this pass
    # produced (D-067). Free -- no model calls -- and reported rather than
    # silent, because it moves the contaminated region.
    carrier_pairs: int = 0
    carriers_reresolved: int = 0
    carriers_upgraded: int = 0
    carriers_downgraded: int = 0
    # How many event groups were ordered by the detector's own per-source
    # confidence rather than by trace position (Phase 5).
    confidence_ordered: int = 0
    # Events whose signature moved on an UNCHANGED re-send, so a counterfactual
    # flip there would have carried no information (Phase 2, `control_run`).
    control_unstable_events: int = 0
    # D-082: what the deferred self-report pass did, when it is enabled. Zero
    # under the inline default, which is how every stored number was produced.
    self_report_calls: int = 0
    self_report_positives: int = 0

    def summary(self) -> str:
        parts = [
            f"examined {self.examined} pairs ({self.cleared} cleared, "
            f"{self.tainted} influenced) in {self.calls} calls, "
            f"{self.tokens} tokens; {self.denied} denied by budget"
        ]
        if self.groups_tested:
            parts.append(
                f"group testing: {self.groups_tested} groups, "
                f"{self.group_calls_saved} calls saved vs leave-one-out"
            )
        if self.sprt_decision:
            parts.append(
                f"SPRT: {self.sprt_decision} after {self.sprt_checks} "
                f"observations (logLR={self.sprt_log_lr:+.2f})"
                + (" -- ABORTED EARLY" if self.aborted_early else "")
            )
        if self.calibration_skips:
            parts.append(f"calibration skipped {self.calibration_skips} checks")
        if self.fallback_invocations:
            parts.append(
                f"budget-attribution fallback fired {self.fallback_invocations}x"
            )
        if self.carriers_reresolved:
            parts.append(
                f"carrier verdicts re-resolved: {self.carriers_reresolved} of "
                f"{self.carrier_pairs} ({self.carriers_upgraded} upgraded, "
                f"{self.carriers_downgraded} downgraded), 0 calls"
            )
        return "; ".join(parts)


def refine_for_verdict(
    trace_path: str | Path,
    malicious: Iterable[str],
    client: Any,
    calibration: Calibration | None = None,
    budget: CheckBudget | None = None,
    repeats: int = 1,
    run_code: Callable[[str], dict[str, Any]] | None = None,
    model: str = "unknown",
    control_run: bool = False,
    use_group_testing: bool = True,
    use_sprt: bool = True,
    sprt_config: Any = None,
    self_report_calibration: Any = None,
    detector_confidence: dict[str, float] | None = None,
    self_report_first: bool = False,
) -> RefineResult:
    """Examine only the pairs the detector's verdict makes relevant.

    docs/02: "Run counterfactual only where it matters: on sources the detector
    flagged." This is that, and it is what makes the analysis cost bearable --
    checking every exposure on every event costs more than the rerun it avoids,
    which is docs/03 issue #7's collapse condition.

    The loop is a frontier expansion, and the shape is the point:

        examine an unchecked pair whose source is currently contaminated
          -> record the verdict
          -> recompute the contaminated region
          -> the frontier moves

    A pair whose source turns out clean never has its downstream pairs
    examined at all, so the cost scales with the contaminated region rather
    than with the trace. Running out of budget leaves the rest `unchecked`,
    which the walk contaminates -- expensive in preserved work, never unsafe.

    Appends `check` and `influence` records to the existing trace, which is what
    the append-only format is for (D-008). Nothing already written is rewritten.

    THREE COST REDUCTIONS, WIRED IN HERE
    ------------------------------------
    Each was built and tested standalone and reached no experiment until now.
    All three default on; the switches exist so the before/after can be
    measured rather than asserted.

    `use_group_testing`  Instead of one counterfactual per candidate source,
        the unchecked sources on a single event are removed in groups and the
        recursion descends only into halves that mattered (Dorfman;
        src/provenance/group_test.py). When the group-test diagnostics say the
        sparsity assumption is failing, this falls through to the fixed-budget
        Lasso attribution (src/provenance/budget_attribution.py) rather than
        continuing to halve blindly -- the fallback the design always specified
        and which was previously unreachable.

    `use_sprt`  Each candidate's verdict is a Bernoulli observation of the
        contamination fraction. After every one, Wald's sequential test
        (src/recovery/sprt_investigate.py) is updated; if the evidence says
        contamination is spreading past the point where selective recovery pays
        for itself, the investigation stops there instead of spending the rest
        of its budget on a trace already trending towards "restart". The
        trajectory is recorded either way, so "SPRT never fired" is a reported
        measurement rather than silence.

    `self_report_calibration`  A measured per-channel precision
        (src/provenance/calibration.py). Channels whose positive self-reports
        are precise enough are accepted without spending a counterfactual.

    `detector_confidence`  The per-source confidence the detector's `Verdict`
        already carries and which nothing downstream has ever read -- only the
        flat list of ids crossed into recovery (Phase 5). It orders the sources
        *within* an event, so when the budget runs out mid-event it has spent
        its calls on the ones worth spending them on.

        **Ascending** confidence, and the direction is the argument. A
        high-confidence flag is near-certainly malicious, so checking it mostly
        confirms taint that was going to be recomputed anyway. A low-confidence
        flag is the one that might be a false positive, and clearing it removes
        its whole downstream region from the recovery set. So the least certain
        flags are checked first, because that is where a call buys the most
        preserved work.

        What it deliberately does NOT reorder is which *event* is taken next.
        That order is the frontier expansion, and it is forward for a reason:
        an upstream verdict can remove downstream pairs from the region
        entirely, so checking downstream first spends calls on pairs that were
        about to become irrelevant. Confidence is a tiebreak inside an event,
        not a replacement for the frontier.
    """
    from src.provenance.budget_attribution import attribute as budget_attribute
    from src.provenance.contamination import contaminate
    from src.provenance.group_test import (
        CounterfactualDecision,
        GroupTestDiagnostics,
        group_test,
        merge_derived_units,
    )
    from src.recovery.sprt_investigate import SPRTConfig, SPRTState
    from src.risk.attack_model import channel_for
    from src.tracing.logger import TraceLogger, read_trace

    calibration = calibration or Calibration()
    budget = budget or CheckBudget()
    result = RefineResult()
    flagged = set(malicious)
    # D-082: self-report deferred to here instead of firing on every event of
    # every run. Asked once per event, the first time that event is reached,
    # and only for events the detector's region actually brings up.
    asked: dict[str, Any] = {}

    sprt = SPRTState(config=sprt_config or SPRTConfig()) if use_sprt else None

    trace = read_trace(trace_path)
    trace.validate()

    def _record(log, sid, eid, request, influenced, error, before, after, repeats_done):
        """One verdict written to the trace. Shared by both investigation
        paths so a group-tested verdict and a single-source one are recorded
        identically -- the trace must not be able to tell which found it."""
        comparator = for_event(request.kind, request.output).name
        if influenced:
            result.tainted += 1
            log.log_influence(
                InfluenceEdge(sid, eid, method="counterfactual",
                              confident=error is None)
            )
        else:
            result.cleared += 1
        log.log_check(
            sid, eid, "tainted" if influenced else "clean", "counterfactual",
            confidence=0.5 if error else 1.0,
            signature_before=before,
            signature_after=after,
            comparator=comparator,
            repeats=repeats_done,
            notes=(
                "group-tested counterfactual" if use_group_testing
                else "single-source counterfactual"
            ) + (f"; error: {error}" if error else ""),
        )
        result.examined += 1
        result.order.append((sid, eid))

    with TraceLogger(trace_path, append=True, meta={"record_kind": "refinement"}) as log:
        while True:
            region = contaminate(trace, flagged)
            pending = _unchecked_in_region(trace, region.sources)
            candidates = [(sid, eid) for eid, sid in pending]
            if not candidates:
                break
            if not budget.allows():
                result.denied += len(candidates)
                break

            # Work one event at a time. Group testing removes several of an
            # event's sources in a single call, so the sources have to be
            # batched by the event whose prompt they are being removed from.
            target_event = candidates[0][1]
            group = [sid for sid, eid in candidates if eid == target_event]

            request = request_for(trace, target_event, run_code=run_code)
            if request is None:
                # Nothing to re-issue. Left unchecked, so the walk keeps it
                # contaminated; a placeholder `clean` here would be a fabricated
                # clearance.
                for sid in group:
                    _mark_unexaminable(log, sid, target_event)
                trace = read_trace(trace_path)
                continue

            # --- 2c: calibration can settle a positive without a call --------
            if self_report_calibration is not None:
                keep: list[str] = []
                for sid in group:
                    channel = channel_for(trace.source(sid).kind)
                    record = trace.check_record(target_event, sid)
                    positive = (
                        record is not None
                        and record.method == "self_report"
                        and record.verdict == "tainted"
                    )
                    if positive and self_report_calibration.accepts_positive(channel):
                        # Accepting a positive costs replay tokens and never
                        # costs safety. A negative is never settled this way.
                        result.calibration_skips += 1
                        continue
                    keep.append(sid)
                group = keep
                if not group:
                    trace = read_trace(trace_path)
                    continue

            # --- Phase 5: detector confidence orders the group ---------
            # Least certain flag first; a source the detector never named gets
            # 1.0, so it sorts last and an unflagged derived source never
            # displaces a doubtful flag. Trace order breaks ties, so the
            # ordering stays deterministic when no confidence is supplied.
            if detector_confidence:
                position = {sid: i for i, sid in enumerate(group)}
                group = sorted(
                    group,
                    key=lambda sid: (
                        detector_confidence.get(sid, 1.0),
                        position[sid],
                    ),
                )
                result.confidence_ordered += 1

            # --- D-082: the lazy self-report, if it is enabled -------------
            # Same claims, same asymmetry, same records as the inline pass --
            # a positive is accepted and costs replay tokens, a negative earns
            # nothing and still has to be paid for with a counterfactual. Only
            # *when* it is asked has changed, and it is asked about the event
            # the frontier has actually reached.
            if self_report_first and target_event not in asked:
                report = selfreport.ask(
                    client,
                    target_event,
                    request.kind,
                    request.output,
                    request.catalogue(),
                )
                asked[target_event] = report
                log.log_usage(
                    "self_report", model=model,
                    prompt_tokens=report.prompt_tokens,
                    output_tokens=report.output_tokens,
                    total_tokens=report.total_tokens,
                    event_id_=target_event, agent_id=request.agent_id,
                )
                result.self_report_calls += 1
                settled: list[str] = []
                for sid in group:
                    claim = report.claims.get(sid)
                    if claim is None or not claim.used:
                        continue
                    log.log_influence(
                        InfluenceEdge(
                            sid, target_event, method="self_report",
                            confident=claim.confidence >= 0.7,
                        )
                    )
                    log.log_check(
                        sid, target_event, "tainted", "self_report",
                        confidence=claim.confidence,
                        notes="agent reported using this source; accepted "
                              "without evidence because a wrong positive costs "
                              "work, not safety (asked on demand, D-082)",
                    )
                    result.examined += 1
                    result.tainted += 1
                    result.self_report_positives += 1
                    result.order.append((sid, target_event))
                    settled.append(sid)
                if settled:
                    group = [sid for sid in group if sid not in settled]
                    if not group:
                        trace = read_trace(trace_path)
                        continue

            context = {"run": run_code} if run_code else None
            decision = CounterfactualDecision(
                client=client,
                request=request,
                calibration=calibration,
                context=context,
                repeats=repeats,
                control_run=control_run,
            )

            # --- ATOMIC UNITS BEFORE ANY REMOVAL TEST (D-051) ---------------
            # A source and the sources it was recorded as derived from are
            # individually unnecessary and jointly necessary, so removing
            # either alone clears both. Merge them into units removed whole and
            # never split -- including inside the halving, which only ever sees
            # unit keys.
            units = merge_derived_units(group, request.derived_links)
            merged_units = [u for u in units if len(u) > 1]
            result.derived_units_merged += len(merged_units)
            result.derived_sources_merged += sum(len(u) for u in merged_units)

            by_key = {unit[0]: unit for unit in units}
            unit_keys = [unit[0] for unit in units]

            def unit_decision(keys, _d=decision, _m=by_key):
                return _d([sid for key in keys for sid in _m[key]])

            # The threshold counts units, not sources: units are what halving
            # splits, so three sources in one unit are one test, not three.
            batched = use_group_testing and len(units) >= MIN_GROUP_TEST_CANDIDATES
            control_unstable = False
            if batched:
                diagnostics = GroupTestDiagnostics()
                influential_keys = set(
                    group_test(unit_keys, unit_decision, diagnostics)
                )
                result.groups_tested += diagnostics.groups_tested
                # What leave-one-out would have spent on the same group.
                result.group_calls_saved += len(group) - decision.calls

                # --- 2b fallback: sparsity assumption failing ---------------
                if diagnostics.sparsity_failing():
                    fitted = budget_attribute(
                        unit_keys, unit_decision, budget=max(16, len(unit_keys) + 2)
                    )
                    result.fallback_invocations += 1
                    if not fitted.degenerate:
                        influential_keys = set(fitted.influential)
                influential = {
                    sid for key in influential_keys for sid in by_key[key]
                }

                for sid in sorted(group):
                    _record(
                        log, sid, target_event, request,
                        influenced=sid in influential,
                        error=decision.errors[0] if decision.errors else None,
                        before=decision._before.value,
                        after=decision._before.value if sid not in influential else "",
                        repeats_done=1,
                    )
                    if sprt is not None:
                        sprt.observe(sid in influential)
                        result.sprt_trajectory.append(round(sprt.log_lr, 3))
                budget.charge(decision.calls)
                result.calls += decision.calls
                result.tokens += decision.tokens
                control_unstable = decision.control_stable is False
            elif len(by_key[unit_keys[0]]) > 1:
                # Below the batching threshold, but the first unit is a merged
                # one and still has to be removed whole -- otherwise the fix
                # would apply only above MIN_GROUP_TEST_CANDIDATES and the
                # exact case it exists for, a summary beside its inputs, would
                # slip through the cheap path untouched.
                unit = by_key[unit_keys[0]]
                influenced = bool(unit_decision([unit_keys[0]]))
                budget.charge(decision.calls)
                result.calls += decision.calls
                result.tokens += decision.tokens
                control_unstable = decision.control_stable is False
                error = decision.errors[0] if decision.errors else None
                for sid in unit:
                    # Recorded against every member. The removal shows the unit
                    # mattered; which member carried it is exactly what
                    # single-source testing cannot determine here, and
                    # splitting the verdict would invent a distinction the
                    # measurement does not support, in the unsafe direction.
                    _record(
                        log, sid, target_event, request,
                        influenced=influenced,
                        error=error,
                        before=decision._before.value,
                        after="" if influenced else decision._before.value,
                        repeats_done=1,
                    )
                    if sprt is not None:
                        sprt.observe(influenced)
                        result.sprt_trajectory.append(round(sprt.log_lr, 3))
                log.log_usage(
                    "counterfactual", model=model, prompt_tokens=0,
                    output_tokens=0, total_tokens=decision.tokens,
                    event_id_=target_event, agent_id=request.agent_id,
                )
            else:
                sid = group[0]
                outcome = counterfactual(
                    client, request, sid,
                    calibration=calibration,
                    repeats=repeats,
                    context=context,
                    control_run=control_run,
                )
                budget.charge(outcome.calls)
                result.calls += outcome.calls
                result.tokens += outcome.tokens
                control_unstable = outcome.control_stable is False
                _record(
                    log, sid, target_event, request,
                    influenced=outcome.influenced,
                    error=outcome.error,
                    before=outcome.before.value,
                    after=outcome.after.value,
                    repeats_done=outcome.repeats,
                )
                log.log_usage(
                    "counterfactual", model=model, prompt_tokens=0,
                    output_tokens=0, total_tokens=outcome.tokens,
                    event_id_=target_event, agent_id=request.agent_id,
                )
                if sprt is not None:
                    sprt.observe(outcome.influenced)
                    result.sprt_trajectory.append(round(sprt.log_lr, 3))

            if batched:
                log.log_usage(
                    "counterfactual", model=model, prompt_tokens=0,
                    output_tokens=0, total_tokens=decision.tokens,
                    event_id_=target_event, agent_id=request.agent_id,
                )

            # Phase 2: whether this event's signature held still on an
            # unchanged re-send. Read after the branches, because that is where
            # the control call (if any) was actually made. `control_stable` is
            # None when no control ran, which is neither stable nor unstable and
            # must not be counted as either.
            if control_unstable:
                result.control_unstable_events += 1

            # --- 2a: stop as soon as the evidence is decisive ---------------
            if sprt is not None:
                result.sprt_checks = sprt.checks
                result.sprt_log_lr = sprt.log_lr
                verdict = sprt.decision()
                if verdict == "abort_restart":
                    # Contamination is spreading past the point where finishing
                    # the investigation pays for itself. Everything still
                    # unchecked stays unchecked, which the walk contaminates --
                    # so stopping here costs preserved work, never safety.
                    result.sprt_decision = "abort_restart"
                    result.aborted_early = True
                    trace = read_trace(trace_path)
                    remaining = _unchecked_in_region(
                        trace, contaminate(trace, flagged).sources
                    )
                    result.denied += len(remaining)
                    break
                result.sprt_decision = verdict

            trace = read_trace(trace_path)

    # --- carrier resolution, after the evidence exists (D-067) --------------
    # Carrier records were written mid-run, when almost nothing had been
    # examined, so they inherited `assumed` -- correct at the time and stale
    # now. Following those pointers against the verdicts this pass produced is
    # free (no model calls) and it is the difference between a hand-off event
    # staying contaminated on a question that has since been answered and it
    # inheriting the answer. Reported here; the walk does the same resolution
    # for itself in `CheckLedger.from_trace`.
    resolution = carriers.resolve(read_trace(trace_path))
    result.carrier_pairs = resolution.carrier_pairs
    result.carriers_reresolved = resolution.resolved
    result.carriers_upgraded = resolution.upgraded
    result.carriers_downgraded = resolution.downgraded

    return result


def _unchecked_in_region(trace: Any, sources: Iterable[str]) -> list[tuple[str, str]]:
    """(event, source) pairs that are unchecked and whose source is contaminated.

    Ordered by position in the trace so the frontier is walked forwards: an
    upstream verdict can remove downstream pairs from the region entirely, and
    checking downstream first would spend calls on pairs that were about to
    become irrelevant.
    """
    contaminated = set(sources)
    out: list[tuple[str, str]] = []
    for event in trace.events:
        for sid in event.exposures:
            if sid in contaminated and trace.checked(event.id, sid) == "unchecked":
                out.append((event.id, sid))
    return out


def _mark_unexaminable(log: Any, source_id: str, event_id: str) -> None:
    """Record that a pair cannot be examined, as `assumed` rather than `clean`.

    Writing a record here is not bookkeeping: without one the frontier
    expansion would pick the same pair again on the next iteration and never
    terminate. `assumed` is the honest verdict -- we looked for a way to check
    it and there was none -- and it is never a clearance, so it keeps the pair
    contaminated.
    """
    log.log_check(
        source_id, event_id, "tainted", "assumed",
        confidence=0.0,
        notes="no stored prompt for this event, so no counterfactual is "
              "possible; conservative fallback applies",
    )


def request_for(
    trace: Any, event_id: str, run_code: Callable[[str], dict[str, Any]] | None = None
) -> AttributionRequest | None:
    """Rebuild the attribution request for a logged event, from the trace.

    Returns None when the event has no stored prompt -- a tool response, or an
    event logged before the content store existed. Callers must treat None as
    "cannot be examined", never as "nothing was influenced".
    """
    event = trace.event(event_id)
    prompt = trace.prompt_text(event_id)
    output = trace.output_text(event_id)
    if not prompt or output is None:
        return None
    labels: dict[str, str] = {}
    for sid in event.exposures:
        source = trace.source(sid)
        where = source.metadata.get("url") or source.metadata.get("key") or source.kind
        labels[sid] = f"{source.kind}, {where}"
    # Recorded derived_from links among the exposed sources, so the
    # investigation loop can merge a summary with its own inputs into one
    # atomic test unit (D-051). Computed from the finished trace here; the
    # pipeline computes the same thing mid-run off its partial log.
    def _producer(sid: str) -> str | None:
        source = trace.source(sid)
        return source.derived_from or source.origin_event

    def _influencers(event: str) -> list[str]:
        return [e.source_id for e in trace.influence if e.target_event == event]

    return AttributionRequest(
        event_id=event_id,
        agent_id=event.agent_id,
        kind=event.kind,
        output=output,
        exposures=list(event.exposures),
        labels=labels,
        prompt=prompt,
        source_block=trace.source_block_text(event_id),
        system=trace.system_text(event_id),
        derived_links=derived_links_for(
            list(event.exposures), _producer, _influencers
        ),
    )
