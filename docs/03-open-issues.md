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

---

## 13. Selective replay desynchronises against a non-deterministic Planner

`SplicingClient` matches spliced outputs to events **by call order**, not by
prompt text, and `src/recovery/replay.py` explains why: a replayed upstream
event changes the downstream prompt, so matching on the original prompt would
miss and fall through to a live call — the exact failure the splice assertion
exists to prevent.

That reasoning is sound and it assumes something the scripted client guarantees
and a real model does not: **that the rerun makes the same number of calls in
the same order.**

The Planner is asked for exactly three research questions and the pipeline
takes `questions[:3] or [self.task]`. A real model that returns two questions
on the replay makes the Researcher loop twice instead of three times. Every
subsequent splice is then matched to the wrong event. Two outcomes, and the
second is worse:

- the queue runs short and `SpliceError` fires — loud, and fine
- the queue stays long enough that ids still line up, and an output is spliced
  into an event it did not come from — **silent**, and the resulting trace is
  wrong in a way nothing downstream can detect

This cannot happen in the scripted matrix. `ScriptedClient` derives its plan
from a fixed vocabulary, so the question count is constant, and every number in
`docs/07` and `docs/08` was produced under that guarantee.

**Current handling:** `real_llm.run_generated()` catches `SpliceError` per
method, records it as a note on the result, and scores the methods that did
replay. A method that could not replay produces no row rather than a wrong one,
and the campaign says so.

**What that does not do:** detect the silent case. Nothing currently asserts
that a rerun's call sequence matches the original's shape.

**Our answer, when someone takes this:** the trace already records everything
needed — `pipeline_model_events()` is the original sequence, and the rerun
produces its own. Comparing them by (agent_id, kind) before accepting any
splice would turn the silent failure into the loud one. That is a few lines in
`SplicingClient.__post_init__` plus an assertion at the end of `replay()`, and
it needs a decision about what to do when they diverge: abort the replay, or
fall back to a coarse agent restart the way the recovery horizon already does
for a trace that has aged past its content store.

**Status:** OPEN. Raised 10-09-2026 by the real-LLM evaluation (D-052).
Not reachable from the scripted mode, so it is a new class of problem rather
than a bug in existing code.

---

## 14. A generated attack has nobody to tune it until it lands

Hand-written attacks in `src/eval/attacks.py` carry a warning that a planted
page "must actually be retrieved to be exposed", and the fixture wording was
tuned by a person until it ranked into the Researcher's top-k. A generated
payload has nobody doing that.

Measured on the first live generation: the model wrote a 25-word payload that
was pure instruction and carried almost no date-parsing vocabulary. It ranked
below the real documentation pages *and* below the scenario's own decoy, never
entered any agent's context, matched no marker, and the run was void — after
592 seconds and 12 requests had been spent.

**Current handling, and it is a mitigation rather than a solution.**
`llm_scenarios.retrievable()` checks the payload against a representative query
before the scenario is kept, and `run_generated()` re-checks before spending a
request. The generation prompt now says why the payload must read like a real
page. Measured after: payloads come back at 90–100 words with zero repairs.

**Why it is still open.** The probe query is a stand-in. The real query is
built from the Planner's questions and does not exist until the run happens, so
a payload can pass the gate and still fail to rank on the day —
`attacks.reaches()` makes exactly this disclaimer for exactly this reason. The
gate can only reject a payload that could *never* rank; it cannot promise one
that will.

The deeper question is whether it should exist at all. A payload that does not
rank is a failed attack, and failed attacks are real. Filtering them out biases
the suite toward attacks that work, which is the bias D-025 warns about. The
current position is that a payload which never enters *any* context is not a
weak attack but an absent one — it produces no exposure, so there is nothing
for an exposure-versus-influence method to be measured on. That is defensible
and it is a judgement, not a fact.

**Status:** OPEN. Raised 10-09-2026. Needs a decision before the suite is
scaled: keep the gate and say so in the paper, or drop it and report the
void rate as a result.

---

## 15. Verification demands absolute task success, and penalises CausalLine when the workflow was already broken

**Raised 11-09-2026 by the first real-LLM campaign. This is the single largest
effect on those numbers, and it runs against us — which is why it is written up
here rather than fixed.**

`verify()` treats a recovery as successful only if `task_success` is true:

```python
ok = (not tainted) and (not refs) and task_success
```

That is the right rule when the original run succeeded at the task. It is the
wrong rule when the original run **failed the task for reasons that have
nothing to do with the attack** — and on a real model that is common.

**Measured.** In the campaign, Nemotron did not reliably produce a script that
prints the five correct ISO dates. Every original run came back
`task_success=False`. The consequence, per test:

- CausalLine plans a selective recovery, replays it, and verifies.
- Verification fails on `task-level check failed` — not because contamination
  survived, but because the task was already failing before recovery started.
- CausalLine escalates to `agent_restart`. Same verdict. Escalates to
  `restart_all`. Same verdict. Budget spent, `scope=exhausted`.
- Final work preserved: **0%**, identical to B0.

Meanwhile B0, B1 and B2 **do not verify at all**, so they keep whatever they
preserved and are never penalised for the same pre-existing failure. On
`gen003` that reads as CausalLine 0% against B1's 21% — the method losing by 21
points on a test where it never had an opportunity to be judged on
contamination.

**Why this is an evaluation artefact and not a property of the method.**
`ScriptedClient` always produces a correct script, so on every scripted run the
original `task_success` is true and this branch never fires. All 96 campaign
cells in `docs/08` were measured under that condition. The rule has simply
never been exercised against a workflow that was broken to begin with.

**The obvious fix, and why it is NOT applied in this session.** Verification
should plausibly require that recovery leaves the workflow *no worse than it
found it*:

```python
ok = (not tainted) and (not refs) and (task_success or not original_task_success)
```

That is a no-op on every existing measurement — in the scripted matrix
`original_task_success` is always true, so the condition reduces to the current
one — and it matches what `docs/06` §1 already says the system claims:
"replay produces a corrected *plan* for what should have happened. It does not
produce a corrected world." CausalLine does not claim to repair a workflow that
was already failing.

**It is not applied because applying it would improve our own numbers in the
middle of the evaluation that revealed the problem.** `docs/04` is explicit that
the dangerous direction is tuning the instrument against the answer, and this
change would move CausalLine from 0% to whatever its selective plan preserved,
on exactly the tests where it currently loses. The finding is reported as
measured, and the fix is a decision for the team to take deliberately, before a
fresh campaign, and to record as such.

**What must not be said about the current numbers while this stands.** Not
"CausalLine loses to B1 on real models". The comparison is not clean: three of
the four methods are exempt from a check the fourth is failing for a reason
unrelated to what any of them are being measured on.

**Status:** OPEN. Needs a decision, not a patch: change the verification
predicate, or exclude runs whose original `task_success` is false from the
method comparison, or report both. Recommendation: change the predicate, then
re-run — and say in the paper that it was changed after the first real-LLM
campaign and why.

## 16. Leave-one-out over a prompt is only sound if every route into that prompt is removable

**Raised 13-09-2026 by the root-cause of `docs/08` §7.5. Half of it is fixed
(D-062); the general statement is not, and it is the more interesting half.**

The counterfactual's whole claim is: remove one source, re-issue, and any
change is attributable to the removal. `redact_source()` removes a source from
the **rendered source block**. Nothing in the method checks that the block was
the source's only route into the prompt, and in this pipeline it sometimes is
not — the Coder's script prompt quotes the decision event's output verbatim,
the Reviewer's quotes the draft script. A source that influenced the quoted
event survives its own redaction, and "the signature did not move" is then the
experiment failing rather than the source being innocent.

**What is fixed.** D-062 detects the quote and refuses to clear such a pair,
recording `assumed`. Cost: two exposed-only cells, 100% → 79% work preserved.

**What is open, and it is three separate things.**

1. **Detection is textual.** It finds an upstream output that was *copied*. A
   pipeline that *summarised* between agents would relay the influence with no
   verbatim span to find, and D-062 would miss it silently. Ours only copies,
   so the detector is complete here and not in general.
2. **Refusing is not the same as answering.** The right answer is a *nested*
   counterfactual: re-run the upstream event without the source, splice the new
   output into the relay slot (`replace_in_prompt()` already does exactly this
   for selective replay), and then test the downstream event. That is one extra
   call per confounded pair and it would turn a refusal into a verdict. Not
   done; it is the obvious next piece of work and it has a real cost model to
   measure.
3. **The invariant is not enforced anywhere.** Nothing stops a future prompt
   from splicing content in outside the block. The structural fix is for the
   pipeline to render *every* piece of prior content as a labelled source, so
   that "redactable" and "in the prompt" are the same set by construction. That
   changes every prompt in the pipeline and therefore every measurement, so it
   is a decision rather than a patch.

**Status:** partially fixed. Items 2 and 3 open.

## 17. A false clean is laundered into a `structural` clearance by carrier records

**Raised 13-09-2026 while measuring what the §7.5 failure actually cost.**

`record_carrier()` writes, for a message or tool call that hands an earlier
event's output onward, a check record with `method="structural",
confidence=1.0` — for both verdicts. The *tainted* direction is sound and
load-bearing (a verbatim copy of contaminated output is contaminated). The
**clean** direction is not a fact about the code path: it is inherited from
whatever the estimator concluded upstream.

`real_llm.code_path_pairs()` already knows this and filters carrier records out
of ground truth by note text, with a test pinning the phrase, on the explicit
reasoning that "reading them back as ground truth would score the estimator
against its own answers". **`checks.ClearancePolicy` does not apply the same
filter.** Its docstring describes `structural` as "Fact, not estimate" and
accepts it unconditionally, so an estimator error is re-stamped at the highest
trust level the system has.

**Measured effect on the reproduction.** The two cleared pairs at the Coder
became four cleared pairs: `S14->e0015` and `S14->e0016` inherited the
clearance as `structural`, confidence 1.0. The contaminated region came out
**empty** — a poisoned script and the Executor's final output preserved
byte-for-byte — against a true region of seven events.

**It does not manufacture new error**; a carrier record is exactly as right as
the verdict it inherits. What it does is make an estimator error invisible: a
reader auditing clearances by method sees two `counterfactual` clears and two
`structural` clears, and only the first two are the estimator's opinion.

**Status:** OPEN. Cheap fix available — either give carrier records their own
method name, or have `ClearancePolicy` read the note the way `code_path_pairs`
does. Not taken here because it is a change to the trace vocabulary
(`CheckRecord.method` is a controlled field) and belongs with the
`docs/02` vocabulary rather than inside a root-cause fix.
