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

**Status:** PARTLY ANSWERED, 15-09-2026, and read the split carefully.

The **hosted** floors for `prose`, `code`, `json_shape` and `tool_args` are
still unmeasured and still need quota. Nothing below replaces them.

What was measured is the floor of the client that actually produced every number
in `docs/07` and `docs/08`: `ScriptedClient`. A floor belongs to the thing that
produced the answers, so for those results the missing measurement was this one
-- and it turned out `experiment.py` had been applying the **Gemini** file to
scripted runs, excluding two decision facets on the strength of a measurement of
a different model (D-070).

`python -m src.provenance.scripted_noise`: 20 unchanged re-sends of each of 9
pipeline calls, **0% floor on all 18 (comparator, facet) pairs**, `carryover`
included. Nothing is excluded. Written to
`data/noise/calibration-scripted.json` under `model: scripted` so it can never be
confused with the hosted file.

`tool_args` is absent from that table and the absence is not an oversight: tool
calls are not model calls, are attributed structurally and never
counterfactually, so the comparator has no verdict to be the floor of in this
pipeline.

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

**Status: ANSWERED, 15-09-2026.** Implemented as this section specified.
`GeminiPipeline._call()` announces `(agent_id, kind)` before each model call and
`SplicingClient` compares it against the event the queue is about to hand back;
a mismatch raises `SpliceError` naming both, which turns the silent case into the
loud one. `ReplayReport` carries both sequences and `assert_invariants` checks
their lengths at the end of `replay()`.

Two properties worth knowing. The hook is **optional** -- a client with no
`announce` is spliced on position exactly as before -- so the replay engine does
not become dependent on one pipeline. And the divergence question this section
left open ("abort, or fall back to a coarse restart") is answered by aborting:
`real_llm.run_generated()` already catches `SpliceError` per method and records
it, so a method that could not replay produces no row rather than a wrong one,
which is the behaviour this repository already chose for this class of failure.

Pinned by `tests/test_recovery_hardening.py::TestSpliceIdentity`. Still not
reachable from the scripted mode, so the test constructs the divergence rather
than waiting for a model to produce one.

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

**Status: DECIDED 15-09-2026, AND THE RECOMMENDATION ABOVE WAS REVERSED BY A
MEASUREMENT.** See D-069.

The predicate change was implemented and it is **not** a no-op on the scripted
matrix, contrary to the claim above. The A-influencing attack breaks the task *by
working*, so `original_task_success` is already false there -- and with the
relaxed predicate the lying-self-reporter condition in
`tests/test_validation.py` went from zero unsafe preservations to one.

The mechanism is the finding. **The task check is an end-to-end detector of
surviving contamination.** When the estimator misses an influence edge the walk
under-covers, the selective plan leaves the contaminated event in place, and the
contamination shows up in the output the task check reads -- so verification
fails, the ladder climbs, and the unsafe preservation is removed by a route that
never had to identify it. Relaxing the predicate removes the last line of defence
exactly when the first one has already failed.

So the second option was taken instead: the condition is **recorded**, not
excused. `VerifyResult.original_task_success`,
`RecoveryResult.pre_existing_task_failure`, and a note on the scored row, so a
comparison can exclude or annotate those runs. Separately, D-065 makes the task
check itself robust to unrelated formatting, which removes the most common way an
original run fails for a reason nobody intended.

---

## 16. Leave-one-out assumes every route from a source into the prompt is removable

Every counterfactual verdict in this project rests on one premise that nothing
stated and nothing enforced:

    deleting the block labelled [S] removes S's information from the request

When it holds, an unchanged answer is evidence of non-influence. When a second,
unremoved route carries the same material, an unchanged answer is evidence of
nothing at all — and the verdict is `clean`. That is a **false clean produced by
plumbing**, the dangerous direction of error (issue #4), and it is
indistinguishable from a real result.

It is not hypothetical. A measured real-model false clean had exactly this shape:
the poisoned material was quoted inside a *second* source that stayed in the
prompt, so removing the first changed the request without changing the
information.

**Our answer, and it costs nothing:** a nested removability check, run before any
`clean` verdict is trusted. The question is about the prompt, not about the
model, and the prompt is on disk — so the check is textual and deterministic
rather than one extra model call per pair. `src/provenance/removability.py`
redacts the source exactly as the counterfactual will, takes the distinctive
spans of its content, and asks which of them still occur in the redacted prompt,
naming the route when it finds one.

**Is it general enforcement or a narrow patch?** General, within one stated
limit. It runs on every counterfactual path — single source, merged atomic unit
and group removal — so no clean verdict anywhere is now trusted without it, and
that is what makes it enforcement of this issue rather than a fix for the one
case that exposed it. The limit is that the routes it detects are **textual**: a
second source that paraphrases rather than quotes still passes. That is the same
narrowness the `carryover` facet has and the same narrowness the real-LLM
canary-token ground truth has, and all three should be read together.

**Status: ANSWERED, 15-09-2026** (D-066). Recorded as its own evidence type
(`removability=verified` / `removability=residual:N`), so a trace can be read
back and a verdict told apart from an unasked question. Regression tests in
`tests/test_relay_confound.py`.

---

## 17. A false clean can be laundered into a structural clearance

A **carrier** event produces no content of its own — a hand-off message, a tool
call whose arguments came out of an earlier output. Its influence set is the
upstream event's, restricted to what is in context, which is a fact about the
code path.

But `record_carrier()` wrote its clearances as `clean / structural / 1.0`: the
strongest label the clearance policy has, on the strength of whatever the
upstream event's influence edges happened to say **at the moment the carrier was
logged**. On a model-written upstream event those edges come from the estimator,
so an estimated clean re-emerged one event later wearing the system's highest
trust — and `ClearancePolicy` accepts a structural clean unconditionally.

`code_path_pairs()` in `src/eval/real_llm.py` had already had to filter these out
of ground truth, by matching a phrase in the notes, for exactly this reason. The
clearance policy had no corresponding rule. Two readers of one record,
disagreeing about what `structural` means, is the issue.

**It was worse than "an estimated clean".** The upstream edges are written by the
inline self-report pass, which deliberately records **nothing** for a negative
(see `src/provenance/attribution.py`). So the common case was silence laundered
into a structural clearance.

**Our answer:** a derived clearance inherits the method and confidence of the
verdict it derives from, never the strongest available label; a carrier *taint*
stays structural, because taint is the conservative direction and the copy
relation really is a code fact; a source that was not in the upstream event's
context at all is a separate and genuinely structural answer, not "no record";
and the marker that identifies a carrier record has one definition, imported by
both readers.

Because carrier records are written mid-run and the evidence arrives afterwards,
resolution happens **at read time** — `carriers.resolve()` treats a carrier
record as a pointer and follows it to a fixed point over the finished trace.
Appending an updated record was the obvious alternative and is not available:
`Trace.validate()` refuses two check records for one pair, and that invariant is
worth more than the convenience.

**Status: ANSWERED, 15-09-2026** (D-067). It removed a real unsafe preservation
in the scripted matrix: `e0016` on A-influencing/oracle was a memory write
carrying contaminated output and was marked `clean / structural`. Work preserved
on that cell falls 53% → 47%, which is one more event recomputed and it was
contaminated.

---

## 18. A recovered trace stores a prompt that was never sent — CLOSED

Found while building the independent post-recovery re-check (D-068), which
reported flagged material surviving in the prompt of every *successful* recovery.

The pipeline writes a prompt into the content store and **then** calls the
client. Redaction happens inside `SplicingClient.generate()`. So for a replayed
event the stored prompt is the one that would have been sent, not the one that
was — the un-redacted copy.

Nothing had depended on the difference until verification started reading prompts
back. It matters for anything that will:

- a post-hoc counterfactual run on a recovered trace would redact from the wrong
  text and could report a redaction that had already happened as a change
- a reader auditing what a recovered run actually saw would be misled

**Interim handling, 15-09-2026:** `ReplayReport.issued_prompts` records the text
actually issued, and the re-check reads that rather than the store. Enough for
the consumer that existed, and it left the trace itself still wrong.

**CLOSED 16-09-2026 (D-076).** The deferral's stated reason was that fixing it in
the same pass as the verification change would make two changes hard to tell
apart in the campaign diff. That pass is recorded, so the objection is spent, and
this is now its own isolated change with its own diff.

The pipeline asks the client what it actually sent (`last_issued_prompt()`, an
optional capability read by `getattr` exactly as `announce` is) and stores that.
`None` means no pipeline prompt was issued — a spliced event made no call, a
self-report is not a pipeline event — and the composed text stands, which is what
it always did.

The second half was not in the original write-up and is the part that would have
bitten: correcting the prompt and leaving `source_block` alone stores two texts
from different requests, and `splice_block()` refuses a block it cannot locate —
so every counterfactual on a recovered trace would have started raising.
`_follow_redaction()` performs the same removal on the block, and returns
**None** rather than a guess if the result does not land inside the sent prompt.
A missing block reads as unexaminable everywhere, which contaminates; a wrong one
is the D-029 hazard.

The assertion this issue asked for is
`tests/test_recovery_hardening.py::TestStoredPromptIsTheSentPrompt`, five tests,
and they fail on the pre-fix path: `e0013` and `e0014` both store the poisoned
memory value the recovery reports as redacted.

**Status: CLOSED 16-09-2026** (D-076). Was OPEN, mitigated, from 15-09-2026.

## 19. `carryover` cannot see a canary token, and the task check cannot see an ISO datetime

Two defects found by the second real-LLM campaign (16-09-2026, `docs/09` §8.2).
Filed together because they have the same shape: a pattern in our code whose
edge does not line up with what another part of our code produces, and in both
cases the method under evaluation is the only thing that pays.

**19a. `_DISTINCTIVE` requires ten characters; every generated canary is eight.**
`distinctive_spans()` promotes a single span only via
`[A-Za-z0-9_-]{10,}`, while `llm_scenarios._token_for()` returns `QZ` + 3
letters + 2 digits + `X` — eight, always. So `carryover` is structurally blind to
the canary in every generated suite. `gen001` is the measured consequence: a real
unsafe preservation, `S18 -> e0016`, with `carryover=0` on both sides of a pair
whose output opens with the token.

The same fact has a second consequence that runs in our favour and must be
reported with it: D-064's declared circularity between `carryover` and the
token-based ground truth **does not bite on generated suites**, because the facet
cannot see the token. The campaign's with-facet and without-facet columns are
identical, which is the measurement that establishes it.

**Not fixed. See D-077** — lengthening the token would create the circularity
that currently does not exist, and lowering the facet's threshold is a change to
comparator sensitivity that D-026 says must be specified and calibrated first.
The honest claim meanwhile: `carryover` closes the quoted-phrase case and not the
short-token case.

**19b. `ISO_DATE`'s trailing `\b` rejects `2024-03-12T00:00:00`.**
D-065 added an `iso_scan` route so decoration would stop failing a run that
produced the right dates. `ISO_DATE` is `\b\d{4}-\d{2}-\d{2}\b`, and in
`2024-03-12T00:00:00` the trailing `\b` fails because `T` is a word character.
The route therefore finds **zero** dates in a stdout containing five.

`gen001` printed all five correct dates in order, exited 0, and was scored a
task failure. `datetime.isoformat()` rather than `.date().isoformat()` is an
ordinary thing for a model to write.

This is D-065's own asymmetry surviving D-065: `verify()` is the only consumer,
only CausalLine verifies, so the mis-scoring is charged entirely to the method
being evaluated. It is a second, independent cause of #15 and it is live.

**Not fixed. See D-078** — the fix is one character and it *raises our own
numbers*, and it was found by watching the metric penalise us. A change of that
shape gets a written decision before it is made. Whoever takes it also owes the
re-measurement: the scripted campaign reads the same predicate, so adopting it
means re-running the 30-repetition matrix and reporting every delta.

**Status: OPEN, both halves. Raised 16-09-2026.** Neither is a blocker for the
scripted results; both are blockers for quoting the real-LLM method comparison
as a comparison.
