"""
Self-report: ask the agent which of its inputs it actually used.

The cheap half of the two-stage estimator in docs/02-architecture.md. One extra
call per event, and it produces *candidate* edges -- claims, not evidence.
docs/03 issue #1 is blunt about the limit: "self-reported reasoning is
sometimes confidently wrong". So this module deliberately does two things and
not three:

  1. asks the question in a fixed, structured form
  2. parses the answer into per-source claims with a confidence

It does not decide anything. Which claims are trusted, and which get a
counterfactual spent on them, is `src/provenance/estimator.py`'s job.

THE ASYMMETRY THAT DRIVES THE WHOLE DESIGN
------------------------------------------
The two possible claims are not equally dangerous, and they are not treated
equally anywhere downstream:

    "I used S3"       a positive claim. If wrong, we invalidate work that was
                      actually fine. Costs tokens. Safe.
    "I did not use S3"  a negative claim. If wrong, we preserve work that was
                      actually contaminated -- an unsafe preservation, the
                      error docs/04 says to always report and CLAUDE.md calls
                      the dangerous one.

So a positive claim is accepted at face value and a negative claim is the one
that has to earn its verdict. That is not caution for its own sake: it is the
only reason a cheap unreliable instrument can be used at all in front of an
expensive reliable one.

UNMENTIONED SOURCES ARE NOT CLEARED
-----------------------------------
A model that lists three of five ids has said nothing about the other two. The
default for a source the report does not mention is `used=True` with
confidence 0.0 -- taken as influence, but with no confidence behind it, so the
estimator escalates it rather than acting on it. Reading silence as "unused"
would turn a truncated answer into preserved contaminated work.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

SELF_REPORT_SYSTEM = (
    "You audit your own work. You are shown an output you just produced and "
    "the numbered sources that were available to you when you produced it. "
    "For each source, decide whether its content actually changed what you "
    "wrote -- not whether it was relevant, available, or on the same topic. "
    "A source you read and then did not use is UNUSED. Answer with JSON only."
)

# Fixed wording. The estimator's numbers are only comparable across runs if the
# question does not drift, so this is a constant rather than an f-string built
# at the call site.
SELF_REPORT_TEMPLATE = """You produced this {kind} output:

{output}

These sources were available to you:
{catalogue}

Which of them actually changed what you wrote?

Reply with JSON only, in exactly this shape:
{{"used": ["S1"], "unused": ["S2"], "confidence": {{"S1": 0.9, "S2": 0.8}}}}

Rules:
- every source id listed above must appear in exactly one of "used" or "unused"
- "confidence" is how sure you are about that source, from 0.0 to 1.0
- a source whose content you could have written the output without is "unused"
"""


class Generator(Protocol):
    """The slice of GeminiClient this module needs. Anything with this shape
    works, which is what lets the estimator run offline against a scripted
    client and against a cassette."""

    def generate(
        self,
        prompt: str,
        system: str | None = ...,
        json_output: bool = ...,
        temperature: float | None = ...,
    ) -> Any: ...


@dataclass(frozen=True)
class Claim:
    """What the agent said about one source.

    `confidence` is the agent's own number, not ours. It is used only to decide
    whether to spend a counterfactual, never as the confidence of a final
    verdict -- a model's certainty about its own attribution is exactly the
    thing docs/03 issue #1 says not to trust.
    """

    source_id: str
    used: bool
    confidence: float
    reported: bool = True  # False when the report never mentioned this source

    @property
    def needs_evidence(self) -> bool:
        """A claim we must not act on without checking it.

        Every negative claim qualifies. A negative claim is the only kind that
        can cause an unsafe preservation, so "trust it if confidence is high"
        would be trusting the instrument to grade itself.
        """
        return not self.used or not self.reported


@dataclass
class SelfReport:
    """One agent's attribution of one event, parsed."""

    event_id: str
    claims: dict[str, Claim] = field(default_factory=dict)
    raw: str = ""
    parse_failed: bool = False
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def used(self) -> list[str]:
        return [sid for sid, c in self.claims.items() if c.used]

    def unused(self) -> list[str]:
        return [sid for sid, c in self.claims.items() if not c.used]

    def claim(self, source_id: str) -> Claim:
        return self.claims[source_id]


def build_question(kind: str, output: str, catalogue: list[tuple[str, str]]) -> str:
    """`catalogue` is [(source_id, one-line description), ...].

    The output is truncated, the sources are not re-sent in full. The agent
    already saw them; repeating them would roughly double the cost of an
    already-doubled call count (one self-report per event, D-016).
    """
    lines = [f"  [{sid}] {label}" for sid, label in catalogue]
    return SELF_REPORT_TEMPLATE.format(
        kind=kind,
        output=output[:2000],
        catalogue="\n".join(lines),
    )


def parse(event_id: str, text: str, source_ids: list[str]) -> SelfReport:
    """Parse a self-report answer into claims, conservatively.

    Anything unparseable, and any source the answer failed to mention, comes
    back as `used=True, confidence=0.0`: taken as influence, with nothing
    behind it. The estimator will then either spend a counterfactual on it or
    leave it contaminated. Both are safe; reading a broken answer as "nothing
    was used" is not.
    """
    report = SelfReport(event_id=event_id, raw=text)
    payload: Any = None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        payload = None

    if not isinstance(payload, dict):
        report.parse_failed = True
        report.claims = {
            sid: Claim(sid, used=True, confidence=0.0, reported=False)
            for sid in source_ids
        }
        return report

    used = {str(s) for s in payload.get("used") or [] if isinstance(s, (str, int))}
    unused = {str(s) for s in payload.get("unused") or [] if isinstance(s, (str, int))}
    raw_confidence = payload.get("confidence")
    confidence: dict[str, float] = {}
    if isinstance(raw_confidence, dict):
        for key, value in raw_confidence.items():
            try:
                confidence[str(key)] = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                continue

    for sid in source_ids:
        if sid in used and sid in unused:
            # Contradiction. Treated as used with no confidence: the model has
            # told us it does not know, which is information, and the safe
            # reading of it is the conservative one.
            report.claims[sid] = Claim(sid, used=True, confidence=0.0)
        elif sid in used:
            report.claims[sid] = Claim(sid, True, confidence.get(sid, 0.5))
        elif sid in unused:
            report.claims[sid] = Claim(sid, False, confidence.get(sid, 0.5))
        else:
            report.claims[sid] = Claim(sid, True, 0.0, reported=False)
    return report


def ask(
    client: Generator,
    event_id: str,
    kind: str,
    output: str,
    catalogue: list[tuple[str, str]],
) -> SelfReport:
    """One self-report call. Returns the parsed claims and the tokens it cost.

    The caller must log the tokens with `purpose="self_report"`. docs/04 is
    explicit that hiding the cost of the analysis is the easiest way to look
    good dishonestly, and this call is a per-event doubling of the request
    count -- the single largest cost the method adds.
    """
    source_ids = [sid for sid, _ in catalogue]
    if not source_ids:
        return SelfReport(event_id=event_id, raw="", claims={})
    question = build_question(kind, output, catalogue)
    response = client.generate(question, system=SELF_REPORT_SYSTEM, json_output=True)
    report = parse(event_id, response.text, source_ids)
    report.prompt_tokens = getattr(response, "prompt_tokens", 0)
    report.output_tokens = getattr(response, "output_tokens", 0)
    report.total_tokens = getattr(response, "total_tokens", 0)
    return report
