# The three conditions on promoting IREC to default

Date: 2026-11-05. Branch `feat/irec`. IREC remains **off by default** and
**unmerged** throughout; nothing in this document changes a production default.

[`IREC_FINAL_DECISION.md`](IREC_FINAL_DECISION.md) §5 attached three conditions
to promoting IREC from a flagged strategy to the default. This document
addresses them one at a time, with the evidence for each, and keeps measured
results separate from explanations.

| condition | status |
|---|---|
| **C1** replicate A3 under documented deterministic decoding | **§1 — met, and the residual is characterised** |
| **C2** typed-interface ablation | §2 — pending |
| **C3** settle the work-preserved discrepancy | §3 — pending |

---

## 1. C1 — A3 replication under deterministic decoding

### 1.1 What needed replicating, and why a re-run would have measured nothing

The first A3 estimate was 96.7% (87/90), of which 79/82 came from the local 8B
model. The obvious next step — "re-run it with determinism switched on" —
measures nothing, because **it was already on**:
`src/common/local_llama.py` sends `seed=0`, `temperature=0.0` and
`num_ctx=4096` on every request, which is Ollama's documented reproducible
configuration. The three failures were therefore not a missing seed.

So C1 had to do three things instead: use the two providers that decode
greedily, preserve the production prompt assembly exactly, and **separate model
nondeterminism from prompt assembly and from harness error**.

### 1.2 Protocol

**Capture is free.** In gate mode the IREC client receives every
`(prompt, system)` pair the pipeline composes and splices all of them, so the
pipeline runs to completion with **zero model calls**. That gives the exact
production prompts, which is what an earlier standalone probe got wrong: it
re-issued stored prompts *without* the system prompt and reported 0/12
reproductions. Every one of the 55 captured events has a system prompt (2
distinct ones), so that probe was comparing a bare-prompt reply against a
system-prompted one.

| | |
|---|---|
| captured | 55 prompts from `20260917/small`, **0 calls, 0 tokens** |
| re-issued | 19 events × 5 repeats = **95 calls**, byte-for-byte, with their system prompt |
| sampling | spread over agent *role* and provider, not over position — the first twelve local events are all `acq*` with near-identical prompts |
| forced into the sample | `e0129` (the one previous failure), plus both Gemini and three NVIDIA events |
| recorded per call | prompt SHA-256, system-prompt SHA-256, output SHA-256, match against the original, tokens, latency |

Runtime fingerprint, recorded with the result:

| | |
|---|---|
| local | Ollama **0.17.7**, `llama3:latest` 8B Q4_0, CPU only, `temperature=0.0`, **`seed=0`**, `num_ctx=4096`, `num_predict=512` |
| Gemini | `gemini-3.8-flash`, `temperature=0.0` |
| NVIDIA | `nvidia/nemotron-3.5-lightning-30b-a3b`, `temperature=0.0` |
| host | Windows-11-10.0.26300, Python 3.13.1 |

Neither hosted provider documents *bitwise* determinism; both were run greedy,
and that is stated rather than assumed.

### 1.3 Results

**Self-consistency — the same prompt re-issued five times:**

> **18 of 19 events returned five byte-identical replies.** One event, `e0129`,
> returned two distinct outputs.

**A3 — reply matches what the original run recorded:**

| provider | calls | matched | rate |
|---|---:|---:|---:|
| **gemini-3.8-flash** | 10 | 10 | **100.0%** |
| **NVIDIA nemotron** | 20 | 20 | **100.0%** |
| local llama3 8B (CPU) | 65 | 61 | 93.8% |
| **total** | **95** | **91** | **95.8%** |

**Attribution of every mismatch:** all four are `e0129`, and in every one the
prompt hash and the system-prompt hash are identical to the captured original
by construction, because a re-issue sends the captured bytes verbatim.

| cause | mismatches |
|---|---:|
| prompt assembly (an upstream output propagated) | **0** |
| harness error (missing or wrong system prompt) | **0** |
| **model / runtime** | **4** |

### 1.4 The residual, characterised

`e0129` is agent `rev3`, a 121-character prompt with a 416-character system
prompt, whose recorded output is `'[S62]\nDoverly HN391'` (19 chars).

It has exactly **two** outputs, both with stable hashes: the 19-character
original and a 20-character variant. Which one appears depends on the runtime's
state, and that is demonstrable rather than inferred:

| condition | result |
|---|---|
| `e0129` six times **back to back** | call 1 → 19 chars (the original); calls 2–6 → 20 chars, all byte-identical to each other |
| `e0129` **alternated** with a different prompt, four pairs | **19 chars every time — 4/4 match the original** |

Same bytes, same system prompt, `seed=0`, `temperature=0`, same model digest,
same process. So:

- it is **not sampling** — the seed and temperature are fixed and the two
  outputs are each perfectly stable within a condition;
- it is **not prompt assembly** — the prompt and system hashes are identical;
- it is **runtime state**. The two candidate mechanisms are prompt/KV-cache
  reuse on a repeated prompt and batch-dependent floating-point reduction order
  on CPU. Distinguishing them needs instrumentation inside Ollama and is not
  attempted here, so the finding is stated as "runtime/cache state" and no
  further.

**This has a consequence for the measurement protocol itself.** Re-issuing one
prompt N times in a row is the condition that *creates* the variance: it is
exactly the back-to-back case. IREC never does that — each event is issued once,
in pipeline order, preceded by a different prompt. So the 93.8% local figure is
a lower bound produced by the probe's own protocol, and the realistic figure is
the one from the full-pipeline A3 probes (each prompt once, in order): 54/55 and
33/35.

### 1.5 Verdict on C1

**Met.** Under greedy decoding on the two hosted providers, reproduction is
**30/30 = 100%**. On the local 8B model it is 93.8–98.2% depending on protocol,
and **all** of the variance is one event with two stable outputs selected by
runtime cache state — not by sampling, and not by anything in the harness or the
prompt assembly.

What this does and does not license:

- **Does:** the early-cutoff rule is physically able to fire on these providers.
  A3 is no longer an assumption, and no mismatch anywhere was attributable to
  prompt assembly or harness error.
- **Does not:** claim bitwise determinism from any provider. Neither hosted
  provider guarantees it; 30/30 is an observation on 30 calls, not a contract.
  And a non-reproducing event still costs IREC unnecessary downstream replay —
  it just cannot cost safety, because a differing reply is treated as changed.

**Cost of C1:** 95 re-issue calls + 16 calls for the cache-state test = **111
calls, 21,480 tokens** (capture was free). Running total for the IREC line:
**114,320 tokens**.

---

## 2. C2 — typed-interface ablation

`FINAL_COST_OPTIMIZATION_RESEARCH.md` §9.3 item 3 asked for "a typed-interface
variant of the scripted chain (Researcher findings passed as parsed fields), to
show mechanism (b) on exact ground truth". **The ablation was run and it does
not show mechanism (b).** It shows something else, which §2.6 states.

### 2.1 Design

One thing changes between the arms: what a producer hands a consumer at the
`researcher -> coder` boundary.

| arm | the consumer receives |
|---|---|
| **free** (the ablated version, and the shipped interface) | the producer's reply **verbatim**, including its trailing `[ref <hash>]` provenance suffix |
| **typed** (full IREC with a typed interface) | the finding's **content field only**; `[ref <hash>]` is dropped, because it is a pointer to what the producer consulted and not part of what the consumer reads |

Held identical across arms: scenario, variant, workflow, seed (`20260906`),
pricing model, contamination placement, detector (`oracle`), the model
(`ScriptedClient`, same seed), the complete production prompt assembly, and
**IREC's recovery logic byte-for-byte**. The typed interface is a property of
the *workload*, implemented as `handoff_projection` on `run_pipeline` (default
`None`), so `src/recovery/irec.py` is the same code in both arms.

The projection is applied **only to model-produced findings**. A planted
inter-agent message is forwarded untouched, so the ablation manipulates the
interface between honest agents and does not also alter the attack. That is a
deliberate choice and it bounds the result: a real typed interface would also
constrain what an attacker can inject, and that is not modelled here.

**The two arms produce different original traces by construction** — changing
the interface changes the consumer's prompt. So absolute token counts across
arms are confounded by the trace difference, and the comparable quantities are
each arm's `R` against its *own* baselines, plus the mechanism counts.

24 cells: 3 scenarios × 2 variants × 2 workflows × 2 pricing models, each arm.

### 2.2 The manipulation landed

| arm | handed-on `agent_message` sources carrying a `[ref ...]` suffix |
|---|---|
| free | **96 / 164** |
| typed | **0 / 164** |

### 2.3 Safety and correctness: identical

| arm | cells | unsafe preservations | recall | contamination escapes | task success | verification |
|---|---:|---:|---:|---:|---:|---:|
| free | 24 | **0 / 38** | **38 / 38** | **0 / 38** | **24 / 24** | **24 / 24** |
| typed | 24 | **0 / 38** | **38 / 38** | **0 / 38** | **24 / 24** | **24 / 24** |

**Removing the typed interface changes nothing measurable about safety or
correctness.** No cell differed on any of these five quantities, and there were
**0 control-flow divergences** in either arm.

### 2.4 Mechanism and cost

| arm | spliced | re-executed | **cutoff kept** | changed | IREC tokens | mean `R` | work preserved |
|---|---:|---:|---:|---:|---:|---:|---:|
| free | 88 | 92 | **0** | 92 | 26,698 | 0.784 | 460 / 552 |
| typed | **98** | **82** | **0** | 82 | **23,637** | **0.716** | **470 / 552** |
| difference | **+10** | **−10** | **0** | −10 | **−3,061** | **−0.068** | **+10** |

### 2.5 Where the effect actually lives — decomposed per cell

14 of 24 cells differed, in two quite different ways:

| effect | cells | what it is |
|---|---:|---|
| **structural** — fewer re-executions | **4** | `A/exposed_only` in all four workflow × pricing combinations. +2 or +3 events spliced, `R` **1.00 → 0.60–0.67**, work preserved +2 or +3 events |
| **incidental** — shorter prompt only | 10 | −22 to −38 tokens per cell, `0` change in spliced or re-executed. Present **only** under proportional pricing and **exactly zero** under flat pricing, which identifies it as prompt length, not mechanism |
| none | 10 | the flat-priced non-`A` cells |

The four structural cells, in full:

| cell | true contaminated events | spliced | re-executed | `R` | work preserved |
|---|---:|---|---|---|---|
| A/exposed_only/short/flat | **0** | 1 → **3** | 5 → **3** | 1.00 → **0.60** | 14 → **16** / 19 |
| A/exposed_only/long/flat | **0** | 1 → **4** | 8 → **5** | 1.00 → **0.62** | 19 → **22** / 27 |
| A/exposed_only/short/proportional | **0** | 1 → **3** | 5 → **3** | 1.00 → **0.63** | 14 → **16** / 19 |
| A/exposed_only/long/proportional | **0** | 1 → **4** | 8 → **5** | 1.00 → **0.67** | 19 → **22** / 27 |

**All four structural cells have zero true contamination.** Banded by
contaminated fraction (the scripted chain has no small/medium/large regimes —
those belong to the 56-agent workload, which has no typed handoff to ablate):

| band | arm | cells | spliced | re-exec | cutoff | IREC tokens | mean `R` | unsafe |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| small (<20%) | free | 22 | 86 | 82 | 0 | 24,052 | 0.765 | 0 / 30 |
| small (<20%) | typed | 22 | **96** | **72** | 0 | **21,014** | **0.691** | 0 / 30 |
| medium (20–40%) | free | 2 | 2 | 10 | 0 | 2,646 | 1.000 | 0 / 8 |
| medium (20–40%) | typed | 2 | 2 | 10 | 0 | 2,623 | 0.995 | 0 / 8 |

There is no large band: the scripted chain's contamination never reaches 40% of
its events.

### 2.6 What the typed interface contributes — and what it does not

**Measured.** The free-text interface forwards the producer's reply including
`[ref <hash>]`, a pointer to what the producer consulted. That pointer moves
whenever the consulted content moves — **even when the substantive finding does
not**. A pre-check isolated this: of the events IREC re-executed, exactly one
per scenario-`A` run differed *only* in the ref suffix (`e0009`, body
byte-identical, `[ref cd0a31b6]` → `[ref 4fec347b]`); scenarios B and C had
**zero** such events.

**Interpretation, stated as such.** Forwarding the pointer verbatim makes a
provenance change indistinguishable from a content change, so the consumer's
prompt differs and IREC re-executes it. Projecting the pointer out makes the
consumer's prompt identical, and IREC splices it instead. That is the whole of
the contribution observed here.

**Three limits on what this result supports:**

1. **It does not demonstrate mechanism (b).** `cutoff kept` is **0 in every one
   of the 24 cells in both arms**. The typed interface did not enable a single
   output-equality cutoff. Its entire benefit came through rule 1 — *prompt*
   byte-equality — not rule 2. The ablation therefore **fails** the goal §9.3
   item 3 set for it, and that is reported rather than relabelled. It also
   reinforces the separate finding that the scripted matrix cannot exercise the
   cutoff at all.
2. **The structural benefit was observed only where nothing was contaminated**
   (4/4 cells, `truth_events = 0`). This is the exposure-without-influence case,
   which is the project's core claim and therefore not a trivial case — but it
   is **not** evidence that a typed interface reduces the cost of recovering
   from real contamination. In the ten cells with genuine contamination the
   structural effect was exactly zero.
3. **The 10 incidental cells should not be credited to the interface.** Deleting
   ~15 characters from a prompt lowers a proportionally-priced token count; any
   shortening would do the same. That it vanishes under flat pricing is the
   proof.

### 2.7 Verdict on C2: retain as a workload property, do not build it into IREC

- **Does removing it change safety or correctness?** **No.** Identical on
  unsafe preservations, recall, escapes, task success and verification, across
  all 24 cells.
- **Does it change cost or work preserved?** **Yes, narrowly.** −3,061 tokens
  and +10 preserved events over 24 cells, of which the structural part is
  confined to 4 cells with zero contamination, and the rest is prompt length.
- **Retain, simplify, or remove?** **Retain as an optional workload property;
  do not make it part of IREC and do not claim it as a contribution of the
  method.** It is a recommendation about how agents should hand work to each
  other — do not forward a provenance pointer the consumer does not read —
  and it lives as `handoff_projection`, default off, which is where it belongs.

C2 is therefore **met in the sense that the ablation was run and is
controlled**, and its answer is **narrower than the hypothesis it was meant to
support**. It removes a claim rather than adding one, which is the correct
outcome when the evidence does not support the claim.

**Cost of C2:** zero. Scripted, deterministic, no model calls, no key.

---

## 3. C3 — the work-preserved discrepancy

Pending. Reported here when complete.
