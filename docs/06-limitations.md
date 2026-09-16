# 06 — Limitations

What this system does not do, cannot do, and assumes. Written to be lifted
into the paper's limitations section largely as-is.

Two rules for this file. Everything here is either a **designed assumption**
(we chose a boundary, and the choice has a reason) or a **measured weakness**
(we ran it, it did this). Nothing here is a defect discovered by a reviewer and
retrofitted with a justification, and nothing here is speculative — if we have
not measured it, the entry says so.

`docs/03-open-issues.md` is the working list of things we are still arguing
about. This file is the settled list of things that will still be true when the
paper is submitted.

---

## 1. Recovery cannot undo an irreversible real-world side effect

**This is a designed assumption, not a discovered flaw.**

CausalLine recovers *in-system* state: memory entries, plans, agent context,
un-executed tool calls, and the outputs of any event it recomputes. It does
this by replaying from a checkpoint, which works because every one of those
things is a value the system owns and can overwrite.

An action with an effect outside the system has no such handle. If a
contaminated Executor sent an email, transferred money, deleted a file,
published a commit, or called any API that changed someone else's state, then:

- the trace records that it happened
- the contamination walk correctly identifies the event as contaminated
- recovery correctly invalidates and recomputes it
- **and the email is still sent.**

Replay produces a corrected *plan* for what should have happened. It does not
produce a corrected world.

**The consequence for deployment, stated as a requirement rather than a
caveat:** recovery must run *before* the Executor stage takes an action with a
real-world effect. In the architecture of `docs/02-architecture.md`, the
Executor is the last agent and its only side effect is running generated code
in a sandbox, so the testbed satisfies this by construction. A deployment
whose agents act on the world mid-workflow needs one of:

- a **staging discipline**: irreversible actions are queued, not executed, and
  the queue is flushed only after the workflow completes and verifies. This is
  the transactional-outbox pattern and it is what we would build next.
- **compensating actions**: an explicit inverse for each side effect (refund
  for charge, retraction for send). These exist for some operations and not
  for others, and "not for others" is the general case.
- accepting that the blast radius of an incident includes everything already
  executed, and sizing the detector's latency budget accordingly. Our measured
  detection latency (mean 8 events between injection and alarm, Phase 9) is
  the number that decides how much has already escaped.

**What we do not claim:** that CausalLine reduces the harm of an executed
action. It reduces the *cost of repairing the workflow* around it.

---

## 2. Influence is estimated, and estimates can be wrong in the dangerous direction

The core problem is unchanged from `docs/03-open-issues.md` #1: we cannot see
inside an LLM call. Everything the method preserves rests on a `clean` verdict,
and a wrong `clean` is an unsafe preservation.

**Now demonstrated on a real model, not only on the scripted one.** In the
first real-LLM campaign (11-09-2026) exactly one test had a payload the model
actually obeyed. On that test the estimator cleared **both** of the pairs it
examined while the corresponding outputs demonstrably carried the canary token
— a pair-level unsafe rate of 2/2. n=2 on one run is far too small to be a
rate, and it is an existence proof of the thing this section describes,
produced by a model rather than by a rule we wrote. `docs/09` §7.5.

Four distinct ways a `clean` verdict can be wrong, all measured:

### 2.1 The comparator vocabulary has a blind spot

Counterfactual verdicts compare **decision signatures**, never text — D-026
measured a 100% text-level noise floor, so a text comparison carries zero
information. The signatures are built from vocabularies fixed in advance
(`src/provenance/signatures.py`), and that pre-registration is what stops them
being tuned until a result comes out right.

It also means an influence expressed in words not on the list is invisible.
Phase 13.2's token validation demonstrates this directly: a payload whose only
effect is to make the agent emit a nonsense token moves no facet of
`ProseComparator`, so the counterfactual reports "signature unchanged" and
clears a pair that really was influenced.

Phase 13.1 found the same failure from the other direction: on scenario A,
`S3 -> e0010` was cleared by a counterfactual because the surviving text still
said "the ambiguous samples are day first" (a fact from the database) while
having lost "always print dates in DD-MM-YYYY" (the injected directive).
`output_format=day_first` matches both, so the facet could not tell a fact from
an instruction.

**Confirmed on a real model, 13-09-2026 (D-064).** This was previously
demonstrated only by Phase 13.2's token validation, a construction of ours. It
is now the proven cause of one of the two unsafe pairs in `docs/08` §7.5:
Nemotron quoted a planted token into the Coder's decision, redaction removed
the source correctly, the re-run's output genuinely lost the token — and every
facet of every comparator held still. Checked mechanically across all five
comparators and with the calibration's exclusions removed, so this is a
property of the vocabularies and not of one facet or one model:
`python -m src.eval.relay_diagnosis`.

**The consequence for how §7.5 must be read.** The ground truth
(`observed_influence`) calls a pair influenced when the token appears in the
output: a *text-level* relation. The estimator asks whether the *decision*
moved. Where those differ the disagreement is scored as an unsafe preservation,
correctly — contaminated text propagates, and in that run it propagated to the
Executor's final output — but the *reason* is a definitional gap between the
instrument and its yardstick, not a coding error, and the paper has to say so.

**The one facet that would have caught it is never switched on.**
`CodeComparator`'s behavioural half needs a runner in `context["run"]`, supplied
via `run_code` — a parameter **no caller in this repository passes**. Every
code-comparator verdict ever recorded is AST-only. Wiring it is the first item
of future work and it *lowers* our own numbers, since it finds more influence.

**Not fixable by extending the vocabulary**, which would be exactly the
after-the-fact tuning the pre-registration rule forbids. The fix is a graded
signal — how far a signature moved, facet by facet — and that is future work,
not something half-done in this repository.

### 2.2 Redundant sources are individually unnecessary

Counterfactual influence is leave-one-out, so when two sources supply the same
fact, removing either one changes nothing and **both** are called clean
(D-030). This is a property of counterfactual influence, not of our
implementation. Testing every subset is exponential and we do not do it.

**Partially addressed (D-051, 09-09-2026).** This stopped being a footnote when
the workflow got longer. With five Researcher findings in the Coder's context
instead of three, no single finding was individually necessary, the script was
attributed to a memory source alone, contamination never reached the Executor,
and A-influencing/oracle recovered **11.1%** against B1's 14.8% — the method
losing a cell it should win.

Where redundancy is **recorded in the trace**, we now merge the redundant
sources into one atomic unit that is removed together and never split, in
leave-one-out and inside the recursive halving alike. Two recorded shapes
qualify: a summary sitting beside its own inputs, and two summaries of an
upstream source that is not itself in context (the shape that actually bit —
the findings share the poisoned page as an ancestor, but the page is not in the
Coder's context, so no finding is derived from another). Measured: the cell
moves **11.1% → 37.0%**, escalations 1 → 0, and it now beats B1 by +22.2
points. Across the 48-configuration matrix the merge fires in 11 of them,
forming 25 units over 82 sources, with **zero** change to any short-workflow
cell.

**What is still not handled, and why we stop here.** Two sources that state the
same fact with *no recorded provenance link between them* are not merged and
are not caught. Our own scenarios sidestep this because the poisoned source
carries a directive no clean source carries, so it stays individually
necessary — a fact about our fixtures, not a property of the method, and it
must not be presented as one.

Closing that gap is not an engineering oversight. It is the known limit of
**single-variable counterfactual testing**, and it is precisely why Halpern and
Chockler's actual-causality framework exists. Their definition of an actual
cause quantifies over *contingencies*: `X = x` is a cause of `φ` when there is
some setting of a subset of the other variables under which changing `X`
changes `φ` — the "AC2" condition. Naive but-for testing, which is what
leave-one-out is, is exactly the special case where that subset is empty, and
it is exactly the case that fails under redundancy: with two symmetric
over-determining causes present, neither is a but-for cause of the outcome even
though together they determine it. Halpern's modified definition (2015) makes
this explicit by requiring a witness subset, and finding one is
`Sigma-2-complete` in the general case (Eiter and Lukasiewicz; Aleksandrowicz
et al.) — which is the formal statement of "testing every subset is
exponential".

So the honest position is: our recorded-provenance merge closes the subset of
this problem where the trace already tells us which sources are redundant, at
no extra counterfactual calls. The remainder is not a bug we have not got
around to; it is a complexity result. A trace-based system can be *sound* about
recorded redundancy and can only ever be heuristic about the rest, and any
future work here should be framed as choosing which contingencies to test
under a budget, not as making leave-one-out complete.

### 2.3 Group testing assumes no interaction effects

Phase 11.2 removes candidates in groups to cut the counterfactual cost. Two
sources that jointly cause an effect neither causes alone are missed whenever
the split puts them in different halves. `GroupTestDiagnostics.interaction_suspected`
counts the signature of this happening — a group that mattered whose halves
both came back clean — and reports it rather than swallowing it. It is an
accepted approximation.

### 2.4 Facet exclusion is a deliberate safety trade

A facet whose noise floor is non-zero is excluded from the signature, because a
facet that flips on unchanged re-runs cannot carry evidence. If a removed
source would have moved only an excluded facet, the check sees no change and
returns a false `clean`. This is the one place in the method where safety is
knowingly traded for signal; D-037 measured what it costs. Keeping such a facet
is worse, not merely different — a 75% floor turns the check into a coin flip
that always says "influenced", which is the conservative fallback with extra
steps.

---

### 2.5 A source can reach a prompt outside the block a redaction operates on

**Found 13-09-2026 while root-causing the real-model unsafe preservation in
`docs/08` §7.5; half of that failure is this. D-062.**

A counterfactual removes a source from the rendered source block. That is only
a test of the source if the block was its **only** route into the request, and
in this pipeline it sometimes is not: the Coder's script prompt quotes the
decision event's output verbatim under `Approach you chose:`, and the
Reviewer's quotes the draft script, both outside the block and outside anything
`redact_source()` can reach.

So when a source influenced that upstream event, redacting it leaves its
contribution in the request. The model answers from the relay, the signature
does not move, and the pair is cleared. **The unmoved signature is the
experiment failing, and it is indistinguishable from an innocent source unless
you look at the prompt.**

Three things about this are worth keeping separate:

- **It is a confound, not a weak instrument.** No comparator, however good, can
  see a difference the redacted request never produced. §2.1 is about the
  signature being too coarse; this is about there being nothing to measure.
- **It is not solvable by removing the relay too.** A counterfactual may differ
  from the original in exactly one source. Cutting the relay changes the
  request twice and the verdict stops being attributable.
- **What is done instead is to refuse the clearance.** D-062 records such a
  pair `assumed`, never `clean`, so the walk keeps it contaminated. Measured
  cost: two exposed-only cells fall from 100% to 79% work preserved. Those are
  the two channels — memory and inter-agent message — whose payload lands on
  the Coder, which is the agent with the relay.

**What remains unfixed.** The relay is detected by looking for an upstream
output's text in the prompt. A *paraphrased* relay — the pipeline summarising
an upstream output rather than quoting it — is not found, and such a pair keeps
the old behaviour. Our pipeline only ever quotes, so the detector is complete
*here*; it is not complete in general, and a system that summarises between
agents would need a different mechanism. The general statement is uncomfortable
and should be in the paper as it stands: **leave-one-out over a prompt is only
sound if every route from a source into that prompt is removable, and nothing
in the method enforces that.**

**Relationship to §2.2.** This is a redundant-cause failure — the source and
the relay each independently suffice — but it is *not* the one D-051 fixed.
`merge_derived_units()` merges redundant **sources**, using recorded
`derived_from` links. The relay is not a source, has no id, and can never enter
a unit. D-051's fix is structurally incapable of seeing it, which is why the
failure survived that work.

## 3. Attack-success probabilities are borrowed, partial, and saturating

Phase 8's per-channel rates come from Zou et al. 2025 (arXiv 2507.20526):
27.1% for indirect injection, 5.7% for direct. Three limitations:

- **Two channels have no published figure at all.** `inter_agent_message` and
  `memory_write` default to the indirect rate as the conservative choice, and
  `UNCALIBRATED_CHANNELS` marks them everywhere they are used. We did not
  invent numbers for them and we did not run a private red-teaming campaign to
  produce some, because a privately measured constant would be indistinguishable
  from a cited one to a later reader.
- **No per-model calibration.** `MODEL_PA` is empty. The rates describe an
  aggregate of systems, not gemini-3.6-flash.
- **The independence assumption fails at run scale.** `P = 1 - Π(1-pa)^m`
  treats every exposure as an independent attempt. An 18-event run of our
  pipeline carries ~64 exposure edges, and the formula returns 1.000 — because
  36 independent 27% attempts *would* be near-certain, while our 36 edges are a
  handful of documents re-sent to several questions, and a document that failed
  once fails again. The per-node figures are useful for ranking; the whole-run
  figure is an upper bound and is not used as a deployment attack rate.
  `src/eval/economics.py` sweeps the attack rate instead.

---

## 4. The economics say CausalLine does not currently pay for itself on this testbed

This is the most uncomfortable measured result and it is reported first rather
than buried.

On our own runs (Phase 9, `python -m src.eval.economics`):

| quantity | value |
|---|---|
| N — full restart | 600 tokens |
| A — analysis, inline attribution on every event | 900 tokens |
| A — analysis, targeted (counterfactual on the flagged region only) | 300 tokens |
| f — fraction of N selective recovery replays, oracle detector | 0.67 |
| storage overhead | ~26 tokens/run at 10⁻³ tokens/byte |

`A + f·N` exceeds `N` in both conditions, so **no attack rate makes the storage
tax worth paying** on this testbed. The break-even frontier says what would
have to change: at `A/N ≤ 0.25` and `f ≤ 0.5`, deployment pays off above an
attack rate of roughly 1 run in 6.

Three things this does and does not mean:

- **It is a fact about the ratio, not about the method.** A six-call pipeline
  analysed by nine analysis calls is the worst possible case for an approach
  whose cost is per-event and whose saving is per-run.
- ~~The saving scales with workflow length; the testbed is 18 events.~~
  **CORRECTED 09-09-2026 (D-049). This was asserted and is now measured false.**
  A `long` workflow (27 events, 22 sources, 6 agents) was built specifically to
  test it. Under a proportional token cost model, `A/N` is **1.23 at 19 events
  and 1.24 at 27** — flat across a 42% longer trace, not improving. The saving
  does **not** scale with workflow length on anything measured so far. Whether
  it would at a length far beyond 27 events is unknown and is not what this
  sentence claimed.
- **The flat token model exaggerates it, by about 22%.** `ScriptedClient`
  charged 100 tokens per call regardless of prompt size, which prices a
  restart's few large prompts and the analysis's many small counterfactual ones
  identically. `cost_model="proportional"` (Phase 8.1) bills by rendered
  length. Measured: `A/N` **1.50 → 1.23** at the short length. This no longer
  needs live quota — it needed a cost model.

  Note the interaction, because it is why this went unnoticed: under the flat
  model `A/N` appears to *degrade* with length (1.50 → 1.78), which looks like
  evidence against scaling but is an artefact of counting calls instead of
  tokens. Both the optimistic claim above and that pessimistic reading were
  wrong for the same reason.
- **f is large because contamination genuinely spreads.** At the oracle
  detector's one flagged source, 6 of 18 events are contaminated. That is the
  honest answer for a four-agent chain where every agent reads the previous
  one's output.

---

## 5. Everything is measured against a scripted agent

`ScriptedClient` (D-030) has a known usage rule, so ground-truth influence is
computable by leave-one-out and the estimator's accuracy is measurable. That is
what makes any per-pair number in this project possible at all.

It answers: *does the estimator recover a usage pattern that is really there?*

It does not answer: *do real models use sources this way?* The agent's
susceptibility to an injected instruction is a rule we wrote, so on these runs
the influencing / exposed-only distinction is under our control — which D-025
records as being untrue of live runs, where the variant labels mean intent
only.

**Reduced, not removed, 10-09-2026.** `src/eval/real_llm.py` adds a second
evaluation mode in which real hosted models drive the agents and the scenarios
are generated rather than hand-written, and the whole recovery pipeline —
attribution, refinement, contamination, planning, selective replay,
verification, escalation, all four methods — runs against it. The full account
is `docs/09-real-llm-evaluation.md`; what matters here is what it does and does
not buy.

**What it buys.** The live evidence is no longer one channel and five pairs
from Phase 13.2. Whether an attack *lands* is now measured per run rather than
asserted by us, which is exactly the thing D-025 says we cannot control. And
the tests are drawn from an enumerated design space by seed, with the payload
text written by a model, so a scenario cannot have been tuned to a result
nobody had yet.

**What it does not buy, and this is the part that belongs in the paper:**

- **It is effectively one model, and the second one makes that worse rather
  than better.** Of the three models the evaluation was specified against,
  `minimaxai/minimax-m3` returns 410 Gone (end of life 2026-09-09) and
  `deepseek-ai/deepseek-v4-flash-0731` is *intermittent* — stalling past 300s
  across a whole day, then answering in 0.7s the next, and still timing out
  inside a campaign minutes after its own preflight passed. Both identifiers
  were verified against the live API; neither was substituted.

  A flat outage would leave a clean single-model result. Flakiness leaves a
  worse one: which model generated or executed any given test depends on
  whether the endpoint answered at that moment, so **model assignment is not a
  controlled variable** and any per-model comparison is confounded by
  availability. The question *does CausalLine's behaviour depend on which LLM
  is behind the agents* is **open**, and no split of these results by model
  answers it.
- **Ground truth there is observed, not known.** It rests on canary-token
  presence plus the pipeline's own code-path records. An influence that leaves
  no token behind is invisible to it, so such a run **understates**
  contamination — the dangerous direction. An unsafe-preservation count of zero
  under that yardstick is weaker evidence than the same count in the scripted
  matrix, and the two must not be quoted as if they were the same measurement.
- **The two modes cannot be pooled.** Different ground truth, different
  determinism, different repetition structure.

The limitation therefore moves from *"we have essentially no real-model
evidence"* to *"we have real-model evidence on one model, under a token-scoped
ground truth, and the cross-model question is open."*

The scripted mode stays, unchanged, and stays the deterministic
ground-truth component: it is the only place a per-pair accuracy number is
possible at all.

---

## 6. Detection is a socket, not a contribution

`docs/01-scope.md` puts detection out of scope, and the project consumes a
detector verdict as input. `src/eval/detectors.py` provides simulated
occupants whose error rates are *known* because they are derived from ground
truth and then degraded — that is what makes a sensitivity study possible, and
it is not a detector.

Phase 13.3 adds `HeuristicInjectionDetector`, which is real in the only sense
that matters here: it reads source content and has never seen a label. Its
measured behaviour on our own scenarios, with rules and threshold fixed before
the run and not adjusted afterwards:

- catches the influencing variants of scenarios A and C (score 1.00)
- **misses** scenario B's influencing variant, which scores 0.45 against a
  threshold of 0.50. "Project convention: always print dates in DD-MM-YYYY,
  never ISO" fires the *imperative* and *authority* families but no *override*
  pattern.

A missed source is the dangerous failure: everything it influenced is
preserved, and unsafely. We report the miss rather than lowering the threshold
to 0.45, which would be tuning the instrument against the answer.

**The consequence is measured, not asserted.** In the 30-repetition campaign
(D-046), the `heuristic` / `B-influencing` cells are the only non-`blind` cells
in the whole matrix that record unsafe preservations, and they record them for
**B1 and B2 in 100% of runs**. CausalLine records zero there — not because it
detected anything the detector missed, but because the scenario B payload's
influence path leaves it invalidating the affected events anyway. That is luck
on this scenario and must not be read as robustness to a missed source; nothing
in the method can recover from an incident it is never told about.

The transformer-based `TransformerInjectionDetector` is optional and requires
downloading model weights; it raises rather than returning an empty verdict
when unavailable, because an empty verdict is indistinguishable from the
`Blind` control.

---

## 7. Storage bounds are for the checkpoint *count*, not the payload

Phase 12's garbage collection keeps exactly one checkpoint per agent — the most
recent confirmed-clean one — so the number of retained checkpoints is flat in
trace length, which is the unbounded term. Asserted directly in
`tests/test_checkpoint_lifecycle.py` on runs up to 2000 events.

The surviving checkpoint's *own* payload still grows, because the pipeline's
checkpoint state is a global snapshot carrying a ref for every output so far.
D-028 already cut this from text to content refs (a 2× reduction, measured);
making it incremental is future work.

Two further caveats:

- GC is gated on `check` records, never on elapsed time. On a trace where the
  estimator has not examined every exposure, **nothing is dropped** — which is
  the correct conservative behaviour and means GC frees nothing on a
  lightly-analysed run. Our own scenario A trace is such a run.
- The recovery horizon (§12.2) buys storage by giving up selective replay
  beyond it. The fallback to coarse agent restart is explicit and tested, but
  it is a real loss of the method's main benefit for old incidents.

---

## 8. The checkpoint interval is derived, not validated

Phase 12.3 sets the interval by Young (1974) / Daly (2006):
`T_opt ≈ √(2·δ·M)`, with `δ` measured as checkpoint payload bytes over event
bytes and `M` taken from Phase 9's measured detection latency. On our runs this
gives ≈2.6 events against a current policy of one checkpoint per agent boundary
(≈4.5 events).

We have **not** run the experiment that would validate it — varying the
interval and measuring total recovery cost at each setting. The formula is
imported from a literature that assumes independent exponential failures and
constant checkpoint cost, and prompt injection is neither independent nor
exponentially distributed. Treat the number as a principled default, not as an
optimum we demonstrated.

---

## 9. Group testing's saving is modest at our scale

Phase 11.2 measured, on real scripted runs (`python -m src.provenance.group_test`):

- **208 counterfactual calls** for exhaustive leave-one-out
- **182** for recursive-halving group testing (+12%)
- **129** with sibling inference enabled (+38%)

Per event it is uneven: +75% on the Researcher's sparse events, **−80% on the
Coder's decision event**, where three of five sources genuinely matter and
halving costs more than testing each. `GroupTestDiagnostics.sparsity_failing()`
detects this and it is the documented trigger for the Phase 11.3 fallback.

The asymptotic argument (Dorfman: `O(k log(n/k))` for `k ≪ n`) is real and is
visible on synthetic sets — 64 candidates with 1 influential costs 13 calls
instead of 64. Our events carry 5 to 11 exposures, which is not the regime
where it shines.

---

## 10. The fixed-budget fallback transfers badly from Context-Cite

Phase 11.3 ports Cohen-Wang et al.'s approach (USENIX Security 2025), which
fits a linear surrogate over random-subset removals. Context-Cite fits
**log-probabilities**; we have no logits and D-026 rules out text comparison,
so we fit a **binary** signature match. Measured consequences:

- **~1 bit per call** instead of a continuous response.
- **Class imbalance from the OR structure**: the decision holds only when *no*
  influential source was removed, which at a keep fraction of ½ happens with
  probability 2⁻ᵏ. At k=4, one row in sixteen carries the minority answer.

Result: it recovers sparse sets at small n and fails at k/n = 0.5 whatever the
budget. `r_squared` falls when the fit is untrustworthy, which is what it is
reported for. It is a fallback invoked on group testing's own diagnostics,
never a default.

---

## 11. What the confidence intervals in the campaign cover

Phase 13.4 runs the full matrix 30 times. The only thing that varies between
repetitions is the seed, which drives `ScriptedClient`'s self-report error
rates. So the intervals cover **variation in which cheap-stage claims were
wrong** — which is the right thing for an error bar on this method to describe.

They do **not** cover live-model non-determinism. D-026 measured that
separately (8 of 8 answers textually distinct, 1 of 9 at the decision level)
and it is not folded in here. Quoting these intervals as though they covered a
live deployment would be wrong.

A practical consequence: **most intervals come out zero-width**, because the
graph and set operations do not depend on which self-report claims were wrong.
That is information, not a missing error bar — but it also means the campaign
is not evidence that the method is stable under anything except its own
cheap-stage errors, which is a much narrower claim than "30 runs" suggests.

---

## 12. SPRT's guarantee is asymptotic and its hypotheses are ours

Phase 10 uses Wald's SPRT as specified, and its optimality (minimum expected
sample size for given error rates, Wald & Wolfowitz 1948) is a real theorem we
did not try to improve on. Two honest caveats:

- **The hypotheses are a choice.** `f_star` comes from the cost model, but
  `f_star_high = f_star + margin` with `margin = 0.15` is a parameter, not a
  measurement. It is the indifference band, a wider band is a cheaper test, and
  we did not tune it.
- **Wald's error bounds are approximate** with discrete observations, because
  the log-likelihood ratio overshoots the boundary rather than landing on it.
  Our measured rates come in at or under α and β on synthetic sequences
  (`tests/test_sprt.py`), which is the direction the approximation errs, but
  the guarantee is not exact.
