# 03 — Open issues

Known weak points. Read before proposing a design. Each one is something a
reviewer can attack, so each needs an answer in the paper.

---

## 1. We cannot see inside an LLM call

All four research results sit in one prompt. There is no reliable way to
prove one of them had zero effect. Attention weights are not causation.
Self-reported reasoning is sometimes confidently wrong.

**Our answer:** counterfactual replay. Remove the source, re-run, compare.
Self-report is a cheap first pass; counterfactual is the evidence. State
plainly in the paper that this is empirical, not a proof.

**Status:** core method. Must work by end of week 2.

---

## 2. Replay is non-deterministic

The same prompt gives different wording each time. So "compare outputs"
cannot be string equality, and "recovery succeeded" cannot mean
"identical state restored".

**Our answer:** compare at the semantic / behavioural level (does the code
do the same thing, does the task still succeed). Temperature 0, fixed
seeds where the API allows, and repeat each comparison 3 times so one
noisy sample cannot flip a verdict.

**Status:** must be decided in week 1 and written into `05-decisions.md`.

---

## 3. Ground truth is expensive

To claim accuracy we need to know the true contaminated set for each run.
Nobody provides that.

**Our answer:** we build the attacks, so we know exactly what was
poisoned. Hand-label around 30 runs per scenario. Small and honest beats
large and guessed. Two people label independently on a subset and report
agreement.

**Status:** start labelling in week 2, not week 4.

---

## 4. Contamination can hide

A poisoned source might not appear in the output but still steer a
decision — the Coder picks library X instead of Y because of it. Our check
sees no overlap and calls the event clean. This is a **false clean**, the
dangerous direction of error.

**Our answer:** track influence on decisions and plans, not only on final
text. And measure how often we are wrong — the unsafe preservation rate —
and report it prominently even if it is unflattering.

**Status:** open. Partially addressed by treating `decision` as its own
event kind.

---

## 5. "Minimum recovery set" is not provable

Minimality cannot be proved when the influence edges are themselves
estimates.

**Our answer:** stop using the word minimum. Claim "a small, safe recovery
set", show it empirically beats the baselines. Claim only what is measured.

**Status:** terminology fix. Enforce it in code names too.

---

## 6. Poisoned memory outlives the workflow

A bad entry written to shared memory can be read by other agents later,
including in a different run. Our event graph covers one workflow.

**Our answer:** every memory write is a tracked event with provenance;
invalidation rolls back the entry. Cross-session memory is declared out of
scope and named in Future work.

**Status:** in scope for single-session, out of scope across sessions.

---

## 7. The check may cost more than the rerun

If verifying that the Coder is clean costs as much as just re-running the
Coder, the whole idea collapses.

**Our answer:** measure tokens spent on analysis against tokens saved by
avoiding recomputation, and report the ratio. Then be selective: only run
counterfactual checks when the event is expensive to redo. The break-even
point is itself an interesting result.

**Status: ANSWERED, and the answer is uncomfortable.** Phase 9 measured it
(`python -m src.eval.economics`). On our own runs N=600 tokens, A=900 with
inline attribution or 300 with the targeted pass, f=0.67 at the oracle
detector. `A + f*N` exceeds `N` in both conditions, so on this testbed the
check does cost more than the rerun and no attack rate makes the storage tax
worth paying. The break-even frontier says what would have to change:
`A/N <= 0.25` with `f <= 0.5` pays off above roughly one attacked run in six.
See D-040 and docs/06-limitations.md 4 for why that is a fact about an 18-event
pipeline rather than about the method, and for the flat-token caveat that makes
the measured `A/N` an overestimate.

Three mechanisms now attack the cost rather than only measuring it: sequential
investigation that can abort early (D-041), group testing over candidates
(D-042), and self-report calibration that removes candidates before the
expensive stage runs at all (D-042). The measured saving from group testing is
+12% of counterfactual calls, or +38% with sibling inference.

---

## 8. Storage overhead

Fine-grained tracing means logging a lot. We claim not to store
everything, so we must say exactly what we drop.

**Our answer:** metadata always, content at checkpoints only, no
token-level detail. Report overhead as a percentage of total workflow
tokens and bytes.

**Status: MEASURED, and now bounded.** `overhead()` reports the three-way
split (trace / checkpoints / content) and D-022 and D-028 measured it. Phase 12
adds the lifecycle that keeps it from growing without end: garbage collection
gated on `check` records keeps one checkpoint per agent, so the retained
*count* is flat in trace length (asserted on runs up to 2000 events in
`tests/test_checkpoint_lifecycle.py`), and a configurable recovery horizon
downgrades old content to its hash with an explicit fallback to coarse
recovery. The surviving checkpoint's own payload still grows with the prefix it
snapshots; making it incremental is future work. See D-043 and
docs/06-limitations.md 7.

---

## 9. Novelty question

Provenance tracking and taint analysis are decades old in OS and database
security. A reviewer will ask what is new here.

**Our answer:** taint in an LLM prompt is *probabilistic*, not binary —
classical taint analysis assumes a deterministic program where data flow
is visible. We handle a system where influence must be estimated, and we
estimate it with counterfactual replay. Lead with that framing, not with
"we built a provenance graph".

**Status:** framing. Affects the intro and related-work sections.

---

## Which two decide acceptance

**#1** (can we actually establish non-influence) and **#7** (is it cheaper
than just re-running). If those two have solid numbers, the rest are
manageable.

---

## 10. We have never measured the noise floor

Counterfactual replay reads a changed output as evidence of influence:
remove the source, re-run, the answer is different, therefore the source
mattered.

That inference is only valid if the model would have given the *same* answer
had we changed nothing. Nobody has checked whether it does. Issue #2 says
replay is non-deterministic and answers "repeat each comparison 3 times", but
3 is a guess. It was never calibrated against a measurement, because the
measurement has never been taken.

Call the thing we are missing the **noise floor**: how often the same request,
sent again completely unchanged, produces a different answer. Every
counterfactual result has to be read against it. If the floor is 5%, a flip is
strong evidence. If it is 40%, a flip is a coin toss and three repeats cannot
tell the difference.

**Our answer:** measure it before collecting any runs.

Take one decision from `data/runs/run1.jsonl`. Re-send that exact recorded
request 20 times with nothing removed, at temperature 0, and count how often
the output differs. That number is the floor for this model. Report it beside
every influence result in the paper.

Cost: 20 requests, one day of free-tier quota (D-017).

Two things this buys, and both are worth a day:

- If the floor is near zero, the whole counterfactual method is on solid
  ground and that becomes one confident sentence in the paper rather than a
  hope.
- If it is high, we learn it *before* spending 540 runs on top of it, and we
  either raise the repeat count, compare outputs semantically instead of
  exactly, or narrow what we claim.

Do it once per model. The floor belongs to a model, so it is void the day the
model changes (D-004 has already happened once).

**Status:** PARTIALLY MEASURED, 31-08-2026, 8 of 20 trials. The result
changed the method — see D-026. Top up to 20 on the next day's quota.

Measured on `gemini-3.6-flash`, temperature 0, the Coder's `decision` call
from `data/runs/run1.jsonl`, 8 live re-sends of the identical request:

```
comparison       distinct  differs   floor
exact                   8        8    100%
whitespace              8        8    100%
alphanumeric            8        8    100%
decision                1        0      0%
```

Every one of the eight answers was textually unique. Every one of them made
the same decision: standard-library `datetime`, candidate format strings,
`strptime`. The wording is pure churn; the choice underneath it did not move
once in nine samples.

So the floor is not one number, it is a property of the comparison, and the
gap between the two ends is total. Temperature 0 does **not** give
determinism on this hosted model, which is the assumption issue #2's
"repeat 3 times" was resting on.

---

## 11. The cassette no longer replays, and nothing noticed for two phases

`data/cassettes/run1.jsonl` is the only recorded real model output we have, and
it replays by matching the exact prompt text. Two prompt changes have since
invalidated it:

- D-033 rendered the Planner's task as a labelled source, so the Planner's
  prompt changed.
- D-036 moved the source block to the end of every prompt, so the Coder's two
  prompts changed.

Both changes are right and neither should be reverted. The problem is that the
first one broke replay silently at the end of Phase 1 and was found by accident
in Phase 3, while running something else.

**Consequence:** the free offline path is now `src/eval/scripted.py`, which is
better for developing the estimator anyway -- it has ground truth and the
cassette does not. What the cassette uniquely provided was *real model text*
flowing through the pipeline, and that is currently unavailable.

**Our answer:** re-record on the next day's quota. Cost is 6 requests (D-017),
and it should be done in the same sitting as the outstanding noise-floor top-up
(issue #2, 12 trials remaining) so the two share a quota window. Until then, no
result may be described as involving real model output except the 8 noise trials
already on disk.

**Status:** OPEN. Detected 06-09-2026. The contract check added in D-036 would
have caught the first break; there is still nothing that checks the cassette
itself, and a one-line replay smoke test belongs in the same commit as the
re-recording.

---

## 12. The prose comparator has no measured noise floor

D-026 measured floors for the `decision` comparator's four facets, on eight live
re-sends. Nothing has measured `prose`, `code`, `json_shape` or `tool_args`, and
`prose` is the one that matters most: it is the comparator for every Researcher
finding, which is three of the six model calls in a run, and D-031 has just
changed it by adding a `formats` facet.

`Calibration.is_calibrated("prose")` is therefore false, and the estimator
records `uncalibrated:prose` on every run that uses it. The current behaviour on
an uncalibrated comparator is to exclude nothing, which is the conservative
setting -- every facet counts, so verdicts lean towards "influenced" and the
method over-invalidates rather than under-invalidating. That is the right default
and it is not a substitute for the measurement.

**Why this cannot be answered from the data on disk:** a floor is measured by
re-sending an *identical* request and counting how often the answer moves. The
eight trials on disk are re-sends of the Coder's decision call, so they can only
speak about decision facets. Prose needs its own re-sends of a Researcher call.

**Our answer:** 8-10 re-sends of one Researcher `agent_output` call from a
recorded run, scored per facet with `python -m src.provenance.noise --facets`.
Cost is 8-10 requests. Until it is done, any prose-comparator verdict quoted in
the paper must carry "floor unmeasured" beside it, and the honest reading of a
`clean` verdict from that comparator is weaker than from the code comparator,
which has an executable behavioural facet.

**Status:** OPEN. Raised 06-09-2026 by D-031.
