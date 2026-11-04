# CausalLine: Project Briefing Report

**Influence-aware selective recovery after prompt injection in multi-agent LLM systems**

Repository: `github.com/Siddharthpandey20/CausalLine`, branch `main`
Prepared for a supervisory discussion with a visiting professor, 23-10-2026.

> Written on `feat/phase-gated-remediation`, which was consolidated into `main`
> on 04-11-2026 — `main` now holds every commit from every branch. Two things
> here have since been superseded: §5.5's "−3.6 pts on the chain workflow" and
> the per-workload restriction that follows from it are **not reproducible**
> across 30 seeds (`docs/15-cost-direction-reopened.md`), and §10.2's "Outcome
> B" rests on that same figure. Every safety number is unchanged.

---

## How to use this document

This briefing is for someone meeting the project for the first time. It is written so that one person can walk through it in a meeting.

| If you have… | Read |
|---|---|
| 2 minutes | §0 (one-page summary) |
| 10 minutes | §0–§4 (problem, idea, positioning, formal problem) |
| 30 minutes | all of it, and §14 (where we want guidance) |

Every number here comes from the repository's committed result files and reports. The source document for each is given in brackets, e.g. `[docs/14]`.

---

## 0. One-page summary

**The problem.** Multi-agent LLM systems can be compromised by prompt injection that arrives through a web tool, a shared memory entry, or a message from another agent. Most research is about *detecting* that this happened. We start one step later. Once a detector has named a malicious source, **which of the work the system has already done must be thrown away and recomputed, and which can safely be kept?**

**Current practice** is to discard the compromised agent's output and everything reachable from it in the call graph. That is safe, but often far too broad, because **reachability is not causation**. An agent that *received* a poisoned value may never have *used* it.

**Core idea: exposure is not influence.** We record which sources were *present* in each agent's context (exposure). After detection, we establish with evidence which sources actually *changed* each output (influence). We then recompute only the influenced work, splice everything else back byte-for-byte, and **verify** the recovered run before accepting it. If verification fails, we escalate: selective, then agent restart, then full restart.

**What we built.** A working Python system of about 36,000 lines. Its core uses only the standard library. It has: an execution tracer, an influence estimator (self-report plus counterfactual replay), a contamination propagation engine, a recovery planner, a selective replay engine, a verification/escalation layer, three baselines, and two evaluation harnesses (scripted and real-LLM). There are about 490 passing unit tests.

**Main result** (56-agent, 6-stage, 145-event workflow over 3 inference providers, 15 runs, 15 distinct attack placements) `[docs/14, minor_report]`:

| Regime | True contaminated fraction | B2 (topology closure) | CausalLine | Paired Δ | Win/Tie/Loss |
|---|---|---|---|---|---|
| small | 6–9% | 89.9% | 72.6% | **−17.4 pts** | 2/2/1 |
| medium | 52–57% | 37.4% | 44.4% | **+7.0 pts** | 5/0/0 |
| large | 75–77% | 8.6% | 23.7% | **+15.2 pts** | 5/0/0 |

- **Safety:** 0 unsafe preservations, 0 contamination escapes, recall 1.0, and 15/15 correct final outcomes.
- **Cost:** CausalLine is **more expensive in tokens than every baseline, including full restart** (1.8×–2.5× a full re-run). We do not claim it is a cost optimisation.
- **Honest boundary:** it wins where reachability badly over-discards. It loses where the closure is already near-optimal, so it should not be used there.

**What we claim and what we don't.** We **do not** claim the exposure/influence distinction or counterfactual attribution as new; both are 2026 prior work. We claim the *combination*: a detector-triggered, verified selective recovery with three-relation propagation, byte-exact splicing and an escalation fallback. We also claim its evaluation at a heterogeneous multi-provider scale, and an honest economic characterisation of when *not* to use it.

---

## 1. The problem and why it matters

### 1.1 Setting

A multi-agent LLM system is a set of LLM agents that exchange intermediate results and read from external channels:

- **tools**, e.g. a web fetch returning a page
- **shared memory**, e.g. a policy entry written earlier
- **other agents**, as inter-agent messages

Each channel is a route by which attacker-controlled instructions can enter. This is *indirect prompt injection* (Greshake et al. 2023; Perez & Ribeiro 2022; Liu et al. 2024).

### 1.2 The gap: everyone stops at detection

There is a large literature on preventing or detecting injection. There is very little on **what to do next**. When an operator learns that "the Researcher agent consumed a poisoned page", the safe response available today is to throw away everything downstream.

### 1.3 Motivating example (from the original project idea, `docs/00-idea.md`)

```
User → Planner → Researcher → Coder → Executor

Researcher reads:  A = trusted docs,  B = trusted DB,  C = malicious web page
Researcher outputs: R1 (from A+B)  R2 (from A)  R3 (from C) ✗  R4 (from B)

Coder receives R1, R2, R3, R4 but writes code using only R1, R2, R4.
```

- **Reachability view:** R3 → Coder → Executor, so everything after the Researcher is contaminated, and the Researcher's other three outputs go too.
- **Influence view:** only R3 is contaminated. The Coder and Executor are clean, so recompute R3 only.
- **If the Coder *did* use R3:** the chain R3 → Coder decision → code → Executor action is contaminated. Recompute that chain, and only that chain.

The target is to **recover the workflow, not necessarily the whole agent**.

### 1.4 Three observations behind the design `[minor_report §II]`

1. **Recovery is coarse because precision is unavailable, not because anyone wants it coarse.** In ordinary programs, taint tracking can see every data dependency in the instruction stream. Inside an LLM call it cannot; the only record of whether a source mattered is the output itself.
2. **The cost of over-discarding grows with system size and with fan-out.** A coordinating hub that broadcasts to many agents makes every later agent *reachable* from every earlier source, so reachability-based recovery discards almost the entire trace. Agent frameworks are moving toward exactly these shapes.
3. **A recovery that is wrong is worse than one that is wasteful.** Preserving a contaminated event means the attack survives the incident response. This asymmetry drives every design decision (see §5.2).

---

## 2. Vocabulary (used identically in code and paper)

| Term | Meaning |
|---|---|
| **event** | one recorded operation: message, tool call/response, memory read/write, agent output |
| **source** | an incoming unit of information, with an ID (`S1`, `S2`, …) |
| **exposure** | a source was *present* in an agent's context |
| **influence** | a source *demonstrably changed* the agent's output |
| **contaminated** | influenced, directly or transitively, by a malicious source |
| **clean** | *established* as not influenced; "never checked" is not clean |
| **recovery set** | the events to invalidate and recompute |
| **unsafe preservation** | we kept something that was actually contaminated. This is the dangerous error and is always reported |
| **carrier** | an event whose output is a copy of an earlier one (e.g. a hand-off); it *inherits* verdicts rather than having its own |
| **removability** | whether redacting a source actually removes its information from the prompt |
| **work preserved** | 1 − \|recovery set\| / \|all events\|. Work is counted in events, never in sources |

---

## 3. Positioning: related work, the gap, and what is ours

### 3.1 What the literature already establishes (we build on these, we do not claim them)

| Area | Representative work | What it gives us |
|---|---|---|
| Attack surface | Greshake 2023; Perez & Ribeiro 2022; Liu 2024 (USENIX Sec) | The threat is real; the delivery channels |
| Prevention | StruQ (Chen 2024) | Prompt-interface hardening, which is upstream of us |
| Exposure ≠ influence for LLM agents | NeuroTaint (Cai et al., arXiv 2604.23374); argument-level provenance (Fan et al., arXiv 2605.11039) | The core distinction, applied to **detection/enforcement** |
| Counterfactual attribution | Causal Agent Replay (Shah, 2606.08275); CausalFlow (Bonagiri et al., 2605.25338) | Intervention as the mechanism, applied to **capability failures with no adversary** |
| Contamination in multi-agent traces | Mazhar et al. (2604.27586) | Contamination is measurable; structural divergence and outcome corruption are decoupled |
| Checkpoint/intervene/resume | DoVer (2512.06749) | Mechanically close to our splice layer, but aimed at debugging |
| Avoiding replay cost | Kang et al. zero-replay debugging (2606.14805) | The same cost problem, approached from the opposite side |
| Survey | Wang et al., execution-provenance survey (2606.04990) | Lists recovery only as retry / compensation / checkpoint-restore, and notes rollback can be **semantically unsafe** |
| Classical | Newsome & Song 2005 (taint); King & Chen 2003 (backtracking); Elnozahy 2002 (rollback-recovery) | Reachability propagation, which is exactly our B2 baseline |

### 3.2 The research gap (three parts)

1. Existing influence tracking answers *"did untrusted data reach a sink?"* It does not answer *"which artefacts already produced may be kept?"*
2. Attribution work targets capability failures with **no adversary**, where a wrong attribution costs accuracy. In our setting a wrong attribution **preserves an attack**. That requires an asymmetric evidence discipline.
3. Where recovery appears at all, it is retry or rollback with no verification gate. We found **no published system** that combines all of the following:
   - triggered by a detector's verdict on a malicious *source*
   - propagation restricted below reachability
   - output gated on verification before it is accepted

### 3.3 Our contribution claims, in decreasing order of defensibility

1. **Recovery as a causal problem over a recorded trace, not graph reachability over agents.** Contamination propagates only along evidence-backed relations (§6).
2. **An influence estimator with an explicit soundness discipline.** Positive evidence is cheap to accept. Negative evidence must be verified. Unexamined pairs count as contaminated.
3. **A verification and escalation layer.** It makes "never worse than restart *in outcome*" a structural property rather than an empirical hope.
4. **The planner optimises exactly what the verifier checks.** The cover target is the contamination closure itself, so a selective plan passes verification by construction (see §5.5 for the bug this fixed).
5. **Atomic units for recorded redundancy.** This is our principled departure from naive leave-one-out (§5.3).
6. **Evaluation at scale and heterogeneity:** 56 agents, 3 providers, 3 channels, 15 placements. It comes with an honest economic analysis and documented negative results.

**Explicitly not claimed:** a new detector, a new attack, an optimality result, or that it is cheaper than restart.

---

## 4. Formal problem definition `[minor_report §IV]`

- A completed run is a trace **T = (E, S)**, where E is the sequence of events and S the set of sources.
- Each event e has exposures **X(e) ⊆ S**.
- A detector gives a flagged set **M ⊆ S**.
- Let **C(M) ⊆ E** be the events *truly* influenced by M. This is not observable.

**Find an invalidation set I ⊆ E such that:**

- **Safety (hard constraint):** C(M) ⊆ I. Any e ∈ C(M) \ I is an *unsafe preservation*.
- **Objective:** minimise |I|, i.e. maximise *work preserved* = 1 − |I|/|E|.

Since C(M) is unobservable, we compute an estimate Ĉ(M) and must ensure **C(M) ⊆ Ĉ(M)**, so we over-approximate when unsure.

- **Baseline (reachability):** I = desc(M), everything downstream. This is safe, but it maximises |I|.
- **Our objective:** make Ĉ(M) strictly smaller than desc(M), never violate safety, and verify before accepting.

**Complexity note.** Choosing the cheapest set of recovery *actions* that covers the closure is a weighted hitting-set / minimum-cost cut on a contamination DAG, which is **NP-hard**. We use the standard greedy set-cover approximation (H(n) bound) and claim *"small and safe, measured"*, never *"minimal, proved"* (decision D-002).

---

## 5. Methodology: how influence is established

This is the technical heart of the project, and the part the other professor is most likely to probe.

### 5.1 We cannot see inside an LLM call

Attention weights are not causation, and self-explanations can be confidently wrong. So influence is **estimated**, in two tiers: cheap first, then expensive.

**Tier 1: Self-report (one cheap call).** We ask the agent, in a fixed structured format, which inputs it used. This gives a *claim*, not evidence. A source the agent does not mention is treated as `used` with confidence 0, so silence never clears a source.

**Tier 2: Targeted counterfactual (expensive, this is the evidence).** We re-issue the *exact stored prompt* with the suspect source's block redacted, and compare the two outputs.

### 5.2 The asymmetry that makes a cheap, unreliable instrument usable

| Claim | If wrong… | Treatment |
|---|---|---|
| "I used S3" (positive) | we recompute work that was fine. Wasteful, but safe | **accepted at face value** (plus a random audit sample, to measure the over-claim rate) |
| "I did not use S3" (negative) | we preserve contaminated work, which is **unsafe** | **must be verified by a counterfactual** before acting |
| no verdict at all | — | `unchecked`, which the walk treats as **contaminated** |

Running out of budget therefore costs *work preserved*, never *safety*. This holds structurally in every component.

### 5.3 What "the output changed" means: decision signatures, not text

We measured this rather than assuming it (decision D-026). On the live model at temperature 0, re-sending an **identical** prompt gave 8 of 8 textually distinct answers. So the textual noise floor is **100%**: a text difference carries zero information. The *decision* underneath (e.g. which library was chosen) had a **0%** floor (1 distinct value across 9 samples).

So comparators read **structured decision facets** instead: chosen library, format codes, control flow, JSON shape, tool arguments, and a `carryover` facet for verbatim copying. There is one comparator per event kind, and each returns a three-valued verdict.

**Pre-registered calibration rule:** a facet whose floor on *unchanged* re-sends is non-zero is excluded, because it cannot carry evidence. A facet is **never** dropped because of what it says about influence. This is calibration against the null, not tuning toward the outcome.

### 5.4 Four refinements that each close a specific false-clean route

1. **Removability check** (D-066, `src/provenance/removability.py`). Leave-one-out assumes that deleting block `[S]` removes S's information. That fails if the same material reaches the prompt by a second route, e.g. quoted inside another source. A real-model false clean had exactly this shape. The check is textual and deterministic: redact the source, then test whether its distinctive spans survive elsewhere in the prompt. It costs zero model calls. If information remains, the "clean" verdict is void.
2. **Honest carriers** (D-067, `carriers.py`). A hand-off or tool event that only copies an upstream output *inherits* that output's verdict. A bug had stamped these inherited clearances as `structural / confidence 1.0`, the strongest label available, even when nothing upstream had been checked ("clearance laundering"). Fixing it removed a genuinely contaminated event from the preserved set. Carriers are now pointers to an upstream verdict, resolved at read time.
3. **Atomic units for redundancy** (`group_test.py: merge_derived_units`). Leave-one-out cannot see **over-determination**. If two sources carry the same poisoned fact, removing either one alone changes nothing, so both get reported clean. Where the trace *records* the redundancy, we merge the sources into one unit that is always removed together. This covers two shapes: *direct* (A was derived from B) and *shared ancestor* (both derived from the same poisoned page). Effect on the long workflow: work preserved went from 11.1% (losing to B1) to **37.0%** (+22.2 over B1), with escalations 1 → 0 and unsafe 0 → 0.
4. **Ingestion relation** (issue #20; see §10.1). This was a missing relation, found by observed ground truth.

**The theoretical limit, stated precisely.** *Coincidental* redundancy (two independent sources stating the same fact, with no recorded link) is **not** handled. This is the known limit of single-variable counterfactual testing. Halpern–Pearl / Chockler actual causality handles it through *contingencies* (AC2), and finding a witness contingency is **Σ₂ᵖ-complete** in general. A trace-based system can be *sound* about recorded redundancy and only *heuristic* about the rest.

### 5.5 Making counterfactuals cheaper

- **Group testing** (Dorfman 1943). Remove a whole group in one call; if the decision doesn't move, the whole group is clean, otherwise split and recurse. Measured: **15% fewer calls**. Below about 4 candidates, halving cannot pay. It assumes no interaction effects, which is the same blind spot leave-one-out has.
- **Wald's SPRT.** A sequential test stops investigating once the evidence decisively says "contamination is spreading, restart is cheaper". It is inert on a 19-event trace and **fires** on 27 events.
- **Targeted attribution** (D-032). Counterfactuals run *after* detection, on the flagged region only.
- **Lazy self-report** (D-082). Self-report is also deferred until after detection. Measured: **72% cheaper and outcome-identical on 12/12** paired fan-out runs, but **−3.6 pts** on the chain workflow. It is per-workload, not global.

---

## 6. System architecture

### 6.1 End-to-end pipeline

```
                          ORIGINAL RUN  (only stage that costs anything if no attack)
   agents execute  ──►  Stage 1: PROVENANCE RECORDING
                        trace.jsonl + content store + checkpoints
                        (events, sources, exposures, structural/carrier/ingestion records)
                                    │
                                    │  detector: "sources S4, S9 are malicious" (+confidence)
                                    ▼
   Stage 2  INFLUENCE ATTRIBUTION     self-report → counterfactual, flagged region only
   Stage 3  CONTAMINATION CLOSURE     walk influence / derivation / ingestion only
            + SAFE FRONTIER           latest checkpoint per agent whose causal past is clean
   Stage 4  PLAN + SELECTIVE REPLAY   greedy cover; recompute I, splice everything else
   Stage 5  VERIFY                    task check · live-memory check · redaction check
               │ fail
               └──► escalate: selective → agent_restart → restart_all
```

**Timing.** Recovery is **post-hoc and batch**, on a finished trace (D-074). This is deliberate, not a time constraint. Establishing *non*-influence needs a counterfactual of an output that already exists, so the central measurement is not available online. An online variant would be a different method.

**Detector interface.** The detector is a pluggable *socket*. It emits **source IDs with confidences**, never "agent X is compromised". An agent-level verdict would *be* baseline B1.

### 6.2 Three graph representations

1. **Call / topology graph:** who called whom. It is used for context and for the baselines, and **cannot** determine contamination on its own.
2. **Event graph:** every operation as a node, with parent links. This is execution history.
3. **Provenance / influence graph:** source → event edges, each labelled with *how* it was established (`self_report`, `counterfactual`, `structural`, `assumed`). **The gap between the exposure set and the influence set is the whole contribution.**

### 6.3 Contamination propagation (Algorithm 1, `src/provenance/contamination.py`)

It walks **exactly three relations**:

| Relation | Direction | Meaning |
|---|---|---|
| influence | source → event | this source changed this output (evidence-backed) |
| derivation (`derived_from`) | event → source | this source *is* that event's output, handed to a downstream agent; lets taint cross agent boundaries |
| ingestion | source → event | the event that *retrieved* the source stores its text in its own output |

```
Ĉ ← ∅; Q ← M
while Q not empty:
    pop s
    for e with influence/ingestion edge from s:        Ĉ ← Ĉ ∪ {e}    (established)
    for e with s ∈ X(e) and (s,e) not cleared:          Ĉ ← Ĉ ∪ {e}    (unchecked ⇒ contaminated)
    for e ∈ Ĉ and s' with derivedFrom(s') = e:          Q ← Q ∪ {s'}    (cross agent boundary)
return Ĉ
```

It **never walks `parents` edges**. Those record execution order, not data dependence, and walking them transitively *reproduces baseline B2 exactly*. That would be a method silently becoming its own baseline. `Source.malicious` (ground truth) is never read by the method; only `src/eval/metrics.py` and simulated detectors may read it.

### 6.4 Recovery planner (`src/recovery/planner.py`, `policy.py`)

- **Step 0:** contamination closure (above).
- **Step 1, safe frontier:** for each agent, the latest checkpoint whose *causal past* is disjoint from the taint. This is not simply the latest checkpoint before the attack in time. A cross-agent **domino pass** catches a kept event that was influenced by one we are about to discard.
- **Step 2, greedy set cover:** over the actions `replay`, `invalidate`, `restart(agent)`, `restart_all`, `isolate`. Each action costs measured tokens. We pick by cost / targets covered, capped at restart_all.
- **Key correction (§3.3 claim 4):** the cover target used to be "cut every malicious-source → final-output path", while verification demanded an empty taint set. A tainted event lying on no such path satisfied the planner but failed the verifier, so plans were systematically rejected and every influencing run escalated. Now **the covering target is the closure itself**, and a test pins this.

### 6.5 Selective replay (`src/recovery/replay.py`)

```
for each event e:
    if e ∉ I: splice(e, logged_output(e))          # never call the LLM
    else:     re-invoke e with flagged sources redacted and clean upstream spliced in
```

Two **runtime-asserted** invariants:

1. every spliced output is byte-identical to its log entry (a 16-char content-hash comparison)
2. the client *refuses* any model call for an event outside I

A redaction that cannot be completed **aborts** the recovery. It does not ship a run that claims a removal which never happened. Replay never depends on the model reproducing earlier text; it reuses logged bytes everywhere outside I.

### 6.6 Verification and escalation (`src/recovery/verify.py`)

A recovered run is accepted only if all of these hold:

- the task-level outcome is correct
- the taint set on the new trace is empty
- no live memory entry points at an invalidated write
- no flagged source remains in any re-issued prompt (checked by reading the actual bytes sent, D-068)

On failure the scope widens: **selective → agent_restart → restart_all**. Restart is never the primary action and is always the guaranteed fallback.

**This is why safety survives total detection failure.** With a simulated detector miss rate of 1.0 (it flags nothing), CausalLine still recorded **0%** unsafe preservation, because the task check fails and the ladder climbs. The baselines have no backstop and record 50%.

### 6.7 Data and storage layer

There is no database; storage is append-only files, a direct consequence of auditability. A recovered run is a *new* trace that references the old one.

| Store | Content |
|---|---|
| `run.jsonl` | tagged records: `meta`, `source`, `event`, `influence`, `check`, `usage`. Flushed as written, so a crashed run still leaves a valid partial trace |
| `run.content.jsonl` | content-addressed store (`ref = "c" + sha256[:16]`) of prompts, outputs, tool args |
| `run.checkpoints.jsonl` | per-agent-boundary snapshots of context and memory |
| memory fixture | per-run JSON, so one run's poisoned writes can't leak into the next |
| `data/results/` | one JSON per experimental run; every table regenerates from these |

The `check` record exists so the trace can distinguish **"checked and clean"** from **"never checked"** (D-024). Without it the system cannot be conservative correctly.

API keys are environment-only. A repository-wide test asserts that no key value appears in any file, trace or result.

### 6.8 Code layout

```
src/common/      shared data models (frozen in week 1), LLM clients (Gemini, NVIDIA, local Llama), prompts, content store
src/tracing/     event logger, graphs, checkpoints; pipelines: chain, fan-out, 56-agent mixed
src/provenance/  self-report, estimator, signatures/comparators, removability, carriers,
                 group testing, contamination walk, noise calibration
src/recovery/    planner, policy (actions/costs), replay, verify, SPRT, causalline.recover()
src/eval/        attacks, baselines B0/B1/B2, detectors, metrics, economics, campaigns,
                 real-LLM scenario generation, 56-agent validation
src/risk/        per-channel attack-probability model
tests/           ~30 test modules
docs/            00–14 + gate1/ + local_llm_frontier/ (decision log D-001…D-09x)
paper/           conference draft "Exposure Is Not Influence"
minor_report/    mid-term report (LaTeX) + figure-generation scripts
```

---

## 7. Evaluation methodology

### 7.1 Two evaluation modes, never pooled

| | **Scripted mode** | **Real-LLM mode** |
|---|---|---|
| Agent | `ScriptedClient`, a deterministic client whose usage rule we wrote | hosted/local models (Gemini, NVIDIA Nemotron, local Llama-3) |
| Ground truth | **known by construction**, per (source, event) pair | **observed**: a canary token in the output, plus code-path records |
| Strength | the only place per-pair accuracy can be measured exactly; free; reproducible | the realism the scripted mode lacks |
| Weakness | not a real model | sees only influence that leaves a token behind, so it *understates* contamination (the dangerous direction) |

The scripted client deliberately reproduces the two properties measured on the real model: the *text* changes on every call (it appends a nonce), while the *decision* moves only if a used input changed. A comparator that fails here is certainly wrong. One that passes is not thereby proven right on a real model.

**Canary ground truth.** Each payload instructs the victim to emit a specific wrong value (e.g. access code `ZZ999`) that appears nowhere in the corpus. If it shows up in an output, that output was influenced. This is a byte-level substring test, not anyone's opinion. A run where the model ignored the injection is flagged `payload_landed: false`, and its "0 unsafe" is reported as **arithmetic, not evidence**.

### 7.2 Baselines (all output a discard set of events)

| | Behaviour | Role |
|---|---|---|
| **B0** full restart | discard everything | trivially safe; 0% preserved |
| **B1** agent-level taint | discard the compromised agent from the compromise onward, plus all downstream agents | "current practice". It is given the *temporal cutoff* deliberately, to be fair |
| **B2** topology closure | discard everything reachable in the call graph | the reachability bound, the classic taint-tracking analogue |
| **CausalLine** | discard only the influence-connected closure, verify, escalate | ours |

Baselines ship *unverified* recoveries. Only CausalLine checks its own work and gets charged when that check fails. This is a deliberate asymmetry in the comparison, and it penalises us (§8.1).

### 7.3 Metrics

- **Work preserved** = 1 − |I|/|E|, the headline number.
- **Unsafe preservations**, count of truly contaminated events kept. Must be 0, always reported, and always read together with work preserved.
- **Contamination escapes**, events in Ĉ outside the structural closure. This is a sanity invariant.
- **Recall**, whether every planted influence was contained.
- **Recovery success**, a correct final task outcome judged by an exact check, never text similarity.
- **Token cost**, split into **analysis** (attribution) and **replay**.
- **Delivered scope**, which rung of the ladder produced the accepted run.
- **Payload landed** (real-LLM only).

There is no confusion matrix or training curve, because this is not a supervised classifier.

### 7.4 Detectors (the socket's occupants)

- **Oracle:** flagged = planted.
- **Heuristic / transformer classifier:** a real detector that never sees labels.
- **Pessimistic:** over-flags.
- **Blind:** flags nothing; a control.
- **Simulated:** tunable miss rate, confidence and latency.

### 7.5 Testbeds (three shapes, each built to make one quantity measurable)

1. **Chain** (Planner → Researcher → Coder → [Reviewer] → Executor; 19 events, a "long" variant with 27). Scenarios:
   - **A** poisoned web page
   - **B** poisoned tool output
   - **C** poisoned memory / malicious inter-agent message

   Each comes in an **influencing** and an **exposed-only** variant. Here contamination fraction `f` is large by construction.
2. **Fan-out** (K analysts → aggregator → executor). The contaminated region is a constant 3 events whatever K is, so `f` becomes a dial (0.21 → 0.06).
3. **56-agent mixed-provider workflow** (the primary testbed, below).

### 7.6 The 56-agent, three-provider testbed `[docs/real_mixed_model_60_agent_experiment.md, docs/14]`

| Stage | Role | Agents | Events |
|---|---|---|---|
| A | Acquisition (each fetches its own facility record) | 12 | 48 |
| B | Normalisation (memory + a block of A) | 10 | 30 |
| C | **Hub** (fan-in over B, then broadcast roster) | 1 | 2 |
| C | Specialists (roster + one B block; each *uses* one record) | 12 | 24 |
| D | Verifiers | 10 | 20 |
| E | Reviewers | 8 | 16 |
| F | Synthesis / Audit / Executor (exact check, **no model call**) | 3 | 5 |
| | **Total** | **56** | **145** |

- **Providers:**
  - 50 agents on local `llama3:latest` (8B, Ollama)
  - 2 on `gemini-3.8-flash` (the hub and synthesis fan-in points)
  - 4 on NVIDIA `nemotron-3.5-lightning-30b-a3b`

  The web-channel contamination path crosses local → Gemini → NVIDIA → local, with different tokenizers and instruction-following at every hop.
- **Why the hub matters:** every specialist is *exposed* to every record and *uses* exactly one. This is the exposure/influence gap built structurally. If CausalLine can't beat B2 here, it can't beat it anywhere.
- **Three attack channels with genuinely different propagation:**
  - **web/tool:** has an origin event
  - **poisoned memory:** has *no call-graph edge* back to whoever wrote it
  - **inter-agent message:** no derivation link
- **Payloads** are written per channel to match the victim's actual job (a real attacker does this), and are always planted *beside* a record, never spliced into it.
- **Regimes are set by *where* the attack enters, not by a knob:**
  - **small:** one message into a stage-D verifier
  - **medium:** 2–4 poisoned stage-A records
  - **large:** 8–10 records, 5–7 memory entries and 1–3 verifiers
- **Workload variation:** the seed picks *which* records/entries/verifiers are poisoned and how many, via SHA-256(regime, seed). That gives **15 distinct placements**. A test asserts that the seed reaches only the attack placement.
- **Config:** temperature 0; oracle detector; 5 fixed seeds × 3 regimes; all methods scored on the *same* trace (a paired design). Hardware: i7-13620H, 15.7 GB RAM, RTX 3050 6 GB, Windows 11, Python 3.13.
- **Pre-registration:** architecture, providers, payloads, regimes, baselines, estimator settings and metrics were fixed before the first run and not changed afterwards.

---

## 8. Results

### 8.1 Primary result: 56-agent, 15 runs, 15 distinct workloads `[docs/14]`

| Regime | f true | B0 | B1 | B2 | CausalLine | Δ vs B2 (sd) | W/T/L |
|---|---|---|---|---|---|---|---|
| small | 6.2–9.0% | 0.0% | 89.9% | 89.9% | 72.6% | **−17.38** (41.97) | 2/2/**1** |
| medium | 51.7–57.2% | 0.0% | 38.1% | 37.4% | **44.4%** | **+7.03** (1.85) | **5**/0/0 |
| large | 75.2–77.2% | 0.0% | 9.2% | 8.6% | **23.7%** | **+15.17** (1.54) | **5**/0/0 |

**Reading it:**

- **The gap grows with contamination.** As more is poisoned, the closure collapses toward discarding everything (B2 keeps 5.5–11%), while influence analysis still finds about a quarter of the trace to keep. Reachability discards up to **91.4%** of the trace where the genuinely affected fraction is **76.3%**.
- **The small-regime loss is the design behaving as specified, not noise.** In seed 20260917, selective replay failed verification, then agent restart failed verification, and recovery delivered *correctly* at full restart. That preserved 0% against B2's 92.4%. B2 was never charged because it never verifies; in these runs it happened to be right.
- **Deployment rule:** *where the topology closure is already near-optimal, don't use this method.*
- **No pooled aggregate is quoted.** Pooling gives +1.61 ± 26.64, an average over regimes with opposite signs, dominated by one run.
- **No significance test.** Five draws per regime support descriptive statistics and a sign count, not a p-value.

### 8.2 Safety, which is the result we would withdraw the project over

| | small | medium | large |
|---|---|---|---|
| Unsafe preservations (all methods) | 0 | 0 | 0 |
| Contamination escapes | 0 | 0 | 0 |
| Payload landed | 5/5 | **3/5** | 5/5 |
| Recall | 1.0 | 1.0 | 1.0 |
| Final task correct | 5/5 | 5/5 | 5/5 |

Two medium runs did not land their payload, so their zero is arithmetic. They are kept, because excluding them on outcome would be selection, but they are flagged.

**Scripted matrix** (96 cells = 4 detectors × 3 scenarios × 2 variants × 4 methods, 30 repetitions each): CausalLine had **0 unsafe preservations in all 24 of its cells**. The only unsafe cells (8) are *baselines* under weak detectors. This also holds under an adversarial self-reporter that lies about which source moved its output.

### 8.3 Economics: the uncomfortable number, reported up front

| Regime | B2 tokens | CL analysis | CL total | CL vs full restart |
|---|---|---|---|---|
| small | 1,595 | 16,312 | 19,598 | 1.8× |
| medium | 7,975 | 15,796 | 23,771 | 2.2× |
| large | 9,772 | 15,911 | 25,683 | 2.5× |

- Attribution dominates: **62–83%** of total cost.
- **Break-even condition (derived, not assumed):**
  - investigate-and-recover beats restart iff **A/N + f < 1**
  - A = analysis tokens, N = full-restart tokens, f = fraction replayed
  - On the chain workflow A/N ≈ 1.23, flat across a 42% longer trace, so it never pays.
- **The first measured win** `[docs/local_llm_frontier/04]` came on the fan-out shape with lazy self-report: **A/N + f = 0.93 at K=8 and 0.59 at K=16**, with zero escalations in 18 landed runs (llama3.2:3b, 3 repetitions). **Both levers are needed:** localized contamination alone never wins (best 2.30), and lazy self-report alone doesn't either (1.56 at K=4).
- **When the trade is favourable** (argued, not measured, since our testbed is replayable by construction):
  - completed work has **external side effects** that restart cannot undo
  - LLM re-execution is **not idempotent**, so a restart may produce a different, worse run
  - wall-clock time is dominated by **rate limits** rather than tokens

### 8.4 Escalation behaviour

12 recoveries were delivered at selective, 2 at agent restart, 1 at full restart. 4 verification failures occurred in total. The escalation budget was never exhausted and no provider degradation happened. Restart works as a *fallback*, not the primary action.

### 8.5 Scripted chain results (separate mode; do not pool) `[README §5]`

Work preserved vs B1, oracle detector, 30 reps:

| Scenario | CausalLine | B1 | Gain |
|---|---|---|---|
| A influencing | 38.2% ± 3.4 | 21.1% | +17.2 |
| A exposed-only | 90.4% ± 4.9 | 21.1% | **+69.3** |
| B influencing | 57.9% ± 0.0 | 57.9% | **+0.0** |
| B exposed-only | 90.4% ± 3.1 | 57.9% | +32.5 |
| C influencing | 57.9% ± 0.0 | 52.6% | +5.3 |
| C exposed-only | 91.2% ± 3.2 | 52.6% | +38.6 |

The **exposed-only** rows are where the claim pays most directly: the poison was present but unused, and reachability discards all of it for nothing.

---

## 9. Research journey (how the project evolved)

Roughly 70 commits, September–October 2026, with all design decisions logged in `docs/05-decisions.md` (D-001 onward).

1. **Weeks 1–2: thin end-to-end system.** Trace layer, provenance, counterfactual checking on a 4–5 agent Gemini chain. Key early findings:
   - free-tier quota was 20 requests/day, which forced the scripted client and local models (D-017, D-023)
   - text comparison is unusable at 100% noise, so we moved to decision signatures (D-026)
2. **Integration and final push** (`docs/07`, `docs/08`): the full method comparison, detector sweeps, the planner/verifier mismatch fix, and atomic units.
3. **Real-LLM mode** (`docs/09`): generated scenarios from a 240-point structural design space, where *we* choose the causal structure and the model writes the words, plus canary ground truth. It found real failure modes a scripted client can't produce, such as degenerate JSON and models quoting source labels back.
4. **Phase-gated remediation** (`docs/10`, `docs/11`; this branch's name): removability check, carrier laundering fix, byte-level verification, comparator noise floors.
5. **Gate 1 / cost direction** (`docs/gate1/`): a falsification-first search for an ex-ante "investigate vs restart" gate and for cost reductions (§10.2).
6. **Local Llama frontier:** GPU campaign, token economics, the fan-out shape and the first measured break-even win.
7. **56-agent, three-provider testbed:** first a fixed-workload campaign (`docs/13`), then the **workload-varied** campaign (`docs/14`), which is the primary result.
8. **Paper draft and mid-term report.**

---

## 10. Negative results and corrections (kept in the record on purpose)

The supervisor's guidance was to report negative results and corrections rather than remove them. These carry as much weight as the positive results.

### 10.1 Issue #20: a safety defect that looked like a performance win `[docs/12]`

- **Symptom:** the 56-agent large regime first reported CausalLine at **31.7%** vs B2's 14.5%, an apparent **+17.2 pt** win, together with **5 unsafe preservations**.
- **Cause:** a *missing relation*. An event that *retrieves* a source (tool response, memory read, hand-off) stores that source's text in its own output. But the source didn't exist yet when the event was logged, so it was never in the event's exposures, no verdict was ever written, and the walk never considered it. The verdict was not wrong; it was **absent**, and nothing looks for absent verdicts.
- **Fix:** record an **ingestion** edge at every ingestion site. This is a provenance-recording change only; the walk, planner and baselines are untouched.
- **Effect:** the large regime dropped to 14.5% with 0 unsafe. **The entire apparent win was the defect.** In the scripted matrix, 10 CausalLine cells dropped by 4.6–5.3 pts. No baseline number moved. On scripted scenario B (influencing), the gain over B1 went to **zero**.
- **Lessons:**
  1. Work preserved and unsafe preservations must always be read together.
  2. This was found only by *observed* (canary) ground truth. Construction-based ground truth assumes by design that a source doesn't influence the event that produced it.

### 10.2 Gate 1 and the cost objective: falsified `[docs/gate1/]`

Goal: decide *before* spending analysis whether to investigate or just restart.

| Hypothesis | Verdict |
|---|---|
| H1: per-node compromise probability P | **falsified**; the aggregate saturates to 1.000 |
| H2: structural prior | survived scripted tests (97% held-out accuracy, 0 false recoveries), then **failed on the real model** and was broken by a 5-event adversarial counterexample at a hub agent |
| H3: adaptive budget / early peek | **falsified**; early-verdict rate does not estimate f (correlation 0.234) |
| H0: cost-aware SPRT | not falsified, but dominated |

The final decision was **"Outcome B: the original cost-reduction objective is not supported."** One find was that 68% of self-report calls land outside the closure and are *provably* irrelevant (recovery region identical on 290/290 traces). But that saving cannot be taken under the current architecture. Gate 1 is **not** part of the contribution.

### 10.3 Other corrected claims

- **"Savings scale with workflow length":** measured false (A/N 1.23 → 1.24).
- **Checkpoint garbage collection frees nothing,** by precondition: a run that did useful work never has an influence-free prefix.
- **The robustness machinery** (repeats, controls) caught no confounds on this testbed and raised analysis cost 53%.
- **Earlier baseline bug:** on the fan-out shape, B1 and B2 were discarding *nothing*, because documents lacked an origin event. Found because the numbers looked too good.
- **The first 56-agent attempt on `llama3.2:3b`** failed on capability (code transpositions), not on the method. We switched to the 8B model and documented why.

---

## 11. Limitations and threats to validity

1. **Five workloads per regime.** That supports a sign count, not a distributional claim. The small regime's sd of 41.97 shows how bimodal the outcome is (selective succeeds vs escalates).
2. **One topology** for the headline. We say nothing about other hub fan-outs, stage depths or provider mixes.
3. **Oracle detector** in the headline campaign. Detector-imperfection results exist only in the scripted matrix.
4. **A copy task.** Agents move short codes. We chose this so small-model capability failures don't masquerade as recovery failures. It means we have **not** shown recovery on tasks with branching control flow or side-effecting tools.
5. **Propagation rule not proved complete.** It is measured (0 escapes) and checkable at run time, but it has already been incomplete once (issue #20).
6. **All textual instruments.** The `carryover` facet, the removability check and canary ground truth are textual. An influence that is **paraphrased** rather than quoted is invisible to all three.
7. **Coincidental redundancy is not handled** (§5.4; a complexity result, not a TODO).
8. **Irreversible side effects cannot be undone** by any replay; recovery can only avoid repeating them.
9. **Cost is in tokens, not money or latency.** Attribution runs on the same model as the agent, and a cheaper attribution model is untested.
10. **Medium regime misses its intended band** (52–57% vs 25–50%). This is a property of the hub topology; it was reported, not tuned away.
11. **Adaptive attackers** who know the method are out of scope.

---

## 12. Claims we can and cannot make

**Supported by the data:**

- On the 56-agent, three-provider topology under an oracle detector, CausalLine preserved more correct work than the topology closure in **10/10** medium/large runs (+7.0 and +15.2 pts), and lost in the light regime.
- **Zero unsafe preservations and zero escapes** across all 15 real runs and all 24 scripted CausalLine cells, including under a blind detector and a lying self-reporter.
- The advantage grows with the contaminated fraction, which is the direction the method predicts.
- Verification plus escalation is necessary: 3 of 15 runs escalated, and all delivered correctly.
- Attribution overhead is large, and at low contamination it erases the benefit.

**Not supported, and must not be said:**

- "significant", or any p-value, or how *often* the gain appears in general
- that CausalLine is cheaper than restart, or a cost optimisation
- that it generalises to other topologies, harder tasks, real detectors or mostly-frontier fleets
- that false cleans are eliminated (the paraphrase blind spot remains)
- the number 31.7% (the pre-fix defect figure)
- anything about detection quality

---

## 13. Reproducibility

```bash
# offline, no API key; source of every scripted number
pip install pytest
python -m pytest tests/
python -m src.eval.experiment            # full method comparison, ~1 min
python -m src.eval.campaign --reps 30    # 96 cells × 30 reps (~9 min)
python -m src.eval.economics             # cost model and break-even figures

# 56-agent campaign (needs Ollama + GEMINI_API_KEY + NVIDIA_API_KEY_1)
python -m src.eval.mixed_validation --seeds 5 --vary-placement
python -m src.eval.mixed_validation_report --dir data/results/mixed56-workload-varied --planned 15
python minor_report/diagrams/make_figures.py
```

- Campaigns write one JSON per run, are resumable, and skip completed runs.
- Each 56-agent run takes 395–793 s of wall clock. All 15 completed on the first attempt.
- Figures are generated from an *executed* trace (`topology.json`) and from committed results, not drawn by hand.
- `python -m src.common.nvidia --doctor` diagnoses an API setup without printing keys.

---

## 14. Where we would value guidance (suggested agenda for the meeting)

These are the open decisions where an outside view would help most:

1. **Is the contribution framing right?** Given that exposure≠influence and counterfactual attribution are 2026 prior work, is a "verified, detector-triggered recovery procedure plus honest evaluation" a strong enough systems/security contribution? Which venue type fits: a security workshop, a systems venue, or an AI-safety venue?
2. **Statistical design.** How many workloads per regime would a reviewer accept, and which test fits a paired, bimodal outcome (e.g. a sign test, or a bootstrap over placements)? Should we pre-register the next campaign formally?
3. **The economics problem.** The method costs 1.8–2.5× a restart. Should the paper lean on the *non-replayable side effects* argument? If so, how do we build a testbed with genuinely irreversible actions so the argument is measured rather than argued?
4. **Task realism.** What is the smallest step up from the copy task (branching control flow, side-effecting tools) that would be convincing, without small-model capability failures swamping the signal?
5. **Completeness of propagation.** Is there a principled way to argue, or adversarially search for, a missing fourth relation, beyond the one defect we found by accident?
6. **Beyond textual instruments.** Ideas for detecting *paraphrased* influence without circularity against the ground truth.
7. **Real detector.** Which existing detector should occupy the socket for the headline campaign, so recovery is measured under realistic false positives and negatives?

**Our planned next steps** (`minor_report §VI`), in the order we judge most useful:

1. a task with branching control flow and side-effecting tools
2. a larger campaign varying hub fan-out and stage depth
3. an adversarial search for a missing propagation relation
4. a real detector in place of the oracle
5. cutting attribution cost (lazy self-report; skipping provably out-of-closure calls)
6. a conference submission, for which items 1–2 are prerequisites

---

## 15. Anticipated questions, with short answers

**Q: Isn't this just taint tracking?**
Classical taint tracking propagates along reachability, which is our B2 baseline. In LLM agents data dependence is invisible, so we *establish* each edge by counterfactual evidence and propagate only along influence, derivation and ingestion. The gap between that and reachability is the measured result.

**Q: How do you know a counterfactual "no change" isn't just luck?**
We compare decision facets whose measured noise floor on identical re-sends is 0%. Facets with a non-zero floor are excluded by a pre-registered rule. We also check removability, so the information really is gone. Anything not verified stays contaminated.

**Q: What if the model lies in self-report?**
A positive claim only enlarges the recovery set, which is safe. A negative claim is never trusted without a counterfactual. Under an adversarial self-reporter the scripted matrix still showed 0 unsafe preservations.

**Q: What if the detector is wrong?**
The detector is a socket, tested with oracle, heuristic, transformer, pessimistic and blind occupants. With a blind detector, verification fails and the ladder escalates to restart, so unsafe preservation stays at 0%, against 50% for the baselines.

**Q: Why not just restart? It's cheaper.**
For replayable, token-priced work, it is, and we say so. The case for CausalLine is work with side effects, non-idempotent re-execution, or rate-limited wall-clock time. We have not yet *measured* that case.

**Q: Why is LLM non-determinism not a problem for replay?**
Replay never relies on the model reproducing earlier output. Non-invalidated events are spliced from logged bytes, which is asserted byte-exact. Only invalidated events are re-invoked, and the result is then verified.

**Q: Isn't the 56-agent workflow artificial?**
Yes, deliberately. A broadcast hub is a common real pattern and it is where reachability fails worst. The copy task keeps capability failures from masquerading as recovery failures. Generalising to harder tasks is future work item 1.

**Q: Why isn't online recovery supported?**
Proving *non*-influence needs a counterfactual of an output that exists. Before an event completes, there is nothing to compare. An online variant would be a different method.

**Q: Is the recovery set minimal?**
No, and we don't claim it is. Minimum-cost cover is NP-hard. We use greedy set cover (H(n)-approximate) and claim "small and safe, measured".

---

## Appendix A: Key decisions referenced (from `docs/05-decisions.md`)

| ID | Decision |
|---|---|
| D-002 | Say "small safe recovery set", never "minimum" |
| D-012 | Work is counted in events, never sources |
| D-017/023 | Free-tier quota too small, so scripted client plus local models |
| D-024 | `check` records distinguish "checked clean" from "never checked" |
| D-026 | Text comparison unusable (100% noise floor), so decision signatures |
| D-028 | Content-addressed store; checkpoints carry refs, not text |
| D-032 | Attribution runs after detection, on the flagged region only |
| D-034 | Detection is a socket; the perfect detector is named "Oracle" |
| D-066 | Removability check before trusting leave-one-out |
| D-067 | Carrier records are pointers to upstream verdicts, never clearances of their own |
| D-068 | Verification reads the bytes actually sent |
| D-074 | Post-hoc, batch scope; the detector reports sources, not agents |
| D-082 | Lazy (deferred) self-report |
| D-090 | Small local models fail on capability, so use an 8B model and a copy task |

## Appendix B: Housekeeping before the meeting

Inconsistencies in the repo that a careful reader may notice. Better to fix them or mention them first:

- **The paper draft (`paper/causalline.tex`) abstract still quotes older figures:**
  - **85.5% vs 72.4%** and **1.6–2.4×**, which come from the fixed-workload `docs/13` campaign
  - the mid-term report and `docs/14` use **91.4% vs 76.3%** and **1.8–2.5×**

  Use the `docs/14` / mid-term numbers.
- **The test count differs between documents:** the README says 327 passed / 7 skipped (an older count), and the mid-term report says 493 passed / 1 skipped. Quote the latest figure after re-running `pytest`.
- **The fixed-workload campaign** (`docs/13`: +5.93 mean, 13/15 wins) is the *control* for model non-determinism, not the headline. Its seed did not vary the workload (medium sd = 0.00), which is what `docs/14` fixed.
- **The README's headline table (§5) is the scripted mode;** the mid-term report's headline is the 56-agent real mode. Keep them in separate tables.
