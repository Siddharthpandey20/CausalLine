# IREC: the decision

Date: 2026-11-05. Branch `feat/irec`. Reads on
[`IREC_LIVE_VALIDATION.md`](IREC_LIVE_VALIDATION.md),
[`IREC_COST_ACCOUNTING.md`](IREC_COST_ACCOUNTING.md) and
[`IREC_SAFETY_AND_NOVELTY.md`](IREC_SAFETY_AND_NOVELTY.md).

---

## 1. Verdict

# CONDITIONAL GO

**Integrate as an independent strategy behind a flag. Do not make it the
default yet.** It is integrated that way already: `src/recovery/irec.py` on
`feat/irec`, `recovery_strategies=("causalline",)` by default, 12 regression
tests, nothing in `src/` importing it at module level. 641 passed, 1 skipped.

**Why GO and not NO-GO.** Every correctness and safety criterion was met on
every one of the 15 live placements, the minimum economic requirement was met
or tied in every run, and the cost reduction against current CausalLine is
**0.216×** — a 78% reduction that comes entirely from deleting a stage the
evidence shows was not merely redundant but *wrong* on at least one pair.

**Why CONDITIONAL and not GO.** Three things are not yet established, and one
of them is the assumption the whole cost argument rests on (§5).

---

## 2. Did the live results validate the offline findings?

Yes, closely, with one correction and one thing the offline work could not see.

| quantity | offline prediction | live measurement |
|---|---|---|
| small, R | 0.76 (0.73–0.80) | **0.76** |
| medium, R | 0.40 pooled; 0.58–0.59 landed | **0.63** landed |
| large, R | 0.95 (0.88–1.00) | **0.95** |
| pooled, 15 primary | 0.71 | **0.838** over the 13 landed |
| unsafe preservations | 0 | **0 / 15** |
| recall | 100% | **100% / 15** |
| scripted, R ≤ 1 | 24/24 | 24/24 (re-measured with defect A fixed) |

The offline pooled 0.71 was optimistic because it pooled the two non-landed
placements, whose R of 0.06–0.08 pulls the mean down; separating them, as
`CLAUDE.md` requires, gives 0.838. The per-regime predictions were accurate.

**Corrected:** the offline report feared "verification-failure economics in
small-closure regimes", noting 3 of 5 small runs where the real system's
selective replay failed the task check, and projected IREC at 2.64× unverified
B2 if charged the same ladder. **That did not happen.** IREC's own task check
passed on all 15 placements — including the 13 whose *original* run failed it —
so verification passed 15/15 and IREC escalated **zero** times, against
CausalLine's 3 escalations on the same 15 runs. The feared 2.64× never
materialised and the ladder was never entered.

**Not visible offline:** the two mechanisms are separable, and the scripted
matrix exercises only one of them. `unchanged` is **0 in all 24 scripted
cells** — a deterministic client given a changed prompt always returns a changed
output — so every scripted saving is the prompt-equality splice, and the
output-equality cutoff has no scripted evidence at all. Live, the cutoff fires
at 60–64% in medium and **100%** on the non-landed runs. Any future claim about
the cutoff must cite real-model evidence.

---

## 3. Acceptance criteria

| required | result |
|---|---|
| 100% recall on exact-ground-truth contamination | **met.** 0 unsafe on all 24 scripted cells (exact per-pair truth) and all 6 scenario cells in `tests/test_irec.py`; recall 100% on 15/15 live |
| zero unsafe preservations | **met.** 0 in all 15 live placements and all 24 scripted cells |
| zero contamination escapes | **met.** 0 escapes; 0 D-068 byte re-check failures; 0 leftover taint after verification |
| no unacceptable degradation in task success | **met, and improved.** 15/15, including the 13 runs whose original failed the task check. CausalLine escalated 3 times on the same runs; IREC never did |
| recovery correctness maintained | **met.** production `verify()` PASS 15/15, driven by IREC's own splice/replay bookkeeping |

No criterion was weakened to obtain this. One apparent failure — verification
failing on all three pilot runs — was traced to the **analysis harness** using
a plain `contaminate()` walk instead of `_post_recovery_region`, the D-024
defect reintroduced by recovery. The recovered traces were correct; the first
measurement of them was not.

---

## 4. Is the economic target reached?

**Minimum requirement — "IREC costs less than both B0 and B2": met or tied
everywhere, strictly met in 13 of 15.**

- cheaper than B0 in **15 / 15**
- cheaper than B2 in **13 / 15**, exactly **equal** in 2 / 15
- **never more expensive than either, in any run**

The two ties are `20260917/large` and `20260918/large`, where the contamination
reaches **100%** of the closure's model events: there is nothing to splice and
IREC necessarily re-executes what B2 re-executes. That is the bound being tight,
not a regression.

**Preferred target — ≤ 0.8× the cheaper baseline: met in 2 of 3 regimes.**

| regime | R | target |
|---|---:|---|
| small | 0.76 | **met** |
| medium | 0.63 | **met** |
| large | **0.95** | **missed** |

Per placement, 7 of the 13 landed runs met 0.8× (small 4/5, medium 3/3,
large 0/5).

**Large's miss is a property of the workload and is established, not
asserted.** Any sound black-box method must query the model once for every
event whose prompt changed, `D` (Theorem 2). `|D|` as a share of B2's model
work is therefore the floor:

| regime | \|D\| / closure model events | floor | measured |
|---|---:|---:|---:|
| small | 77.0% | ~0.77 | 0.76 |
| medium | 53.8% | ~0.54 | 0.63 |
| large | **94.6%** | **~0.95** | **0.95** |

**The attainable limit in large is ~0.95, and IREC is at it.** 0.8× is
unreachable there for any sound black-box method under exact output semantics.
It is reported per regime; the pooled 0.838 does not hide it.

**Against current CausalLine, which is the question that matters for this
project:** IREC is **0.216×** — 67,638 tokens against 312,706 over the same 13
landed placements. CausalLine is 3.88× the cheaper baseline; IREC is 0.84×.

---

## 5. Is integration justified, and what are the conditions?

Integration as a flagged strategy: **yes, done.** Promotion to default:
**not yet**, on three conditions.

**C1 — replicate A3 on a provider with documented deterministic decoding.**
The cutoff mechanism's yield is capped by the provider's ability to reproduce
its own output, measured at **96.7% (87/90)**. But **82 of those 90
observations are one local 8B model on CPU**; Gemini contributed 2 and NVIDIA 6.
All three failures were the local model. The figure is sound for *this* machine
and must not be quoted as a property of hosted models. Until it is replicated,
the cost argument for the cutoff half of IREC rests on a thin sample.

**C2 — run the typed-interface ablation.** Asked for in
`FINAL_COST_OPTIMIZATION_RESEARCH.md` §9.3 item 3 and still unrun: a variant of
the scripted chain passing findings as parsed fields, to demonstrate the
projection-cutoff mechanism on exact ground truth. Without it, the mechanism's
only evidence is real-model and therefore non-deterministic.

**C3 — settle the work-preserved direction.** IREC preserves **more** work than
CausalLine on all three live regimes (92.1 / 88.3 / 35.9% against 72.6 / 44.6 /
23.7%) and **less** on the scripted matrix (61.9% against 68.3%). Both are
measured. The explanation — byte-equality is stricter than accurate attribution,
but real-model noise and escalation cost CausalLine more than strictness costs
IREC — is coherent but is an *interpretation*, and a default change should not
rest on one. The small-regime gap in particular is mostly CausalLine's single
`restart_all`.

None of these is a safety condition. Safety is settled: the argument needs no
comparator, and the one measured attempt to relax exact equality produced 1 and
4 unsafe preservations.

### What happens to the existing modules if IREC is ever promoted

| module | fate |
|---|---|
| self-report | **removed from the recovery path.** It is the single largest cost and IREC does not use it. It stays available as a research instrument — it is what makes `(S3, e0010)` come out right under `hybrid`, which is itself a finding about the counterfactual |
| counterfactual attribution | **removed from the recovery path.** Measured wrong on `(S3, e0010)`, where it records a false clean against exact ground truth |
| provenance / dependency tracking | **kept and load-bearing.** IREC needs the recorded prompts, exposures, ingestion edges and the event graph to splice at all |
| recovery planning | **replaced.** There is no plan; dependency order plus the two rules is the plan |
| selective replay | **kept.** IREC is a replay client; `replay()`'s splice machinery and `ReplayReport` are reused unchanged |
| verification | **kept, unchanged**, including the strict task check and the D-068 re-check. IREC supplies the splice/replay sets it needs |
| escalation | **kept as the fallback.** Never entered in 15 live runs, but a fallback that is never free is still a fallback |

**Nothing is deleted on this branch.** CausalLine remains the default strategy
and the comparison baseline, exactly as the mission requires.

---

## 6. Remaining limitations

1. **A3's sample is narrow** (C1): 90 observations, 82 from one local 8B model.
2. **The typed-interface ablation is unrun** (C2).
3. **The work-preserved direction flips between evaluation modes** (C3) and the
   explanation is an interpretation.
4. **Zero cutoff yield in the small regime.** All 5 small placements replaced
   every re-executed event; the cutoff earns nothing there and the savings are
   entirely the splice.
5. **n = 5, 3, 5 per regime.** One placement per seed per regime.
6. **Two placements never landed**, so 13 runs carry safety evidence, not 15.
7. **`run_python` is not replay-safe.** This workload never calls it (`mixed56`
   uses only the read-only `fetch`), and tool sandboxing is out of scope per
   `docs/01-scope.md`. A workflow with irreversible external effects needs a
   suppression layer first.
8. **A1 and A5 are measured on this pipeline only.** 165/165 prompts reproduced
   here; a provider with server-side conversation state or an output-changing
   cache would violate A1, and a tool with unrecorded external reads would
   violate A5.
9. **The bound is narrower than it sounds.** It is a minimum over *sound
   black-box* methods under *exact output semantics*, instance-wise on the run
   that happened, counting queries rather than tokens, and it says nothing about
   work preserved. A sound *projection* can legitimately beat it. See
   `IREC_SAFETY_AND_NOVELTY.md` §6.
10. **Novelty is moderate.** The execution mechanism is prior art (Retro, build
    systems, agent-rewind). What is defensible is the reduction, the bound, the
    negative comparator evidence and the economic characterisation — not the
    mechanism.
11. **Scripted and real-model numbers may not be pooled** (`CLAUDE.md`), so the
    24-cell and 15-placement results stay in separate tables throughout.

---

## 7. What was not done, and why

- **No full re-run of the original 56-agent pipeline.** IREC replays against
  the saved traces, which is what kept the campaign to 77% of its budget. The
  original runs are the published ones.
- **No campaign re-run for CausalLine.** Its escalated-run costs were recovered
  from the escalation traces already on disk (`IREC_COST_ACCOUNTING.md` §3)
  rather than by spending tokens again.
- **No tuning of anything to make a target.** The large regime misses 0.8× and
  is reported as missing, with the floor established.
- **No new optimization hypothesis.** The mission forbade it and none was
  introduced.
- **No production default changed.**

Budget: **92,840 tokens of a ~120,000 constraint (77%)**, 464 model calls,
53 minutes of wall clock for the 15 placements. One probe worth 2,208 tokens was
invalid and is counted anyway.
