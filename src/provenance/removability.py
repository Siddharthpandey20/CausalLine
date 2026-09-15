"""
The nested counterfactual: is this source actually removable from this prompt?

WHAT LEAVE-ONE-OUT QUIETLY ASSUMES
----------------------------------
Every counterfactual verdict in this project rests on one unstated premise:

    deleting the block labelled [S] removes S's information from the request

When that holds, an unchanged answer is evidence of non-influence. When it does
not -- because the same material also reached the prompt by a second route the
redaction never touched -- an unchanged answer is evidence of nothing at all,
and the verdict is `clean`. That is a **false clean produced by plumbing**, the
error docs/03 #4 names as the dangerous direction, and docs/03 #16 records that
nothing in this project enforced the premise anywhere.

It is not hypothetical. A measured real-model false clean had exactly this
shape: the poisoned material was quoted inside a *second* source that stayed in
the prompt, so removing the first changed the request and not the information.

WHAT THIS MODULE DOES, AND WHY IT COSTS NOTHING
-----------------------------------------------
The proposal on the table was a nested counterfactual: one extra model call per
pair to confirm the removal really removed something. That is not necessary,
because the question is about the *prompt*, not about the model. The prompt is
on disk. So this check is textual and deterministic:

    1. redact the source, exactly as the counterfactual will
    2. take the distinctive spans of its content (src/provenance/signatures.py,
       the same shingling the `carryover` facet uses -- one definition, so the
       two cannot drift apart)
    3. ask which of them still occur in the redacted prompt
    4. say where: inside another rendered source, or outside the source block

Zero extra calls, and strictly stronger than the call would have been: a model
that happened to answer the same way twice proves nothing, while a span found
in the redacted prompt proves the route exists.

HOW A FAILED CHECK IS TREATED
-----------------------------
A source that is not removable cannot be cleared. The counterfactual is still
run -- see `estimator.counterfactual` -- because a *changed* answer is still
evidence of influence and throwing the call away would lose it. What the failed
check forbids is the other verdict: an unchanged answer on an incompletely
removed source is recorded as influenced, with the residual route named. That is
the conservative direction, which costs preserved work and never costs safety.

The result is recorded as its own evidence type rather than folded into the
counterfactual's notes: `removability=verified` and `removability=residual:N`
are different grounds for the same word, and a reader of a trace must be able to
tell "we removed it and the answer held still" from "we could not remove it".
"""

from dataclasses import dataclass, field
from typing import Any

from src.common.prompts import (
    SourceNotInPrompt,
    parse_sources,
    redact_in_prompt,
    redact_source,
)
from src.provenance.signatures import SHINGLE, distinctive_spans, spans_present_in

# The marker written into `CheckRecord.notes`. Read back with `verdict_of()`
# rather than by string-matching at the call site, so there is one spelling.
NOTE_PREFIX = "removability="
VERIFIED = "verified"
UNCHECKED = "unchecked"


@dataclass
class Removability:
    """Whether redacting one source really takes its content out of the prompt.

    `verified` is the only value that licenses a `clean` verdict. `residual`
    means the redaction left the information behind; `unchecked` means the
    question could not be asked (no stored prompt, nothing to redact), which is
    not a pass.
    """

    source_id: str
    state: str = UNCHECKED
    residual: int = 0
    routes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def verified(self) -> bool:
        return self.state == VERIFIED

    def note(self) -> str:
        if self.state == VERIFIED:
            return f"{NOTE_PREFIX}{VERIFIED}"
        if self.state == UNCHECKED:
            return f"{NOTE_PREFIX}{UNCHECKED}" + (
                f" ({self.error})" if self.error else ""
            )
        detail = "; ".join(self.routes[:3])
        return f"{NOTE_PREFIX}residual:{self.residual} ({detail})"


def verdict_of(record: Any) -> str:
    """The removability state recorded on a `check` record.

    Returns `unchecked` for every record written before this check existed,
    which is the correct reading: nobody asked.
    """
    notes = getattr(record, "notes", "") or ""
    index = notes.find(NOTE_PREFIX)
    if index < 0:
        return UNCHECKED
    tail = notes[index + len(NOTE_PREFIX) :]
    return tail.split()[0].rstrip(";,").strip() if tail.strip() else UNCHECKED


def check(
    prompt: str,
    block: str,
    source_id: str,
    content: str | None = None,
    n: int = SHINGLE,
) -> Removability:
    """Does redacting `source_id` remove its content from `prompt`?

    `content` defaults to whatever the block says the source's content is, which
    is the right default: the check has to be about the text that is actually in
    the prompt, not about the stored source, and D-061's `defuse()` means those
    two can differ by an indent.
    """
    out = Removability(source_id=source_id)
    if not prompt or not block:
        out.error = "no stored prompt or source block"
        return out

    parsed = {sid: text for sid, _header, text in parse_sources(block)}
    if content is None:
        content = parsed.get(source_id)
    if content is None:
        out.error = f"{source_id} is not in this source block"
        return out

    try:
        redacted_block = redact_source(block, source_id)
        redacted_prompt = redact_in_prompt(prompt, block, source_id)
    except (SourceNotInPrompt, KeyError) as exc:
        out.error = f"could not redact: {exc}"
        return out

    spans = distinctive_spans(content, n)
    if not spans:
        # Nothing distinctive to look for -- an empty or near-empty source. The
        # premise is vacuously satisfied: there is no information left behind
        # because there was none to begin with.
        out.state = VERIFIED
        return out

    survivors = spans_present_in(redacted_prompt, spans)
    if not survivors:
        out.state = VERIFIED
        return out

    out.state = "residual"
    out.residual = len(survivors)

    # Name the route. Which other source carries the material matters: it is the
    # difference between "a sibling quotes it" (a real second route, and a
    # candidate for atomic-unit grouping) and "the task statement repeats it"
    # (a route no source removal can ever cut).
    remaining = parse_sources(redacted_block)
    for sid, _header, text in remaining:
        hits = spans_present_in(text, survivors)
        if hits:
            out.routes.append(f"{len(hits)} span(s) also in {sid}")
    outside = redacted_prompt.replace(redacted_block, "") if redacted_block else redacted_prompt
    hits_outside = spans_present_in(outside, survivors)
    if hits_outside:
        out.routes.append(
            f"{len(hits_outside)} span(s) outside the source block "
            "(task statement or system text)"
        )
    if not out.routes:
        out.routes.append("route not localised")
    return out


def check_group(
    prompt: str,
    block: str,
    source_ids: list[str],
    n: int = SHINGLE,
) -> Removability:
    """The same question for a whole group removed at once.

    A group removal can be removable when its members are not: two sources that
    each quote the other are individually unremovable and jointly removable,
    which is exactly the case atomic-unit grouping (D-051) exists for. So the
    group is redacted as a unit and the surviving spans are looked for once.
    """
    from src.common.prompts import splice_block

    out = Removability(source_id=",".join(source_ids))
    if not prompt or not block or not source_ids:
        out.error = "no stored prompt, source block, or group"
        return out

    parsed = {sid: text for sid, _header, text in parse_sources(block)}
    missing = [sid for sid in source_ids if sid not in parsed]
    if missing:
        out.error = f"not in this source block: {missing}"
        return out

    reduced = block
    try:
        for sid in source_ids:
            reduced = redact_source(reduced, sid)
        redacted_prompt = splice_block(prompt, block, reduced)
    except (SourceNotInPrompt, KeyError) as exc:
        out.error = f"could not redact: {exc}"
        return out

    spans: list[str] = []
    for sid in source_ids:
        spans.extend(distinctive_spans(parsed[sid], n))
    spans = sorted(set(spans))
    if not spans:
        out.state = VERIFIED
        return out

    survivors = spans_present_in(redacted_prompt, spans)
    if not survivors:
        out.state = VERIFIED
        return out

    out.state = "residual"
    out.residual = len(survivors)
    for sid, _header, text in parse_sources(reduced):
        hits = spans_present_in(text, survivors)
        if hits:
            out.routes.append(f"{len(hits)} span(s) also in {sid}")
    if not out.routes:
        out.routes.append("route not localised")
    return out
