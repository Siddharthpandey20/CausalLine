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

Pending. Reported here when complete.

---

## 3. C3 — the work-preserved discrepancy

Pending. Reported here when complete.
