# IREC: the exact-equality safety argument, and what is actually new

Date: 2026-11-05. Companion to [`IREC_LIVE_VALIDATION.md`](IREC_LIVE_VALIDATION.md),
[`IREC_COST_ACCOUNTING.md`](IREC_COST_ACCOUNTING.md) and
[`IREC_FINAL_DECISION.md`](IREC_FINAL_DECISION.md). Updates
`FINAL_COST_OPTIMIZATION_RESEARCH.md` §7–8 with what the live campaign measured.

---

## 1. The claim, stated precisely

IREC keeps an original output `y_e` in exactly two situations:

1. **Prompt-identical splice.** The prompt the rerun composes for `e`, with the
   flagged sources redacted, is **byte-identical** to the prompt the original
   run issued. No model call is made.
2. **Output-identical cutoff.** The prompt differs, `e` is re-executed on the
   clean prompt `p'_e`, and the reply is **byte-identical** to `y_e`.

**Safety claim.** Every kept output is an output the model produced on the
clean-world prompt.

In case 2 this is immediate and needs no assumption at all: `M(p'_e)` was
actually computed, and it equalled `y_e`. This is the strongest form of
evidence available for a stochastic black box — not an inference about
influence but a direct observation of the counterfactual.

In case 1 it needs exactly one assumption, A1 below: that the prompt determines
the output distribution, so an output produced on a prompt is a valid output on
the identical prompt.

**No comparator, no similarity threshold, no calibrated noise floor, no
self-report, no removability analysis appears anywhere in that argument.** That
is the point of the design, and §4 records what happens when the exactness is
relaxed.

---

## 2. What must be compared: the projection boundary

Comparing the agent's reply text is sufficient only if the reply is the *whole*
of what downstream execution can see. The brief asks this to be established
rather than assumed, so here is the census of every event kind in both
workflows, and which of them a model produces.

### mixed56, 145 events (the real-model testbed)

| event kind | produced by | count |
|---|---|---:|
| `agent_output` | **a model call** | **55** |
| `message` | pipeline code | 55 |
| `tool_call` | pipeline code | 12 |
| `tool_response` | pipeline code | 12 |
| `memory_read` | pipeline code | 10 |
| `agent_output` (the final comparison) | pipeline code | 1 |

Tools invoked: `fetch` × 24, and nothing else. **No `memory_write` occurs at
all**; memory is read-only in this workflow.

### the scripted chain, long workflow, 27 events

| event kind | produced by | count |
|---|---|---:|
| `agent_output` | **a model call** | **7** |
| `plan` | **a model call** | **1** |
| `decision` | **a model call** | **1** |
| `agent_output` (executor comparison) | pipeline code | 1 |
| `message` | pipeline code | 5 |
| `tool_call` | pipeline code | 5 |
| `tool_response` | pipeline code | 5 |
| `memory_read` | pipeline code | 1 |
| `memory_write` | pipeline code | 1 |

Here a `memory_write` *does* occur, and it matters: event `e0023` writes key
`last_run/approach` whose value is the text of the model's `decision` at
`e0019`. So a model output reaches persistent state — but it reaches it through
pipeline code that derives the write from the output deterministically.

### The resulting requirement

**Complete mediation (A5).** Every downstream-visible effect of a model
invocation is a deterministic function of that invocation's recorded output
together with pipeline code — messages, tool-call arguments, tool responses,
memory writes and shared state included.

Under A5, byte-equality of the reply implies byte-equality of every mediated
surface, and comparing the reply is sufficient. **Where A5 fails, the
comparison must move to the surface itself**: the tool-call arguments, the
memory-write value, the typed fields a consumer parses. The permitted
generalisation of "exact equality" is *exact equality of a wider tuple*
— reply, tool arguments, memory writes, structured fields — and never a
similarity judgment on any one of them.

### A5 is measured here, not assumed

The fidelity gate is the test. Run the replay with **no flagged sources**, so
nothing is redacted and nothing should change. A prompt in this pipeline is
assembled from upstream outputs *and* from tool and memory state, so if any
mediating surface had drifted, downstream prompts would have differed.

| placement | model events | prompts byte-identical | model calls |
|---|---:|---:|---:|
| 20260917 small | 55 | **55 / 55** | 0 |
| 20260917 medium | 55 | **55 / 55** | 0 |
| 20260917 large | 55 | **55 / 55** | 0 |

165 of 165 prompts reproduced exactly, with zero model calls. That is direct
evidence for A1 and A5 on this pipeline, and it is the precondition for reading
any later difference as the intervention or the model rather than the harness.

---

## 3. Assumptions, each with its evidence

| | assumption | needed for | status |
|---|---|---|---|
| **A1** | input completeness: a model call's output depends only on its recorded prompt and system prompt, with no hidden state | **safety** (case 1 only) | **measured**: 165/165 prompts reproduced in the fidelity gate |
| **A2** | complete ingestion redaction: every flagged source is removed wherever rendered. `redact_flagged(strict=True)` raises if it cannot | **safety** | enforced by construction; behaviour retained |
| **A3** | faithful replay: an unaffected agent re-executed on clean inputs reproduces its output | **cost only, never safety** | **measured: 96.7% (87/90)**. See `IREC_LIVE_VALIDATION.md` §4 |
| **A4** | detector completeness: the flagged set contains every malicious source | **recall** | shared by every method; the detector's job |
| **A5** | complete mediation (§2) | **sufficiency of comparing the reply** | **measured** via the fidelity gate; holds in both workflows here |

The asymmetry between A3 and the rest is the single most important property of
the design. **A3 failing costs money and never costs safety**: a reply that
differs for any reason — genuine influence or sampling noise — is treated as
*changed*, the original is discarded, and the change propagates. The
conservative direction is the cheap-to-be-wrong direction.

Measured consequence: 3 of 90 re-executions did not reproduce their output
under an identical prompt. All three were the local 8-billion-parameter model on
CPU. Each one costs unnecessary downstream replay. None of them could produce
an unsafe preservation.

---

## 4. Why the equality must stay exact

Two measurements, both on exact ground truth, both negative results that are
kept deliberately.

**Relaxing IREC's cutoff to the production comparator is unsafe.** Replacing
byte-equality with the calibrated signature comparator and carryover facet that
the production counterfactual uses produced **1 and 4 unsafe preservations** on
scenario A/influencing (short and long). The comparator admits as "equal" a
reply whose material content moved.

**The production counterfactual itself returns a false clean.** On the named
hard case `(S3, e0010)`, scenario A/influencing:

| estimator | pair cleared? | influence edge? | by what evidence |
|---|---|---|---|
| `targeted_only` | **YES — unsafe clearance** | **no** | `method='counterfactual' verdict='clean'` |
| `hybrid` | no | yes | `method='self_report' verdict='tainted'` |
| `lazy` | no | yes | `method='self_report' verdict='tainted'` |

Ground truth: S3 **did** influence e0010. So the counterfactual is wrong in all
three modes, and `hybrid`/`lazy` reach the right answer only because a
self-report *positive* settled the pair first. In that cell CausalLine's
event-level safety is carried by self-report and the escalation ladder, not by
the counterfactual.

**IREC gets it right with no attribution stage at all**: 0 unsafe
preservations, e0010 correctly recomputed, task success, byte re-check clean, in
both the short and long workflow. The replay *is* the counterfactual, so there
is no comparator to be wrong.

This is the reason the cutoff is exact and placed at the consumer's projection.
Any relaxation must be a projection the consumer applies anyway, never a
similarity score.

---

## 5. Side effects during replay

Counterfactual replay re-executes work. If that work has external effects, the
replay causes them again.

| surface | in this system | replay-safe? |
|---|---|---|
| `web_pages`, `db`, `mixed_docs`, `mixed_messages`, `fanout_docs` | in-process fixtures passed into `Tools`; no network, no real database | **yes**, read-only |
| `memory` | per-run dict, optionally a per-run JSON file; replay builds fresh `Tools` | **yes**, reset per replay |
| `fetch` | fixture lookup — the only tool mixed56 calls (24 times) | **yes** |
| **`run_python`** | **a real subprocess executing model-written code** | **no** — see below |

`Tools.run_python` runs generated code via `subprocess.run` with a fresh
temporary working directory and a timeout. Its own docstring is explicit: *"NOT
a sandbox. Tool sandboxing is explicitly out of scope (docs/01-scope.md), and
this runs model-written code on the machine that invokes it."*

Consequences, stated rather than waved at:

- **The live validation touched none of this.** mixed56 calls only `fetch`, so
  every IREC run in `IREC_LIVE_VALIDATION.md` was free of non-idempotent
  effects. The fresh temp directory also makes `run_python` idempotent *with
  respect to the workflow* — no state carries between calls — so the scripted
  cells are replay-safe at the workflow level even though they execute code.
- **IREC must not be pointed at a workflow with irreversible external effects**
  without a suppression or sandbox layer. The cutoff at tool arguments limits
  re-issue to calls whose arguments actually changed, which bounds the exposure,
  but it does not eliminate it.
- **Effects that already left the system can only be reported, not undone** —
  the same limitation Retro, Warp and agent-rewind state. CausalLine already
  reports this for memory: `selective_memory_rollback` names the keys a live
  store would have to undo rather than undoing them.

---

## 6. The bound, and what it does not say

### Theorem 1 (no single-run clearance)

For a black-box `M`, no rule that sees only `(p_e, y_e)` can decide "`s` did not
influence `e`" with zero false negatives unless it never clears an exposed pair.
Two models are consistent with any single observation: one where
`M(p_e \ s) = y_e` and one where it differs. The rule's input is identical under
both.

This is noninterference being a 2-safety hyperproperty (Clarkson & Schneider):
certifying it for a given input takes a second execution. **Corollary:** logging,
provenance, attention weights or self-report cannot clear an exposed event with
a zero-false-negative guarantee. The `(S3, e0010)` result in §4 is this corollary
appearing as a measured defect.

### Theorem 2 (soundness, and query-minimality within a stated class)

Let `D = {e : p'_e ≠ p_e}` — the events whose prompt actually changed.

*Soundness.* Under A1 and A2, every output IREC keeps was produced by `M` on its
clean-world prompt. Zero unsafe preservations by construction.

*Query-minimality.* Any **sound, black-box** method that distinguishes influence
from exposure **under exact output semantics** must query `M` on `p'_e` for each
`e ∈ D`, by Theorem 1. IREC queries exactly `D`, once each. Hence
`C_IREC ≤ C_B2` (because `D ⊆ closure`) and `C_IREC ≤ C_B0`.

*Premises checked, not assumed.* On the live runs:

| run | \|D\| | model events in B2 closure | `D ⊆ closure` | every canary-bearing model event in `D` |
|---|---:|---:|---|---|
| 20260917 small | 4 | 5 | **yes** | **yes** |
| 20260918 medium | 20 | 37 | **yes** | **yes** |
| 20260920 large | 45 | 52 | **yes** | **yes** |

And `R = C_IREC / min(C_B0, C_B2) ≤ 1.000` in **24 / 24** scripted cells.

### What the bound explicitly does not claim

This matters more than the bound itself, and the brief asks for it directly.

1. **It is not a claim about every possible recovery algorithm.** It is a
   minimum over *sound black-box* methods under *exact output semantics*. Three
   escapes are real:
   - a **sound projection** — if a consumer parses one field, a reply that
     changes elsewhere need not propagate, so a method comparing the field can
     query fewer than `|D|`. This is a legitimate way to beat IREC, and §2 says
     it is the only legitimate relaxation.
   - **white-box access** does not reduce the *count* — the contaminated outputs
     must be recomputed and certifying an unchanged consumer still takes a
     forward pass — but it makes each query cheaper (prefix and KV reuse,
     speculative verification). The bound is on queries, not on FLOPs or money.
   - **problem structure** outside the black-box model, e.g. a provably
     side-effect-free agent.
2. **It is instance-wise, not worst-case over inputs.** `D` is defined on the
   realised replay outputs — the same notion of optimality build systems use for
   early cutoff. It says IREC is minimal *on the run that happened*, not that
   its expected cost is minimal over a distribution of attacks.
3. **It bounds queries, not tokens.** Equal query counts can differ in cost if
   prompts differ in length.
4. **It says nothing about work preserved.** IREC can be query-minimal and still
   discard more events than a method with better attribution — and on the
   scripted matrix it does: 61.9% against CausalLine hybrid's 68.3%.

### The saturation limit, where 0.8× is unreachable

The 0.8× target is attainable only where the contaminated computation is a
small enough part of the closure. On mixed56 **large** it is not: contamination
genuinely reaches most of the closure's model work. An oracle that re-runs only
the canary-bearing model events and ignores the consumers any method must
re-execute already costs 0.66–0.92 of B2 per run (0.79 pooled). Under faithful
replay IREC's large-regime ratio *is* that floor. **No sound black-box method
reaches 0.8× on that workload** — an impossibility result for the workload, not
a deficiency of the method. It is reported per regime and never hidden in a
pooled average.

---

## 7. Novelty, after validation

### Known prior art — a reviewer will treat IREC's mechanism as old

- Selective re-execution with an input-equality skip and an output-equality
  stop: **Retro** (OSDI 2010) predicates and shepherded re-execution, **Warp**,
  **Aire**.
- Early cutoff and backdating: **Shake**, **Bazel**, **Salsa**.
- Change propagation: self-adjusting computation, **Adapton**, differential
  dataflow, **DBSP**.
- Noninterference by re-running with high inputs removed: **secure
  multi-execution**.
- For multi-agent LLM workflows specifically: **agent-rewind** (UCSD technical
  report, 2026) re-runs and reuses steps whose Merkle-hashed reads verify, with
  early cutoff, greedy decoding and speculative decoding against the previous
  output, including a TAMAS indirect-injection scenario.

**Not defensible:** IREC as a new recovery mechanism; any claim that this
project invented early cutoff for agents; any claim of novelty for
build-system-style unchanged-step skipping.

### Defensible, and now supported by measurement

1. **The reduction: attribution is subsumed by the recovery replay.** The
   counterfactual of the joint intervention on `F` and the recomputation are the
   *same model call*, so a separate attribution stage is pure overhead. On the
   real-model workload that overhead is 16,006 tokens per run — **67% of
   CausalLine's total cost**, removed entirely. agent-rewind marks a faulty
   *producer* and treats every reader as in the cone; it makes no
   exposure-versus-influence claim. The new evidence is that
   exposure-is-not-influence can be *established by the replay itself at no
   extra cost*, and the `(S3, e0010)` case shows the separate attribution stage
   was not merely redundant but wrong.
2. **Theorems 1 and 2 for stochastic black-box components**, with the scope
   limits of §6 attached, plus dominance over B0 and B2 verified as set
   containment on real runs. None of the cited systems states this for
   stochastic components.
3. **Negative evidence that comparator-based clearance is unsound**, on exact
   ground truth: the production counterfactual's false clean on `(S3, e0010)`,
   and IREC-with-comparator's 1 and 4 unsafe preservations. This is *why* the
   cutoff must be exact.
4. **An economic characterisation with an impossibility result** (§6), measured
   on a 56-agent three-provider workload with exact recall and unsafe
   accounting, reported per regime.
5. **New in this document: the two mechanisms are separable, and only one needs
   A3.** The prompt-equality splice needs only A1 and is free; the
   output-equality cutoff needs A3 and costs one call per event. The scripted
   matrix cannot exercise the second at all — `unchanged` is 0 in all 24 cells,
   because a deterministic client given a changed prompt always produces a
   changed output. The cutoff's value is visible **only** against real models
   (12 cutoffs on one medium placement). Any future paper claiming the cutoff
   mechanism must cite real-model evidence; scripted evidence cannot support it.

### What would still strengthen the contribution

- A typed-interface variant of the scripted chain (findings passed as parsed
  fields), to quantify mechanism 5's second half on exact ground truth. Named in
  the previous report's §9.3 item 3 and **still not done**.
- A direct comparison against agent-rewind's producer-level policy on the same
  placements.
- Replication of the A3 rate on a hosted model with documented deterministic
  decoding, to separate "this 8B model on CPU" from "hosted models in general".
  The current A3 estimate rests on 82 local calls, 6 NVIDIA calls and 2 Gemini
  calls.
