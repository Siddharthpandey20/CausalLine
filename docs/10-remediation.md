# 10 — Remediation Report (phase-gated pass)

Covers Phases 0–8 of `task.md`. Supersedes `docs/08` on the numbers it
re-measures, and `docs/03` on issues #12, #13, #15, #16 and #17.

**Everything here was measured in this pass.** Where something was not run, it
says so and says why. The real-LLM mode was deliberately not exercised: it costs
hosted-model quota, and nothing in Phases 0–7 needs it. `docs/09` stands
unchanged and §9 below says exactly which of its claims this pass moves.

Commands that regenerate every number:

```
python -m pytest                                  # 390 passed, 1 skipped
python -m src.eval.action_census                  # Phase 0
python -m src.provenance.scripted_noise           # Phase 2 (floors)
python -m src.eval.robustness                     # Phase 2 (repeats + control)
python -m src.eval.selfreport_value               # Phase 6
python -m src.eval.campaign                       # Phase 8, 30 reps, ~525s
python -m src.eval.economics                      # Phase 8
```

---

## 1. What this pass was, and the one thing to read first

Two audits were merged into `task.md`: a relay-confound investigation with a
proven root cause, and a 21-question architectural audit. The instruction was to
work phase by phase and attach evidence to each box rather than a claim.

**Read this first, because it changes how everything below should be read.** The
remediation brief described several fixes as already made and several decisions
as already specified — D-064, D-065, `docs/03` #16 and #17, a `relay_diagnosis`
module. **None of them exists in this repository.** `git log --all` ends at
`ebaf20c`, `docs/05` ends at D-061, and `docs/03` ends at #15. So the work
below is not "finish half-done fixes"; it is those fixes specified and built
from the brief's description of the failures, plus the audit's findings.

The two failures the brief names as `e0013` and `e0014` are real and are recorded
in `docs/08` §7.5: on `gen006`, the one real-LLM test whose payload actually
landed, the estimator cleared both pairs it examined while those very outputs
carried the canary token. That is the project's only measured unsafe
preservation and it is what Phase 1 is about.

---

## 2. Phase 0 — reconciling "the escalation machinery never runs"

**The two claims were about different quantities, and both are right.**
`src/eval/action_census.py` separates them and runs the same 24-configuration
matrix the audit used (3 scenarios × 2 variants × 4 detectors, seed 20260906).

| quantity | invalidate | replay | restart | isolate | restart_all |
|---|---|---|---|---|---|
| the planner's **first choice** | 75 | 24 | 0 | 0 | 0 |
| **every scope actually replayed at** | 75 | 24 | **4** | 0 | **4** |

- The audit's 73 / 22 / 0 / 0 / 0 is the **first-pass planner selection**, and
  it reproduces (75 / 24 here; the small difference is one extra configuration
  producing a plan under this pass's fixes).
- Of the 24 configurations: 10 produce no plan at all (the blind control flags
  nothing; the heuristic detector misses on four), 10 end at `selective`, and
  **4 climb the whole ladder** — replaying at `agent_restart` and then
  `restart_all` before ending `exhausted`.
- **A count of selected actions can never show escalation.**
  `invalidation_for_scope()` widens the set of events to recompute; the action
  vocabulary in `policy.py` is not consulted again. The machinery runs and
  leaves no mark on the thing the audit counted.

**Is the safe frontier inert, or just out-bid? Neither, cleanly (D-063).**

- **Not inert.** Of the 56 `(configuration, agent)` restart actions offered, the
  frontier made **14 (25%)** strictly cheaper than restarting that agent from
  INIT, removing **3044 tokens** of recompute in total.
- **Never wins, and not on price.** In all 11 configurations that produced a
  plan the best-scoring `restart(agent)` scored **exactly 1.0** on
  `cost / tainted-events-broken` — the same as the winning `invalidate` — and
  lost the deterministic `(cost, label)` tiebreak.
- **And the frontier does not lower that score.** In **0 of 11** did the
  frontier make the best-*scoring* restart cheaper, because the best-scoring
  restart is always a chain of events that spent no tokens and cost 1 each.

So the honest sentence for the paper: on an 18-event workflow whose cheap events
dominate the cover, agent-level restart is never the cheapest way to cut
contamination; the frontier's value is a bound on what that action *would* have
cost, not a selection it wins. The tiebreak was **not** adjusted to let
`restart` win — that would be tuning the planner until the machinery looks used.

---

## 3. Phase 1 — the comparator gap, and everything that turned out to be behind it

### 3.1 D-064, taken as a decision rather than a reflex

The question was whether adding a "quotes the removed source" facet immediately
after seeing what it would have caught violates D-026's rule against tuning a
comparator until it reports the answer we want.

**It does not, and the distinction is specific.** D-026's rule is about facets
included or excluded *on the basis of what they say about influence*.
`carryover` is **not a vocabulary**: it shingles the removed source's own content
(8-word shingles, plus any token of 10+ characters mixing letters and digits) and
asks which of those shingles occur in the answer. There is no list to extend, no
term to add, no threshold to move. A hand-written "does the output contain the
payload" rule would have been the violation. It is also self-cancelling on
redundancy — a span also present in a source that stayed appears on both sides of
the comparison, so the facet holds still and cannot manufacture influence out of
shared boilerplate.

**The cost, stated rather than buried.** The real-LLM mode's ground truth is
canary-token presence unioned with code-path records. `carryover` asks a question
of the same shape. **So on a real-LLM run the estimator and the ground truth now
share a mechanism.** Any future real-LLM pair-level agreement number must either
exclude `carryover` or be reported as an instrument scored against a relative.
The scripted matrix is unaffected: its ground truth is `ScriptedClient`'s own
leave-one-out record over a substance function, which has nothing to do with
quoted spans.

**Measured effect on the scripted matrix: none.** Adding the facet changed no
cell's work preserved, unsafe count or escalation count. It is dormant here and
exists for the failure mode the real model produced.

### 3.2 The nested removability check (`docs/03` #16, D-066)

Leave-one-out is only sound when deleting the block labelled `[S]` actually
removes S's information. Nothing enforced that anywhere.

The proposal was one extra model call per pair. **That is not necessary and is
weaker than what was built.** The question is about the prompt, not the model,
and the prompt is on disk. `src/provenance/removability.py` redacts exactly as
the counterfactual will, takes the same distinctive spans `carryover` uses, and
asks which survive in the redacted prompt — naming the route (a sibling source,
or text outside the source block). **Zero calls**, and strictly stronger: a model
answering the same way twice proves nothing, a span found in the redacted prompt
proves the route exists.

**The asymmetry is deliberate and it is what the brief warned about.** The
counterfactual still runs and its result is still kept, because a *changed*
signature is evidence of influence whether or not the removal was clean.
Short-circuiting before the call — the brief's account of Investigation A's first
failed attempt — destroys every real positive edge the call would have found.
What a failed removability check forbids is only the other verdict.

**General enforcement, within one stated limit.** It runs on every counterfactual
path: single source, merged atomic unit (D-051) and group removal. So no clean
verdict anywhere is now trusted without it. The limit is that the routes it
detects are **textual** — a second source that paraphrases rather than quotes
still passes. That is the same narrowness `carryover` has and the same narrowness
the canary-token ground truth has, and the three should be read together.

Recorded as its own evidence type: `removability=verified` /
`removability=residual:N` in the check record's notes, read back with
`removability.verdict_of()`. A record written before this existed reads
`unchecked`, which is correct — nobody asked.

### 3.3 Carrier laundering (`docs/03` #17, D-067) — worse than described, and it was hiding a real unsafe preservation

The audit said an estimated false clean could be laundered into a `structural`
clearance. **The common case was worse: it was silence being laundered.** The
upstream influence edges a carrier inherits are written by the inline
self-report pass, which deliberately records *nothing* for a negative. So
`record_carrier()` was writing `clean / structural / 1.0` — the strongest label
the clearance policy has — on the strength of no verdict at all.

Three parts to the fix:

1. **One definition of the marker.** `CARRIER_NOTE` lives beside the function
   that writes it; `code_path_pairs()` and `ClearancePolicy` both import it. Two
   copies of the test is how they came apart.
2. **A carrier clearance inherits method and confidence.** Upstream structural →
   structural; upstream counterfactual → counterfactual at the same confidence;
   nothing recorded → `assumed` at 0.0, which no policy accepts. A carrier
   *taint* stays structural — the copy relation is a code fact and taint is the
   conservative direction.
3. **"Not in the upstream context at all" is a third answer, and it is
   load-bearing.** An output cannot have been influenced by something never in
   front of it, so that clearance genuinely is structural. Collapsing it into "no
   record" cost 42 points of work preserved on one cell before it was separated
   out.

Resolution happens **at read time and writes nothing**. Carrier records are
written mid-run, before any counterfactual exists. Appending an updated record is
not available — `Trace.validate()` refuses two check records for one pair,
because two verdicts on one pair means one is stale and nothing in the file says
which — and that invariant is worth more than the convenience. So a carrier
record is treated as what it always was, a **pointer**, and `carriers.resolve()`
follows the pointers to a fixed point.

**This removed a real unsafe preservation from the scripted matrix.** On
A-influencing/oracle, `e0016` is a memory write carrying the Coder's contaminated
decision. It was marked `clean / structural` for `S10` because the counterfactual
establishing `S10 → e0014` had not run yet when the carrier record was written.
It is now correctly invalidated. That single event is most of the campaign delta
in §8.

### 3.4 Post-recovery verification now reads the bytes (Q20, D-068)

Verification cleared a replayed event of a flagged source on one ground:
`redact_flagged()` had been called. That is an argument, not a check, and it is
the argument *both* measured false cleans defeated.

`surviving_payload()` now scans the re-issued prompt (the e0014 class) and the
recovered output and tool arguments (the e0013 class) for distinctive spans of
the flagged source's content, using the same extraction as `carryover` and the
removability check. A hit withholds the clearance → the pair stays contaminated →
verification fails → the run escalates.

**Would it have caught e0013/e0014-style cases before the Phase 1 fix?** Yes, and
by construction rather than by luck: e0013's signature is *the flagged source's
material is in the recovered output*, which is the second test; e0014's is *the
flagged source's material is still in the prompt we issued*, which is the first.
Both are checked independently of whatever the estimator concluded. The
constructed reproductions in `tests/test_relay_confound.py` and
`tests/test_recovery_hardening.py` fail on the pre-fix code path and pass on the
current one.

**Building it immediately found a defect, which is the point.** The first run
reported surviving payload on *every successful recovery*. The pipeline writes a
prompt into the content store and **then** calls the client, and redaction
happens inside the client — so for a replayed event the stored prompt is not the
prompt that was sent. `ReplayReport.issued_prompts` now records what was issued.
The store is still unfaithful and that is logged as `docs/03` **#18**, open and
mitigated.

Also tightened: the walk's inherited clearances are read through `CheckLedger`
under the same policy the walk uses, not off the raw `verdict == "clean"` field.
Verification had been more lenient than the thing it was verifying.

### 3.5 D-065 and the reversal on `docs/03` #15

`task_outcome()` now accepts a run whose stdout contains the right ISO dates in
the right order even with a banner or a prefix around them. A wrong date, a
missing one, a duplicate, an extra one or a different order still fails. Only
decoration is forgiven, and the trace records which route decided it.

**The predicate change `docs/03` #15 recommended was implemented, measured, and
reversed.** #15 asserted that relaxing `ok` to
`task_success or not original_task_success` would be "a no-op on every existing
measurement". **It is not.** In the scripted matrix the A-influencing attack
breaks the task *by working*, so `original_task_success` is already false there —
and under the relaxed predicate the lying-self-reporter condition in
`tests/test_validation.py` went from **zero unsafe preservations to one**
(`e0010`, preserved while truly contaminated).

The mechanism is the finding and it generalises. **The task check is an
end-to-end detector of surviving contamination.** When the estimator misses an
influence edge, the walk under-covers, the selective plan leaves the contaminated
event in place, and the contamination shows up in the output the task check
reads. Verification fails, the ladder climbs, and the unsafe preservation is
eliminated by a route that never had to identify it. Relaxing the predicate
removes the last line of defence exactly when the first one has already failed.

So #15's *second* option was taken: the condition is **recorded, not excused** —
`VerifyResult.original_task_success`, `RecoveryResult.pre_existing_task_failure`,
and a note on the scored row — so a comparison against three methods that never
verify can exclude or annotate those runs. The safety rule is not loosened to
make a table fairer; the table is labelled. (D-069.)

---

## 4. Phase 2 — turning the robustness machinery on

**Two things were found, and the first one is that the machinery was broken.**

`control_run` compared a `before` signature carrying the removal-aware facet
against a control signature computed without it, so the facet differed every
single time, **every event read as unstable, and every verdict fell back to
"influenced"**. A control that always fires is not a control. Fixed, and pinned
by `tests/test_recovery_hardening.py::TestControlRunSymmetry`. This is what "the
mechanism has never been exercised in a reported result" costs: it was wrong and
nothing would have said so.

`repeats` and `control_run` also existed only on the single-source path, while
group testing is on by default — so on every reported run they were
*unreachable*. Both now work on `CounterfactualDecision` too, with the control
cached per **event** (it is a property of the request, not of which subset is
being tested).

### 4.1 Noise floors for the remaining comparators (`docs/03` #12, D-070)

The hosted floors for `prose`, `code`, `json_shape` and `tool_args` are still
unmeasured and still need quota. What this pass measured instead is the floor of
the client that produced **every number in `docs/07` and `docs/08`**:
`ScriptedClient`.

That turned out to matter, because `experiment.py` called `Calibration.load()`
with **no model argument** — so the guard written to refuse cross-model transfer
never fired, and every scripted verdict was scored under a **Gemini** calibration
that excluded the decision comparator's `strategy` and `dependency` facets.
Excluding a facet is the one place the method knowingly trades safety for signal.

`python -m src.provenance.scripted_noise`: 20 unchanged re-sends of each of 9
pipeline calls in the long workflow.

| comparator | facets measured | floor |
|---|---|---|
| `decision` | library, output_format, strategy, dependency, carryover | **0%** |
| `prose` | terms, formats, output_format, dependency, carryover | **0%** |
| `code` | imports, calls, formats, control, carryover | **0%** |
| `json_shape` | keys, counts, carryover | **0%** |

18 of 18 (comparator, facet) pairs at a 0% floor. Nothing is excluded.
`data/noise/calibration-scripted.json` carries `model: scripted` and is loaded
with the model named, so a future mix-up raises.

Two honesty notes. `tool_args` is absent because tool calls are not model calls
and are attributed structurally, so the comparator has no verdict to be the floor
of. The `code` comparator's `stdout`/`returncode` facets are absent because no
runner is supplied in that context. And a methodological one worth keeping: the
first version of this measurement built a *fresh* client per trial, which resets
the counter the churn is derived from, so every "re-send" came back identical and
the 0% was an artefact of the harness. The number above is from the corrected
version, one client across all trials.

### 4.2 What turning them on costs, and whether it catches anything

`python -m src.eval.robustness`, six CausalLine cells at the oracle detector:

| metric | as reported | repeats=3 + control | delta |
|---|---|---|---|
| analysis tokens (total) | 4500 | 6900 | **+2400** |
| replay tokens (total) | 800 | 800 | 0 |
| recovery tokens (total) | 5300 | 7700 | +2400 |
| work preserved (mean) | 78.9% | 78.9% | 0.0% |
| unsafe preservations | 0 | 0 | 0 |
| escalations | 0 | 0 | 0 |
| **A / N** | **1.25** | **1.92** | **+0.67** |

**The honest answer to the open question: no. The control run caught no
additional relay-confound-style case.** No event's signature moved on an
unchanged re-send, no verdict changed, and no cell's answer moved.

That is the expected result given the measured 0% floor — a control cannot fire
on a client that holds still — and it is worth stating as a *measured zero*
rather than an assumption. It also means the cost is currently pure: **A/N rises
from 1.25 to 1.92**, a 53% increase in analysis cost for no change in outcome, on
this testbed. Against a hosted model with a real floor the trade would be
different, and that is exactly the experiment the quota is for.

**Default: both stay off**, because turning them on buys nothing measurable here
and makes `docs/03` #7's ratio worse. They are now reachable, tested, and
reportable, which is the thing that was missing.

---

## 5. Phase 3 — the two structural fragilities

**`SplicingClient` now checks identity, not just position (`docs/03` #13).**
`GeminiPipeline._call()` announces `(agent_id, kind)` before each model call;
`SplicingClient` compares it against the event the queue is about to return and
raises `SpliceError` naming both on a mismatch. `ReplayReport` carries both
sequences and `assert_invariants()` compares their lengths at the end of
`replay()`. The hook is **optional** — an un-announcing client is spliced on
position exactly as before — so the replay engine does not become dependent on
one pipeline. Divergence aborts rather than falling back to a coarse restart,
which is the behaviour this repository already chose for this class of failure:
`real_llm.run_generated()` catches it per method, so a method that cannot replay
produces no row rather than a wrong one.

**`redact_flagged()` now fails loudly.** A source that is not in the prompt is
still skipped — nothing to redact is not a failed redaction. A source that *is*
rendered and cannot be removed raises `RedactionError`, and there is a
post-condition check that no flagged id is still rendered when the function
returns.

The test for this documents what the old behaviour actually produced, which is
worse than "no-op": given a prompt containing two renderings of the same source,
the permissive path removes **one** of them and returns a partially redacted
prompt with the payload still in it, reporting nothing. That is the e0014
mechanism class in its purest form.

---

## 6. Phases 4, 5 and 7 — three boundaries, decided

**Phase 4, upstream attribution (D-073).** `src/provenance/upstream.py` walks
back over `derived_from`, using the trace's influence edges into the producing
event where they exist and that event's exposures where they do not. On the
scripted A-influencing run it takes the Coder's script back through the
Researcher's findings to the planted page — **two hops, correctly surfaced**,
without reading `Source.malicious`. That is the constructed test the brief asked
for, and it did not need constructing.

It **surfaces and stops**. Candidates go onto `RecoveryResult.notes`; nothing is
flagged, nothing seeds the contamination walk, no recovery decision changes. The
relation walked is "was an input to", which is **exposure** — promoting its output
to a seed would re-import the exact conflation this project exists to remove and
would grow the region back towards B2's. `origin_event` is deliberately not
walked: a web page is not derived from the tool response that fetched it.

**Phase 5, the detector interface (D-074).** Confidence now orders the sources
*within* an event, ascending — least certain flag first, because clearing a
doubtful flag removes a whole downstream region while confirming a certain one
mostly re-derives taint. It does **not** reorder which event is taken next; that
is the frontier expansion and it is forward for a reason.

The scope statement is now permanent and lives in `docs/02-architecture.md` and
`docs/01-scope.md`: **this system is post-hoc, batch recovery.** No online
mid-execution path exists or is half-built, and the reason is about evidence
rather than effort — counterfactual replay needs an output that already exists.
The same edit corrects `docs/02`'s opening diagram, which said the detector
reports "agent X compromised". It never did: `Verdict` is a map from source id to
confidence and cannot express an agent-level claim. That difference is not
cosmetic — an agent-level verdict *is* baseline B1.

**Phase 7, memory rollback (D-071).** Wholesale reset is not a shortcut here.
This testbed's replay re-executes the whole workflow from its starting state, so
seeding it with writes the original run made would show an early `memory_read` a
value the original never saw — the replay would stop being a replay.

But a deployment asks a different question, and there the audit is right.
`selective_memory_rollback()` returns `{key → value to restore, or None to
delete}` covering only keys an invalidated write touched, reverting each to the
last *surviving* write, then the fixture value, then removal. A key written only
by surviving events **does not appear at all** — absent means it stays.
`RecoveryResult.memory_rollback` carries it and the notes name what was undone
and what was kept. Tested in `tests/test_scope_boundaries.py`, on a hand-built
two-write trace, because the four-agent testbed writes memory exactly once and
cannot express the case.

---

## 7. Phase 6 — self-report, re-measured, and the docs were wrong

`targeted_only` is `hybrid` with the inline self-report removed and nothing else
changed. It is the only comparison that isolates the triage; the existing
`--ablation` modes also skip the targeted pass, so they differ in two things at
once. Six CausalLine cells, oracle detector:

| | hybrid | targeted_only | delta |
|---|---|---|---|
| analysis tokens | 4500 | 1600 | **−2900** |
| recovery tokens | 5300 | 2500 | **−2800** |
| work preserved (mean) | 78.9% | 73.7% | −5.3% |
| unsafe preservations | 0 | 0 | 0 |
| pair-level false negatives | 0 | **1** | +1 |
| escalations | 0 | **1** | +1 |

**Self-report is not a cost reducer. It costs 2800 recovery tokens across these
cells and it buys safety.** Its over-claimed positives are recorded as `tainted`
without verification, so they taint pairs the targeted pass alone misses — which
is why removing it produces a pair-level false negative and an extra escalation.

**Decision: keep, and describe it correctly** — a conservative bias bought with
tokens, not a cost-ordering heuristic (D-072). The design note in
`src/provenance/selfreport.py` and `docs/03` #7 describe it as cost triage; on
this testbed that is not what it does.

---

## 8. Phase 8 — the fresh campaign, and every delta explained

### 8.1 Test suite

**390 passed, 1 skipped, 101 subtests passed**, up from 341 / 1 / 101 at
`ebaf20c`. 49 new tests across `tests/test_relay_confound.py` (18),
`tests/test_recovery_hardening.py` (21) and `tests/test_scope_boundaries.py`
(10).

One existing test's **fixture** changed and no existing test's **expectation**
did. `tests/test_escalation.py` forced the ladder to climb by poisoning a run and
flagging nothing; D-069 examined exactly that route, so the fixture was replaced
by one that does what the rule actually forbids — a run that *passed* the task,
and a recovery that breaks it by removing a source the workflow needs.

### 8.2 The 30-repetition campaign

96 cells × 30 repetitions, 524s, `data/results/campaign-30x-remediation.txt` and
`data/results/campaign.json`. Compared against the stored reference,
`data/results/campaign-d051.txt`, which is the run `docs/08` §1 quotes.

**Baselines: 0 of 72 cells changed.** B0, B1 and B2 are byte-identical across
every detector, scenario and variant. Every delta below is CausalLine's alone.

**CausalLine: 7 of 24 cells changed, all of them downward.**

| detector | scenario | variant | stored (D-051) | this pass | Δ |
|---|---|---|---|---|---|
| oracle | A | influencing | 44.2% ± 4.4% | **42.8% ± 4.1%** | −1.4 |
| oracle | B | influencing | 65.1% ± 1.0% | **63.2% ± 0.0%** | −1.9 |
| oracle | C | influencing | 66.5% ± 1.1% | **63.2% ± 0.0%** | −3.3 |
| heuristic | A | influencing | 44.2% ± 4.4% | **42.8% ± 4.1%** | −1.4 |
| heuristic | C | influencing | 66.5% ± 1.1% | **63.2% ± 0.0%** | −3.3 |
| pessimistic | B | influencing | 65.1% ± 1.0% | **63.2% ± 0.0%** | −1.9 |
| pessimistic | B | exposed_only | 71.8% ± 1.1% | **63.2% ± 0.0%** | −8.6 |

The other 17 CausalLine cells are identical, including all six exposed-only
oracle cells and every blind-control cell.

**Every delta has the same cause, and it is D-067.** A carrier clearance that
used to be written as `clean / structural / 1.0` on the strength of no upstream
verdict now inherits what is actually recorded. On the influencing cells that is
`e0016`, the Coder's memory write, which carries the contaminated decision:
previously preserved, now correctly invalidated. One more event recomputed out of
19 is 5.3 points, which is the size of the largest deltas; the smaller ones are
the same event appearing in fewer of the 30 repetitions.

Three second-order observations, all of them checks that the delta is what it
claims to be:

1. **Recovery tokens are unchanged in all seven cells** (e.g. oracle/A/inf stays
   1300 ± 29.4). The newly-invalidated event is a memory write — recomputed by
   pipeline code, no model call — so the blast radius grew and the token cost did
   not. That is the correct signature for "one more code-computed event", and it
   would not hold if something else had moved.
2. **Four cells became deterministic**: their 95% CI collapsed from ±1.0–1.1 to
   **±0.0**. Carrier verdicts used to depend on whatever the inline self-report
   happened to record, which varies with the seed; they now resolve against the
   refined counterfactual verdicts, which do not. The answer no longer depends on
   the cheap stage's random errors — an improvement in the result's meaning, not
   just its variance.
3. **`pessimistic/B/exposed_only` moved most (−8.6)** and it is the cell where
   the pessimistic detector flags a whole channel, so more carrier records sit
   inside the region and more of them stop clearing.

**Safety, unchanged and still the headline:**

- **Zero event-level unsafe preservations for CausalLine across all 24 cells ×
  30 repetitions.** Same as the stored run.
- The only cells recording any unsafe preservation are **B1 and B2** under
  `blind` (all three influencing scenarios) and under `heuristic` on
  `B-influencing` — identical to the stored run.
- Pair-level unsafe rate: **1.8% on the four A-influencing cells**, 0% everywhere
  else. Unchanged.
- Total mean escalations across the 24 CausalLine cells: **16.27**, against the
  16 `docs/08` reports. Unchanged.

### 8.3 Economics, re-run fresh

`python -m src.eval.economics`. Against the stored `economics.json`:

| | stored | this pass |
|---|---|---|
| N (restart tokens) | 600 | 600 |
| A, inline attribution | 800 | 800 |
| A, targeted attribution | 200 | 200 |
| f (contaminated fraction) | 0.675 | 0.677 |
| storage per run | 26470 B | 26499 B |
| inline: selective wins | 0 / 24 | 0 / 24 |
| targeted: selective wins | 12 / 24 | 12 / 24 |
| attack-rate threshold | none | none |

**Materially unchanged.** `f` moves by 0.002 and storage by 29 bytes, both
consistent with the one extra invalidated event. `docs/03` #7's uncomfortable
answer stands: on this testbed the check still costs more than the rerun, and no
attack rate makes the storage tax worth paying.

### 8.4 The real-LLM diagnostic: not run, and what that means

`task.md` asks for a re-run of the real-LLM diagnostic to confirm e0013 is
resolved and e0014 remains resolved. **It was not run.** Two reasons, and the
second is the binding one:

1. It spends hosted-model quota, which this pass was asked to avoid.
2. **`carryover` would make that particular confirmation circular.** Ground truth
   in the real-LLM mode is canary-token presence; `carryover` asks a question of
   the same shape (§3.1). So "the estimator now catches e0013" measured against
   that ground truth is partly true by construction, and quoting it would be
   exactly the kind of number this project refuses elsewhere.

What was done instead is stronger for the purpose: both failure shapes are
reproduced as **deterministic regression tests** that fail on the pre-fix path
and pass now — e0013's in `tests/test_relay_confound.py::TestCarryoverFacet`
(with the facet excluded via the pre-registered calibration mechanism, the
verdict is `clean`; with it, `tainted` and `carryover` is the facet that moved),
and e0014's in `TestRemovability` and
`tests/test_recovery_hardening.py::TestRedactionFailsLoudly`.

**What is still owed and cannot be paid without quota:** a real-LLM campaign
under the current code, scored with `carryover` excluded, to confirm the fixes
behave on a real model rather than on a reproduction of one. That is the next
thing to spend quota on, and it should be done before any real-LLM number from
this pass is quoted.

---

## 9. What can now be said that could not be said before

**Claims this pass earns.**

1. **A clean verdict is no longer trusted without checking that the removal
   removed something.** Before: every counterfactual verdict rested on an
   unstated, unenforced premise, and a false clean produced by a second
   unremoved route was indistinguishable from a real result. Now that premise is
   checked on every counterfactual path, for free, and the check is recorded as
   its own evidence type. (`docs/03` #16, D-066.)
2. **No clearance in the system now outranks the evidence behind it.** Before:
   the absence of an upstream verdict was being written into the trace as a
   `structural` clearance at confidence 1.0, the strongest label the policy has,
   and the two components that read those records disagreed about what the label
   meant. That is fixed at the point of writing and at the point of reading, and
   fixing it removed a genuinely contaminated event from the preserved set.
   (`docs/03` #17, D-067.)
3. **Post-recovery verification is independent of the recovery plan.** Before:
   it cleared events because redaction had been *called*. Now it reads the
   re-issued prompt and the recovered output. Building it immediately exposed a
   trace-fidelity defect nobody knew about (`docs/03` #18).
4. **The comparator can represent verbatim carry-over, and the price of that is
   declared.** One slice of `docs/06` §2.1's blind spot is closed; the rest is
   not; and the circularity it creates with real-LLM ground truth is written
   down rather than discovered later by a reviewer. (D-064.)
5. **The scripted results are calibrated on the client that produced them.**
   Before: a Gemini-measured floor was silently applied to scripted runs,
   excluding two facets for no reason that applied. Now measured at 0% across all
   18 (comparator, facet) pairs, on the client that produced every number in
   `docs/07` and `docs/08`. (`docs/03` #12, D-070.)
6. **Four campaign cells are now deterministic that were not.** The answer no
   longer depends on which self-report claims the seed happened to get wrong.
7. **The escalation and frontier machinery is measured rather than argued
   about.** `restart(agent)` and `restart_all` each run 4 times across the
   24-configuration matrix; the frontier makes 25% of restart actions strictly
   cheaper; and it never wins the greedy, by a tie rather than by price. (D-062,
   D-063.)
8. **Three implicit boundaries are now written scope decisions**: upstream
   attribution surfaces and does not seed (D-073); the system is post-hoc and
   batch, and `docs/02` no longer says the detector reports "agent X
   compromised" (D-074); selective memory rollback is the deployment's answer and
   wholesale reset is the replay's (D-071).
9. **Two design claims were contradicted by measurement and corrected rather
   than left standing**: `docs/03` #15's "this change is a no-op" (it costs an
   unsafe preservation, D-069), and self-report as a cost-ordering heuristic (it
   is more expensive and buys safety, D-072).

**Claims this pass does NOT earn, and must not be made.**

- **Not "the relay confound is fixed on a real model."** It is fixed against
  deterministic reproductions of both failure shapes. No hosted model has been
  run under this code. §8.4.
- **Not "the estimator's real-LLM pair-level agreement improved."** That number
  cannot be computed honestly with `carryover` active, because it now shares a
  mechanism with the ground truth it is scored against.
- **Not "false cleans are eliminated."** Three narrownesses remain and they are
  the same narrowness: `carryover`, the removability check and the canary-token
  ground truth are all **textual**. An influence that is paraphrased rather than
  quoted is invisible to all three.
- **Not "the robustness machinery catches confounds."** It caught none here. Its
  measured contribution on this testbed is a 53% increase in analysis cost for
  no change in outcome — which is a real result about a 0%-floor client, not
  evidence about a hosted one.
- **Not "the safe frontier pays for itself."** It prices 25% of restart actions
  lower and never wins a selection.
- **Not "work preserved improved."** Seven of 24 campaign cells went **down**,
  because one contaminated event stopped being preserved. That is the direction
  a safety fix should move a preservation metric, and reporting it as a loss is
  the honest framing.
- **Nothing here touches `docs/06` §4.** The analysis still does not pay for
  itself: A/N is unchanged, and turning on the Phase 2 machinery would raise it
  from 1.25 to 1.92.
