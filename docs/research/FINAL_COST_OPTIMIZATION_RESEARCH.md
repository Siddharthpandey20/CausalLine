# Making CausalLine cheaper than restart and topology recovery, without weakening recovery

*Research report, 05-11-2026. No production code changed. Every new number here comes
from `docs/research/prototype/` (an isolated harness that imports `src/` but is not
imported by it) or from result files already in `data/results/`. Sources are in
`RELATED_WORK_AND_SOURCES.md`.*

---

## 1. Executive verdict

**CONDITIONAL GO.**

**The recommendation.** Remove attribution as a separate stage. Recovery should run as
**Interventional Replay with Early Cutoff (IREC)**:

1. Remove the flagged sources where they enter.
2. Re-execute events in dependency order.
3. Splice back the original output of any event whose *recovered prompt is
   byte-identical* to its original prompt.

That replay *is* the counterfactual. An agent that was exposed to the malicious source
but ignored it gets re-executed once. If its projected output comes back unchanged,
nothing downstream of it runs. An influenced agent's output changes, and only then does
the change propagate.

**Why this beats both baselines.** Today a contaminated event is paid for twice: once
as a counterfactual and once as a replay. Self-report is paid on top of that. Under
IREC each event is paid for once, and only if its input actually changed.

**What it guarantees.**

- **Safety does not depend on any comparator, self-report or noise floor.** A kept
  output is one whose recorded inputs are byte-identical to the clean world's inputs.
- **Cost can never exceed B2 or B0.** The re-executed set is a subset of B2's closure,
  and each event is invoked at most once.
- **It is query-optimal** among sound black-box methods (§7, Theorem 2).

**What was measured**, against existing ground truth, with no new LLM calls:

| evaluation | IREC cost / min(B0, B2) | unsafe | recall | CausalLine (current) / min(B0, B2) |
|---|---|---|---|---|
| scripted chain matrix, 24 cells (exact ground truth, both pricing models) | **1.00** in A and B (16 cells), **0.30–0.40** in C (8 cells); never above 1 | **0 / 24** | **100%** | 0.93–4.33 (hybrid), 0.37–2.51 (lazy) |
| mixed56 workload-varied (primary), small | **0.76** (0.73–0.80) | 0 / 5 | 100% | 12.3 |
| mixed56 workload-varied, medium | **0.40** (0.58–0.59 landed; 0.11–0.14 not landed) | 0 / 5 | 100% | 2.98 |
| mixed56 workload-varied, large | **0.95** (0.88–1.00) | 0 / 5 | 100% | 2.63 |
| all 15 primary runs, pooled tokens | **0.71**; **0.86** with CausalLine's real escalations charged to IREC | 0 / 15 | 100% | 3.57 |

**How the target lands:**

- **"Strictly below both baselines"** is met in every regime when there is no
  escalation.
- **The 0.8× target** is met in medium, met in small only when there is no escalation,
  and **not met in large**.

**Large: the 0.8× target is out of reach for any sound black-box method on this
workload.** IREC is query-minimal, so its 0.88–1.00 is the floor. Even an oracle that
re-runs only the canary-bearing model calls and ignores their consumers sits at 0.79
pooled (0.66–0.92 per run). That is an impossibility result for this workload, not a
gap in the method (§7.4).

**The two conditions that decide feasibility, both unmeasured:**

1. **Faithful replay at the projection boundary.** An unaffected agent, re-executed on
   clean inputs, must reproduce the field its consumers read (the `site code` message in
   mixed56). The mixed56 savings above assume this.
   - If it fails, IREC degrades to *exactly* B2's cost. It does not become unsafe.
   - Indirect support: CausalLine's selective replays of the same agents passed the
     task check in 12 of 15 primary runs.
   - The direct measurement is §9.3; it costs about one B2 replay per run.
2. **Verification-failure economics in small-closure regimes.** In 3 of 5 small runs the
   real system's selective replay failed the task check (replay noise of the local
   model). Charging IREC the same ladder (agent restart, then full restart) makes it
   **2.64× the unverified B2**. B2 is never verified; verifying it under the same rule
   puts IREC at **1.13×**.
   - Small is where CausalLine already loses (docs/14). IREC does not fix that.
   - It narrows the loss from 12.3× to 2.64×, or to 1.13× under symmetric
     verification.

**Novelty: moderate, not large.** Selective re-execution with an equality cutoff is
old: Retro (OSDI 2010), build-system early cutoff, Salsa backdating. It was applied to
multi-agent LLM workflows in 2026 (agent-rewind, UCSD technical report). §8 states
precisely what remains defensible: the reduction (attribution is subsumed by the
recovery replay), the lower bound and optimality argument, the measured unsoundness of
comparator clearance, and the exact-ground-truth evaluation against B2.

**Recommendation:** implement IREC behind a flag, and run the §9.3 measurement before
changing any default or any paper claim.

---

## 2. The existing system, as measured (not as documented)

### 2.1 Pipeline

```
detector -> flagged sources F
  -> provenance: self-report (inline on every model event, "hybrid") or deferred ("lazy")
               + targeted counterfactual: redact one source, re-run one event,
                 compare signatures (comparator + calibrated noise floor + carryover
                 facet + removability check)
  -> influence edges -> contamination walk -> recovery region
  -> recovery: SplicingClient re-runs the pipeline, re-invokes invalidated model events,
               splices the rest
  -> verify: taint walk on recovered trace + byte re-check + memory references + task check
  -> escalate: selective -> agent_restart -> restart_all
```

### 2.2 Where the cost goes

Pricing reconstructed from `data/results/mixed56-workload-varied/*.json`; per-run means
over 15 runs:

| quantity | small | medium | large |
|---|---|---|---|
| N, the original pipeline (tokens) | ~10,860 | ~10,950 | ~10,850 |
| CausalLine analysis A | 16,312 | 15,796 | 15,911 |
| CausalLine replay | 3,286 | 7,975 | 9,772 |
| **B2 replay** (topology closure) | 1,595 | 7,975 | 9,772 |
| B0 replay (full restart) | ~10,700 | ~10,710 | ~10,340 |

Three things in these rows were not visible in the existing reports.

- **In medium and large, CausalLine's replay tokens equal B2's exactly, in all 10 runs.**
  The extra 6–25 events CausalLine "preserves" are tool, memory-read and message events
  that cost zero tokens to redo. Its +7.0 and +15.2 point *work-preserved* gains have
  **zero token value**. So no attribution scheme that has positive cost and preserves the
  same events can beat B2 on that workload.
- **A is about 1.5 N and nearly constant across regimes.** Analysis does not scale with
  the contaminated region on this workload; it is a fixed tax of about 16k tokens.
- **Self-report is 80% of analysis cost** (`gate1/final_cost_direction.md` §2, 290
  traces), and in hybrid mode it is paid on *every* run, attacked or not.

### 2.3 Demonstrated guarantees, and what actually carries them

- 0 unsafe preservations and 0 escapes in every reported campaign. Recall is 1.0 on
  mixed56 (docs/14 §4).
- **New finding: a production counterfactual returns a false clean.** In `targeted_only`
  mode, the production counterfactual records `(S3, e0010)` on scenario A/influencing as
  `clean (counterfactual, group-tested)`. Scripted ground truth says it is influenced.
  Hybrid and lazy get it right only because a self-report *positive* settled the pair
  first.
  - Reproduce: `docs/research/prototype/cutoff_replay.py` together with
    `_original_run('A', True, …, 'targeted_only')`.
  - **So in this cell, CausalLine's event-level safety is carried by self-report
    positives and by the task-check escalation, not by the counterfactual.**
  - This is the mechanism behind docs/15's "targeted_only commits 30 pair false
    negatives".

### 2.4 Two accounting defects found while reproducing

They affect the comparison, not safety.

1. **Long-workflow baselines replay on the wrong fixtures.**
   `src/eval/experiment.py:run_cell` calls `run_baseline_recovery(...)` without
   `tools`, so B0/B1/B2 replay on `_fresh_tools(attack, out)`, the *short* fixtures.
   CausalLine replays on the extended ones. That is why B2 "fails the task" in long cells.
   Passing the same tools gives B2 task success 24/24 and changes its token cost (long
   A: 4,217 → 4,490).
2. **Escalated runs under-count CausalLine's cost.**
   `src/recovery/causalline.py:406` reports `replay_tokens = last_report.replay_tokens`.
   The failed selective and agent-restart replays before an escalation are never charged,
   so CausalLine's cost in escalated runs is **understated**. The "CL" columns in this
   report use the repository's figures and are therefore favourable to CausalLine.

---

## 3. Research landscape: what each field contributes

Full inventory with links: `RELATED_WORK_AND_SOURCES.md`. This is the technical
synthesis.

| field | mechanism | what transfers | what does not |
|---|---|---|---|
| **Intrusion recovery** (Taser 2005, Retro 2010, Warp 2011, Aire 2013, Undo for Operators 2003) | Record an action history graph, roll back the attack action, **selectively re-execute** dependents. Retro's *predicates* skip an action whose inputs are unchanged; *shepherded re-execution* stops once state matches the original run | This is CausalLine's exact problem in an OS / web setting, and the core of IREC: repair at the attack's entry, re-execute, cut off on equality | Retro assumes mostly deterministic syscall-level re-execution. LLM calls are stochastic and prompt-sensitive, so the equality test must sit at a *projection* of the output (§5.4) |
| **Build systems / incremental computation** (Shake, Bazel, Salsa red-green and backdating, Adapton, self-adjusting computation) | Verifying traces: hash inputs, skip a task when they match. **Early cutoff**: when a rebuilt output equals the old one, dependents are not rebuilt | Gives the formal "verifying trace + early cutoff" rule and its minimality intuition | These systems assume deterministic tasks. Salsa flags backdating of provisional values as a safety stop-gap, the same hazard as comparator-based cutoff |
| **Rollback recovery** (Elnozahy et al. 2002; Strom & Yemini 1985) | Piecewise-determinism (PWD); orphan detection; causal message logging | The PWD assumption is exactly IREC's *input completeness* (A1). "Orphans" are consumers of a changed message | Classical protocols re-execute to *reproduce* the same history. Here we re-execute to obtain a *different*, clean one |
| **Durable execution** (Temporal, DBOS, Flink, Spark lineage) | Event history with recorded activity results; deterministic replay; lineage recomputation | Recorded tool responses mean replay re-issues a side-effecting call only when its arguments change, i.e. cutoff at the tool boundary | Temporal *requires* deterministic workflow code; LLM agents are not, which is why equality is tested rather than assumed |
| **Databases** (IVM, DBSP, differential dataflow, provenance semirings) | Propagate deltas; a zero delta stops propagation | Equivalent to early cutoff on structured outputs; motivates typed inter-agent messages | Data lineage is *exposure*: it says which inputs were read, not whether they mattered |
| **Information flow / PL** (noninterference, hyperproperties, secure multi-execution, dynamic taint, slicing, delta debugging) | Noninterference is a **2-safety hyperproperty**: refuting it needs two executions. **Secure multi-execution** runs a "low" copy with high inputs removed, and that copy is noninterferent *by construction* | The lower bound (no single-run clearance) and the safety-by-construction argument: the recovered run *is* the low execution | Taint and slicing propagate on data dependence, which is exposure-based. Delta debugging minimises failing inputs with many runs |
| **Causal inference** (Pearl's hierarchy, causal hierarchy theorem; Halpern–Pearl actual causation) | Interventional quantities are generically not identifiable from observational data | Formal reason why one execution cannot establish non-influence (§7.1) | Not a cost reduction |
| **LLM context attribution** (ContextCite, AttriBoT, TracLLM, AttnTrace, attention-based screeners) | Ablation and surrogate models, cached-activation LOO approximation, attention scores | AttriBoT's KV reuse is a way to make each intervention cheaper on self-hosted models | No false-negative bound. They need logits or white-box access, answer "which span" not "safe to keep", and still cost queries *in addition to* the replay. Attention is not explanation |
| **Agent security** (CaMeL, FIDES, RTBAS, AgentArmor, MELON) | Capability / IFC labels; PDGs of agent traces; masked re-execution as a *detector* | MELON shows re-execution as a security primitive; CaMeL/FIDES are complementary prevention | Label propagation is exposure-based. They are prevention or detection, not post-detection recovery |
| **Agent recovery, 2026** (agent-rewind; Selective Revocation and Replay; REVISE; ESC-CR) | Merkle-verified reads + rerun + early cutoff (agent-rewind); provenance-based revoke-and-replay; dependency-based recomputation | **agent-rewind is the closest prior art**: the same execution mechanism, applied to multi-agent LLM workflows, including a TAMAS indirect-injection scenario | Marks the faulty *producer* rather than intervening on *sources*. No exposure-vs-influence evaluation, no exact-ground-truth unsafe/recall measurement, no comparison with attribution-based recovery, no lower bound |
| **Inference determinism and serving** (batch-invariant kernels; Ollama seed; prefix and KV caching; CacheBlend; speculative decoding against the previous output) | Make temperature-0 output reproducible; reuse KV across prompts; verify a draft in one forward pass | Raises cutoff yield (A3) and cuts the cost of each re-execution on self-hosted models | Billing levers (prompt caching) apply equally to B0 and B2, so they do not change the ratios |
| **Prompt sensitivity** (Sclar et al. 2024; Shi et al. 2023) | Outputs change under irrelevant context or formatting edits | Explains why byte-equality of *raw* outputs rarely holds after removing a source, and therefore why the cutoff must sit at the consumer's prompt | — |

---

## 4. Approaches investigated and rejected

| # | approach | why rejected | evidence |
|---|---|---|---|
| R1 | **Keep attribution, make it cheaper** (lazy self-report, prompt caching on self-report) | Still pays counterfactual *and* replay on contaminated events, plus self-report. Cannot reach B2 when CausalLine's replay already equals B2's | Lazy exceeds B2 in 17 of 24 matrix cells (§9.1); hybrid exceeds min(B0, B2) in 23 of 24; mixed56 CL replay = B2 replay in 10/10 medium/large runs |
| R2 | **IREC with a semantic comparator** (cut off when the production signature comparator says "same") | **Unsafe.** 1 unsafe preservation (short) and 4 (long) on A/influencing | §9.1. The production counterfactual makes the same false clean on `(S3, e0010)` (§2.3) |
| R3 | **Attention / gradient / token attribution** (attention screeners, integrated gradients) | Not causal; no zero-false-negative guarantee; white-box only; useless across three providers | Jain & Wallace 2019; RTBAS's attention screener is a heuristic |
| R4 | **Surrogate context attribution** (ContextCite, AttriBoT, TracLLM) | Approximate LOO with no FN bound; needs logprobs; answers "which span", not "safe to preserve"; adds queries on top of the replay | Their own papers report precision/recall below 1 |
| R5 | **Training-data influence** (influence functions, TracIn) | Wrong object: training examples, not context sources | — |
| R6 | **Activation patching / causal tracing** | One intervention per forward pass, so the query count is the same; white-box, single model, not portable | ROME/causal tracing |
| R7 | **Static IFC / capabilities** (CaMeL, FIDES) | Prevention, and requires re-architecting agents. Label propagation on read *is* exposure taint, so post-hoc it collapses to roughly B2 | Their designs |
| R8 | **Provenance / lineage / PDG / dynamic taint as the decision** | Exposure, not influence. Over-taints by definition | CLAUDE.md rule; Schwartz et al. 2010 |
| R9 | **Cheaper model for the counterfactual** (routing, cascades) | A different model's dependence on a source says nothing about the original model's. Unsound | — |
| R10 | **Group testing across sources** | Recovery needs only the *joint* intervention on F, which IREC does in one query per event. Already at 1 query per event | gate1 §1 |
| R11 | **Priors, SPRT, Gate 1, closure-scoped deferral, memoization** | Falsified or exhausted | docs/gate1, docs/15 |
| R12 | **Retry-at-scope before escalating** | Not rejected, but **not evaluated**. It changes escalation cost, not the verification predicate. Listed as an open policy question in §10 | — |

---

## 5. Candidates compared, and the selected solution

### 5.1 Candidates

| | candidate | influence or exposure? | safety basis | expected cost vs B2 | provider-portable | prior-art overlap | verdict |
|---|---|---|---|---|---|---|---|
| C1 | **IREC, exact cutoff on consumer prompts** | influence, by intervention (the replay) | input completeness only | **≤ 1 by construction**; < 1 where frontier precision or projection cutoff exists | yes (black-box) | high on mechanism (Retro, agent-rewind) | **selected** |
| C2 | IREC with semantic comparator | influence, by intervention + comparator | comparator soundness | < C1 | yes | high | rejected (R2, measured unsafe) |
| C3 | Lazy CausalLine + prompt caching | influence (CF) | comparator + self-report + verify | > 1 on this workload | yes | low | rejected (R1) |
| C4 | Surrogate attribution + replay | approximate | none (no FN bound) | > 1 | white-box | ContextCite etc. | rejected (R4) |
| C5 | IFC labels at runtime | exposure | label soundness | ≈ 1 | partial | CaMeL / FIDES | rejected (R7) |
| C6 | Activation patching | influence (white-box) | exact on one model | ≈ C1, but white-box | no | ROME | rejected (R6) |
| C7 | C1 + speculative verification against the old output (self-hosted) | as C1 | as C1 | C1 with cheaper per-query cost on unchanged events | self-hosted only | agent-rewind M1 | **optional add-on** |
| C8 | C1 + deterministic decoding (T=0, fixed seed, batch-invariant kernels) | as C1 | as C1 | raises cutoff yield | partial (hosted APIs vary) | Thinking Machines 2025 | **enabler** |

### 5.2 The selected solution: IREC

**Definitions.**

- Every model event `e` has a recorded prompt `p_e` and output `y_e`.
- The prompt is assembled deterministically by pipeline code from recorded inputs:
  sources, upstream message *projections*, memory values and tool responses.
- `F` is the detector's flagged set.
- The recovered prompt `p'_e` is the prompt the pipeline assembles in the recovery run,
  with `F` removed at ingestion and upstream outputs replaced by their recovered values.

**Algorithm.**

```text
IREC(trace T, flagged F, client M):
    D  := []                         # re-executed model events
    Δ  := {}                         # events whose recovered output differs
    rerun the pipeline in its own (topological) order, with F removed at ingestion;
    for each model call the pipeline makes for event e:
        p'  := assembled prompt, redacted of F             # redact_flagged(...)
        if p' == T.prompt(e):                               # byte equality
            return T.output(e)                              # splice, 0 tokens
        y'  := M.generate(p')                               # THE intervention = THE recomputation
        D.append(e)
        if y' == T.output(e): return T.output(e)            # identical bytes: keep
        Δ.add(e); return y'                                  # changed: new output flows on
    non-model events (tools, memory, executor) are recomputed by pipeline code;
      a tool call with byte-identical arguments returns its RECORDED response
      (Temporal-style activity result), so side effects are not re-issued.
    discarded := Δ ∪ {non-model events whose recomputed bytes differ}
                   ∪ {events whose stored output holds flagged content}   # issue #20
    verify(recovered)  — same predicate as today: byte re-check of flagged material,
                         live-memory references, task check;
    on failure: same escalation ladder.
```

**What it needs at runtime.**

- **Data structures: nothing new.** The trace store already keeps byte-exact prompts
  (`prompt_text`), outputs, source contents and pipeline order. The production
  `SplicingClient` already re-runs the pipeline and maps calls to events by order, with
  the `announce` identity check. IREC replaces one condition: `event_id not in
  invalidation` becomes `cleaned_prompt == stored_prompt`.
- **The cutoff must be tested at the consumer's prompt**, i.e. after the pipeline's own
  projection, not at the producer's raw output. In mixed56 a specialist's free text may
  differ, but `_message(f"{site} {answered}")` is byte-identical, so the verifier's
  prompt is identical. This makes cutoff robust to wording churn *without a comparator*.

### 5.3 What happens to the existing modules

| module | fate |
|---|---|
| `src/tracing/*` (logger, content store, checkpoints, pipelines) | **unchanged**; IREC consumes what is already recorded |
| `src/recovery/replay.py` `SplicingClient` | **changed**: splice decision moves from a precomputed invalidation set to prompt equality; `assert_invariants` is redefined accordingly. ~40 lines; the prototype `CutoffClient` is the reference |
| `src/recovery/verify.py` | **kept**. Byte re-check, memory-reference check and task check unchanged. The taint walk becomes a check that every kept event's recovered prompt equals its original |
| `src/recovery/causalline.py` escalation ladder | **kept**; the cost-accounting defect (§2.4) should be fixed regardless |
| `src/recovery/planner.py` | the selective plan is no longer precomputed; still used for escalation scopes |
| `src/provenance/estimator.py`, `selfreport.py`, `attribution.py`, `group_test.py`, `calibration.py`, `removability.py`, `carriers.py`, inline `HybridAttributor` | **unnecessary on the recovery path** (kept for the paper's ablations and comparison). This removes 100% of A and 100% of the normal-run attribution overhead |
| `src/eval/*` | add an `IREC` method row; fix `run_cell`'s missing `tools` |

### 5.4 Exposure versus influence

IREC never decides influence by reading the prompt for the source, or by asking the
agent. It decides by intervention:

| case | what IREC does | cost |
|---|---|---|
| agent **exposed, ignored** the source | its prompt changes (source removed), so it is re-executed once; the field its consumers read comes back identical, so its consumers' prompts are identical and they are spliced | **1 call, 0 downstream** |
| agent **influenced** | re-executed; the field changes; consumers re-executed | as many calls as the change actually reaches |
| agent **not exposed**, but reads an upstream output that was influenced | its prompt changes, so it is re-executed; same two sub-cases | 1 call |
| agent **downstream of an exposed-but-ignoring agent** | prompt identical, spliced | **0** |

**Measured.** In the two medium runs where the models ignored the injection
(`payload_landed: false`), IREC re-executes 3–4 model events (the exposed acquisition
agents and their normalisers) and stops: **0.11–0.14× B2**. CausalLine, which "knows"
these agents were not influenced, still spends 15.8k analysis tokens plus a full B2
replay on them (2.9–3.0×).

### 5.5 The three attack channels

- **Indirect injection (web/tool).** The flagged source is removed at ingestion.
  Consumers are in D.
- **Poisoned memory.** The flagged memory value is removed where it is read. Memory
  writes by changed events are recomputed by the replay itself, so rollback happens by
  re-execution and IREC needs no separate `selective_memory_rollback`. The "memory has no
  call-graph edge" hazard (gate1 §8) **does not arise**, because IREC never consults the
  call graph: a memory flow shows up as a changed prompt.
- **Malicious inter-agent message.** The flagged message is removed. On scripted C this
  is where IREC beats B2 most (0.30–0.40×): B2 attributes the message to the *sending*
  agent and replays everything downstream of it, while IREC starts at the prompt that
  actually contains it.

---

## 6. Cost model

### 6.1 Components

| component | B0 | B2 | CausalLine (hybrid, as measured) | IREC |
|---|---|---|---|---|
| normal-run overhead (model calls) | 0 | 0 | inline self-report on **every model event of every run** | **0** |
| attribution / analysis | 0 | 0 | A ≈ 1.5 N on mixed56 (measured) | **0** |
| replay / recomputation | N | M(closure) | M(region), equal to B2 on mixed56 medium/large | **M(D) ≤ M(closure)** |
| verification | none | none | code checks, ~0 model tokens | same, ~0 |
| equality test | — | — | — | one byte comparison per model event: microseconds, no tokens |
| storage | trace store | trace store | trace store (~482 KB/run on mixed56) | **same** |
| escalation | — | — | ladder; **under-counted** (§2.4) | same ladder |
| latency | all events | closure | analysis + replay (Gate 1 excluded) | D, in dependency order (no worse than B2's replay) |

### 6.2 mixed56 workload-varied (primary), pooled tokens over 5 runs per regime

| regime | B0 | B2 | CausalLine | IREC, no escalation | IREC / min | IREC + real escalation path | / min | target 0.8× |
|---|---|---|---|---|---|---|---|---|
| small | 53,469 | 7,974 | 97,991 | 6,030 | **0.76** | 21,069 | 2.64 (1.13 vs equally verified B2) | met without escalation only |
| medium | 53,568 | 39,874 | 118,854 | 16,024 | **0.40** | 16,024 | 0.40 | **met** |
| large | 51,684 | 48,859 | 128,413 | 46,432 | **0.95** | 46,432 | 0.95 | **not met, and unreachable (§7.4)** |
| **all 15** | | | | | **0.71** | | **0.86** | |

Hosted-only (billed provider) tokens, same runs: IREC/B2 is 0.74 (small), **0.45**
(medium) and 1.00 (large). The saving is not just on the free local model.

**Measured, estimated and assumed:**

- **Measured:** B0, B2 and CausalLine tokens, from `data/results`.
- **Estimated:** IREC tokens. They are priced from a reconstruction of each run's
  145-event trace (same placement, same flagged source ids) using a deterministic stub,
  with proportional pricing scaled so the stub's full pipeline equals the run's measured
  B0. The same pricing reproduces the measured B2 within **0.4–5.1%** on every run.
- **Assumed:** which events change. A model event's output changes in recovery iff its
  real output carried the canary (*faithful replay*, A3).
- **Unknown:** live cutoff yield; live verification-failure rate under IREC.

### 6.3 Scripted matrix (exact ground truth), measured end-to-end through production `run_pipeline`

| cells | IREC / min(B0, B2) | B2 / B0 | CausalLine hybrid / min | lazy / min |
|---|---|---|---|---|
| A (web injection), 8 | 1.00 | 0.83–0.95 | 1.64–2.40 | 1.14–2.00 |
| B (memory), 8 | 1.00 | 0.27–0.33 | 3.80–4.33 | 1.96–2.51 |
| C (message), 8 | **0.30–0.40** | 0.83–0.96 | 0.93–1.60 | 0.37–1.20 |

**Why A and B tie B2.** In this chain the inter-agent messages are raw free text with
per-call wording churn (`ScriptedClient._with_churn`, modelling D-026's 100% text-level
noise floor). Every re-executed event's consumers therefore see changed bytes. Under
exact semantics, B2's closure *is* the minimal D here. Nothing sound can do better on A
and B without a typed interface (§7.3).

---

## 7. Safety and correctness

### 7.1 Definitions

- **Event.** `y_e ~ M(p_e)`, where `p_e = assemble_e(recorded inputs)`.
- **Clean world.** The same pipeline run with `F` removed at ingestion, giving `p'_e`.
- **Recovery-relevant influence.** `F` influences `e` iff `M(p'_e) ≠ y_e`. This is
  CausalLine's leave-one-out definition, applied jointly to `F`, which is what recovery
  acts on.
- **Safe preservation.** Keeping `y_e` is safe iff `y_e` is an output `M` produced on
  the clean-world prompt `p'_e`.

### 7.2 Assumptions

- **A1, input completeness** (the piecewise-determinism analogue). A model call's output
  depends only on its recorded prompt (and system prompt), with no hidden state.
  Memory reads, tool responses and messages are all recorded and assembled into the
  prompt. Hosted provider-side state (server-side conversation memory, caches that
  change outputs) would violate A1.
- **A2, complete ingestion redaction.** Every flagged source is removed wherever it is
  rendered. `redact_flagged(strict=True)` raises if it cannot remove a rendered source,
  and that behaviour is kept.
- **A3, faithful replay.** Needed **for cost only, never for safety**: an unaffected
  agent re-executed on its clean inputs reproduces the projected field its consumers
  read.
- **A4, detector completeness.** `F` contains every malicious source. Every method
  shares this; it is the detector's job.

### 7.3 Theorems and arguments

**Theorem 1 (no single-run clearance).** For a black-box `M`, no rule that sees only
`(p_e, y_e)` and decides "`s` did not influence `e`" has zero false negatives, unless it
never clears an exposed pair.

*Proof sketch.* For any observed `(p_e, y_e)` with `s` in `p_e`, there are two models
consistent with the observation: `M₁` with `M₁(p_e \ s) = y_e`, and `M₂` with
`M₂(p_e \ s) ≠ y_e`. The rule's input is identical under both, so it errs on one of
them. This is the standard fact that noninterference is a 2-safety hyperproperty
(Clarkson & Schneider): refuting it, and certifying it for a given input pair, takes a
second execution. It is also an instance of the causal hierarchy theorem (interventional
facts are not identifiable from one observational sample).

*Corollary.* Clearing an exposed event needs at least one query of `M` on a prompt
without `s`. Logging, provenance, attention or self-report alone cannot clear it with a
zero-FN guarantee.

**Theorem 2 (IREC is sound and query-optimal).**

*Soundness.* Under A1 and A2, every output IREC keeps was produced by `M` on its
clean-world prompt. Either the recovered prompt is byte-identical to the original (so
the original output is a valid output on the clean prompt, by A1), or it was produced
just now on `p'_e`. Hence **zero unsafe preservations by construction**. This holds with
no comparator, no noise floor, no self-report and no removability analysis.

*Optimality.* Let `D = {e : p'_e ≠ p_e}`. For each `e ∈ D`, any sound black-box method
must query `M` on `p'_e`: by Theorem 1, an adversarial `M` can make the unqueried
`M(p'_e)` differ from `y_e`. IREC queries exactly `D`, once each.

Hence `C_IREC ≤ C` for any sound black-box method under exact output semantics, and in
particular:

- `C_IREC ≤ C_B2`, because `D ⊆ closure`: a prompt can change only through a flagged
  source or a changed upstream output, and both lie in the closure;
- `C_IREC ≤ C_B0`.

`D` is defined on the realised replay outputs, so this is instance-wise optimality,
the same notion build systems use for early cutoff.

**Recall.** Under A1, A2 and A4, every truly influenced model event is in `D`. Its prompt
contains a flagged source, or a changed upstream projection, or else it could not have
been influenced. So it is re-executed, and its output is replaced if it changes.

*Measured.* 0 missed canary-bearing model events in all 30 mixed56 runs. 0 unsafe events
against each run's `truth_events` in all 30. 0 unsafe in all 24 scripted cells.

### 7.4 Limits, failure conditions and the impossibility result

- **Comparator cutoff is unsound** (R2). Relaxing exact equality to "semantically equal"
  reintroduces the comparator gap and was measured unsafe. Any relaxation must be a
  *projection the consumer applies anyway* (typed message fields, parsed tool arguments).
  It must never be a similarity judgment.
- **Saturation bound.** The 0.8× target is reachable only where
  `M(D_min) / M(closure) ≤ 0.8`. On mixed56 large, the contamination truly reaches most
  of the closure's model work. Re-running only canary-bearing model events, and
  ignoring the consumers that any method must re-execute, already costs 0.66–0.92 of B2
  per run (0.79 pooled). Under faithful replay IREC's 0.88–1.00 *is* `D_min`.
  **No sound black-box method reaches 0.8× there.** White-box access does not change the
  count: the contaminated outputs must be recomputed, and certifying an unchanged
  consumer still takes a forward pass. White-box access only makes each pass cheaper
  (prefix and KV reuse, speculative verification).
- **Noise costs money, never safety.** If replay is noisy at the projection, cutoffs
  vanish and IREC degrades to B2's cost; the noisy bound measured 0.95–0.99 of B2. A
  noisy *wrong* field fails the task check and escalates, exactly as for CausalLine
  today.
- **Hidden state** violates A1. Detectable cases include a provider-side cache that
  changes outputs or a tool with unrecorded external reads; these must be recorded or
  forced into re-execution.
- **Side effects.** A tool call re-issued with *different* arguments is a new effect.
  The cutoff at tool arguments limits re-issue to calls whose arguments actually changed,
  but irreversible effects that already left can only be reported (as in agent-rewind
  and Warp).

---

## 8. Novelty assessment

**Already published or implemented.**

- Selective re-execution with an input-equality skip and an output-equality stop:
  Retro's predicates and shepherded re-execution, Warp, Aire.
- Early cutoff and backdating: Shake, Bazel, Salsa.
- Change propagation: self-adjusting computation, Adapton, differential dataflow and
  DBSP.
- Noninterference by re-running with high inputs removed: secure multi-execution.
- **For multi-agent LLM workflows specifically:** agent-rewind (UCSD, 2026) re-runs and
  reuses steps whose Merkle-hashed reads verify, with early cutoff, greedy decoding and
  speculative decoding against the previous output, including a TAMAS indirect-injection
  scenario. **A reviewer will treat IREC's execution mechanism as known.**

**Not addressed by that prior art, and defensible:**

1. **The reduction.** In post-detection recovery, *attribution is subsumed by the
   recovery replay*. The counterfactual of the joint intervention on `F` and the
   recomputation are the same model call, so a separate attribution stage (self-report,
   leave-one-out, surrogates) is pure overhead. agent-rewind marks a faulty *producer*
   and treats every reader as in the cone; it makes no exposure-vs-influence claim.
   CausalLine's contribution is to show that exposure-is-not-influence is established by
   the replay itself, at no extra cost.
2. **The lower bound and instance-optimality for sound black-box recovery** (Theorems 1
   and 2), including dominance over B0 and B2. None of the cited systems states it for
   stochastic black-box components.
3. **Negative evidence that comparator-based clearance is unsound**, measured on exact
   ground truth: the production counterfactual's false clean and IREC-comparator's
   unsafe preservations. This is the reason the cutoff must be exact and placed at the
   consumer's projection.
4. **An economic characterisation with an impossibility result.** Savings come only
   from (a) frontier precision (prompt-level entry vs agent-level origin) and (b)
   projection cutoff. On saturated workloads, no sound method meets 0.8×. Measured on the
   project's 56-agent, three-provider workload with exact recall and unsafe accounting.

**Not defensible:** IREC as a new recovery *mechanism*; any claim that CausalLine
invented early cutoff for agents.

**Experiments that would establish the contribution:**

- the §9.3 live runs;
- an exact-ground-truth comparison of IREC against CausalLine (hybrid, lazy) and against
  agent-rewind's producer-level policy, on the same placements;
- a typed vs free-text interface ablation, to quantify mechanism (b).

---

## 9. Validation evidence

### 9.1 Reproduced and tested offline (this report)

| what | result | where |
|---|---|---|
| harness reproduces documented numbers | B/exposed_only: CausalLine 0.947, B2 0.579 (docs/15 §3) | `cutoff_replay.py` |
| IREC exact, 24 scripted cells, both pricing models | 0 unsafe, 24/24 task success, cost ≤ min(B0, B2) in 24/24, 0.30–0.40 on C | `scripted_matrix_results.json` |
| IREC with comparator | **1 and 4 unsafe** on A/influencing (short, long) | §4 R2 |
| production counterfactual false clean | `(S3, e0010)` clean under `targeted_only`; truth says influenced | §2.3 |
| mixed56 reconstruction | 145 events, same flagged ids and placement; B2 pricing within 5.1% of measured | `mixed56_cp.py` |
| mixed56 IREC simulation, 30 runs | 0 unsafe vs `truth_events`, 0 missed canary events, ratios in §6.2 | `mixed56_*_sim.json` |
| accounting defects | `run_cell` tools; `last_report` replay tokens | §2.4 |

The prototype runs under the repository's Python 3.11 code through an import shim
(`py39shim.py`), because this machine has only 3.9. It modifies nothing on disk.

### 9.2 Demonstrated in source systems (not in CausalLine)

- Selective re-execution with equality cutoff recovers real intrusions while preserving
  legitimate work: Retro, Warp, Aire.
- Early cutoff is sound and production-grade for deterministic tasks: Shake, Bazel,
  Salsa.
- Greedy-decoding reruns matched plain greedy output in 74/74 cases up to 512 tokens
  (agent-rewind's own report).
- Batch-invariant kernels made 1,000 temperature-0 completions identical (Thinking
  Machines, secondary reports).

### 9.3 Not established: the minimal measurement required

1. **Live cutoff yield (A3).** For each of the 15 primary placements, run IREC once
   against the same models: an estimated ≤ 1 B2 replay per run, about 120k tokens total,
   no analysis. Record per re-executed event whether the projected field matches the
   original. This replaces the faithful-replay assumption with a measurement; the ratios
   in §6.2 are its prediction.
2. **Verification-failure rate under IREC vs verified B2**, on the same runs, so the
   small-regime economics compare like with like.
3. **A typed-interface variant of the scripted chain** (Researcher findings passed as
   parsed fields), to show mechanism (b) on exact ground truth.

---

## 10. Final recommendation

**Implement IREC behind a flag (CONDITIONAL GO).** Gate any default change or paper
claim on the §9.3 measurement.

**Why it is worth doing.** It is the only candidate that:

- removes the entire analysis tax (about 16k tokens/run, roughly 1.5 N, on the primary
  workload) and all normal-run attribution overhead;
- keeps CausalLine's core distinction (exposed-but-ignoring agents and their downstream
  are preserved) and establishes it by intervention;
- makes zero unsafe preservation a consequence of construction, not of a comparator,
  self-report or calibration;
- is provably never more expensive than B2 or B0 before escalation.

On existing evidence it brings CausalLine from **3.57×** to **0.71×** of the cheaper
baseline over the 15 primary runs (0.86× with escalations charged). It meets 0.8× where
the workload permits (medium) and is strictly below both baselines in every regime when
there is no escalation.

**What it does not do:**

- **It does not make large-regime recovery 20% cheaper than B2.** Nothing sound can on
  that workload (§7.4).
- **It does not by itself fix the small-regime verification loss.** That needs either
  symmetric accounting (verify B2 too) or a retry-at-scope policy, which is unevaluated.
- **It is not a new mechanism.** The paper must cite Retro and agent-rewind and claim
  the reduction, the bound and the measurements, not the replay engine.

**Housekeeping that should land regardless:**

- fix `run_cell`'s missing `tools` for long-workflow baselines;
- charge every escalation attempt's replay in `recover()`;
- record in `docs/05-decisions.md` that CausalLine's event-level safety on A/influencing
  currently rests on self-report positives (§2.3).
