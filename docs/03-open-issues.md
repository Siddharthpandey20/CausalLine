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

**Status:** highest-risk issue after #1. Instrument token counts from day one.

---

## 8. Storage overhead

Fine-grained tracing means logging a lot. We claim not to store
everything, so we must say exactly what we drop.

**Our answer:** metadata always, content at checkpoints only, no
token-level detail. Report overhead as a percentage of total workflow
tokens and bytes.

**Status:** measure in week 4.

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
