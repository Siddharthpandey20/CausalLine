# IREC cost accounting: the two measurement defects, corrected

Date: 2026-11-05. Companion to [`IREC_LIVE_VALIDATION.md`](IREC_LIVE_VALIDATION.md),
[`IREC_SAFETY_AND_NOVELTY.md`](IREC_SAFETY_AND_NOVELTY.md) and
[`IREC_FINAL_DECISION.md`](IREC_FINAL_DECISION.md).

`FINAL_COST_OPTIMIZATION_RESEARCH.md` §2.4 reported two defects that make the
method comparison unfair, and said its own CausalLine columns were therefore
"favourable to CausalLine". Both are now diagnosed against execution traces,
fixed, and the size of each is measured. **No recovery semantics changed**: in
both cases the fix is to what the harness records or hands over, not to what the
ladder decides. `623 passed, 1 skipped` before and after.

---

## 1. What a recovery costs

Every figure in these four documents is the sum of:

| component | charged? | note |
|---|---|---|
| initial replay | yes | |
| every **subsequent** replay attempt | yes | defect B; was omitted |
| verification | yes, and it is **0 tokens** | `verify()` is a taint walk, a byte re-check, a memory-reference check and a string task check. No model call. §4 |
| failed recovery attempts | yes | defect B |
| escalation (`agent_restart`, `restart_all`) | yes | defect B |
| full B2 fallback after an IREC attempt | yes | §5; nothing has needed it yet |
| tokens spent before a fallback | yes | fallback is never free |
| the original run `N` | **no**, for any method | every method pays it identically, so it cancels. Reported alongside for scale |
| attribution / analysis | yes, where a method has one | CausalLine only; IREC has none |

Costs are **tokens**, counted from each provider's own usage record, plus model
calls and wall clock reported separately. Two pricing models appear in scripted
results because the repository supports both: `flat` (every call one unit) and
`proportional` (tokens scale with prompt size). Real-model figures are actual
provider-reported tokens.

---

## 2. Defect A — long-workflow baselines replayed on the wrong fixtures

### Diagnosis

`src/eval/experiment.py:run_cell` called:

```python
result, report = run_baseline_recovery(
    method, discard, original, replay_client, flagged, attack, out
)
```

with no `tools=`. `run_baseline_recovery` then defaults to
`_fresh_tools(attack, out)` — the **short** fixture corpus. CausalLine, twenty
lines below, is given `_fresh_tools(attack, out, extended=(workflow == "long"))`.

So in a long workflow the baselines replayed against a tool corpus that is
missing the material the second research round needs, and CausalLine did not.
The two were not scored on the same world.

### Fix

The same expression, passed to the baselines. `replay()` is called exactly as
before.

### What it was worth

Measured by running each baseline on both fixture sets, same trace, same seed
(3 scenarios × 2 variants × 3 baselines, long workflow):

| | short fixtures (the defect) | shared fixtures (correct) |
|---|---:|---:|
| **baseline task success** | **6 / 18** | **18 / 18** |
| B2 replay tokens, `flat` | 12,000 | 12,000 (unchanged) |
| B2 replay tokens, `proportional` | 19,652 | **21,668  (+10.3%)** |

Per cell, `proportional`:

| cell | B2 short | B2 shared | delta |
|---|---:|---:|---:|
| A/influencing | 4,217 | 4,490 | +273 |
| A/exposed_only | 4,183 | 4,490 | +307 |
| B/influencing | 1,533 | 1,414 | −119 |
| B/exposed_only | 1,471 | 1,384 | −87 |
| C/influencing | 4,104 | 4,945 | +841 |
| C/exposed_only | 4,144 | 4,945 | +801 |

**The important half of this is not the tokens, it is the task column.** The
claim that "B2 fails the task in long cells" was an artifact of the fixture
mismatch and nothing else. Corrected, every baseline passes every long cell.
Any statement anywhere that a baseline is less *correct* than CausalLine on a
long workflow has to be re-derived; the token effect is a 10.3% understatement
of B2's cost, which was flattering to both CausalLine and IREC.

---

## 3. Defect B — escalated runs charged for one replay out of three

### Diagnosis

`src/recovery/causalline.py` ended the escalation loop with

```python
replay_tokens = last_report.replay_tokens if last_report else 0
```

`last_report` is rebound on each pass, so a run that replayed at `selective`,
failed verification, replayed at `agent_restart`, failed again, and replayed at
`restart_all` reported **only the third replay**. The runs whose cost was most
understated were therefore the most expensive ones in the corpus, and they were
compared against baselines that never escalate at all.

### Fix

An accumulator over every attempt, plus a per-attempt breakdown written into the
run's notes so the total is auditable:

```
replay cost by attempt: selective=500, agent_restart=500, restart_all=600
  (total 1600; the final attempt alone was 600)
```

`replay_tokens` is written once and read only by the returned record — no scope,
plan or verdict depends on it — so this changes no recovery decision.

### What it was worth, scripted matrix

| cell | delivered scope | reported before | true | understated by |
|---|---|---:|---:|---:|
| A/influencing/pessimistic | exhausted | 600 | **1,600** | ×2.67 |
| C/influencing/pessimistic | exhausted | 600 | **1,700** | ×2.83 |
| A,B,C/influencing/blind | exhausted | 600 | 600 | — (earlier attempts made no model call) |
| */influencing/oracle | selective | 200–400 | same | — (no escalation) |

### What it was worth, real-model corpus

The harness kept each attempt's recovered trace, so the uncharged attempts are
recoverable from disk. Attempt *n* is written to `-CausalLine`, then `-esc1`,
then `-esc2`; the reported figure matches the last of them.

| set | seed | regime | delivered | reported | true total | ratio |
|---|---|---|---|---:|---:|---:|
| primary | 20260917 | small | restart_all | 10,687 | **13,106** | 1.23× |
| primary | 20260920 | small | agent_restart | 1,835 | **3,214** | 1.75× |
| primary | 20260921 | small | agent_restart | 1,207 | **2,301** | 1.91× |
| control | 20260919 | large | agent_restart | 9,272 | **18,685** | 2.02× |
| control | 20260919 | small | agent_restart | 1,221 | **2,289** | 1.87× |
| **escalated runs, pooled** | | | | **24,222** | **39,595** | **1.63×** |

Corpus-wide effect on mean CausalLine replay tokens per run:

| corpus | reported | corrected | ratio |
|---|---:|---:|---:|
| primary (15 runs) | 7,011 | **8,014** | 1.143× |
| control (15 runs) | 6,065 | **6,764** | 1.115× |

So CausalLine's real-model total cost per primary run moves from
16,006 + 7,011 = 23,017 to **16,006 + 8,014 = 24,020**, which against B0's
10,581 is **2.27× B0**, not 2.18×.

---

## 4. Verification symmetry

The brief requires B0 and B2 to get "equivalent verification treatment". The
answer is simpler than expected and worth stating plainly:

**Verification costs zero tokens in this system.** `verify()` performs a taint
walk over the recovered graph, a D-068 byte re-check of recovered prompts and
outputs, a live-memory-reference check, and a task check that is a string
comparison. None of them calls a model.

So verifying the baselines does not change their token cost. What verification
can do is **fail**, and failure triggers escalation, which does cost. The real
asymmetry was therefore never "CausalLine pays for verification" but
"CausalLine pays for escalation and the baselines cannot escalate because
nobody checks them."

Both are now reported:

- **unverified**, which is how B0 and B2 are defined, and
- **verified-and-escalated**, which is what CausalLine and IREC actually
  deliver.

`IREC_LIVE_VALIDATION.md` §6 reports IREC's verification verdict per run from
the production `verify()`, driven by IREC's own splice/replay bookkeeping. On
the live runs so far it passes without escalating, so its verified and
unverified costs are equal and the comparison against unverified B2 is
like-for-like. **If a future IREC run escalates, its cost must be the sum of
the IREC attempt and the fallback**, and the harness records both.

### A caution carried over from `docs/16`

Escalation in this corpus is driven entirely by the task-level check failing on
runs whose original task had already failed, and all five such escalations were
measured unnecessary — the contamination was already gone at the selective
scope. That is a property of the *corpus*, not of IREC, and it means the
escalation term in CausalLine's cost above is largely a false-alarm cost. It is
still a real cost and is charged. See `docs/16-six-investigations.md` §1.

---

## 5. The fallback term

IREC can in principle do work and then escalate to a B2-shaped recovery. The
accounting for that case is **additive and is not discounted**:

```
C_IREC_total = C_IREC_attempt_1 + ... + C_IREC_attempt_k + C_fallback
```

Nothing in the live campaign has needed it yet, so this formula currently
appears with `k = 1` and `C_fallback = 0`. It is stated here so that a future
run which does escalate cannot be reported as if the earlier attempt were free.

---

## 6. Corrected scripted comparison, 24 cells

3 scenarios × 2 variants × 2 workflows × 2 pricing models. Exact ground truth
per (source, event). No model calls. Both defects fixed.

| method | mean tokens | unsafe cells | task ok | mean work preserved |
|---|---:|---:|---:|---:|
| **IREC (exact)** | **1,112** | **0 / 24** | 24 / 24 | 61.9% |
| B0 full restart | 2,242 | 0 / 24 | 24 / 24 | 0.0% |
| B2 topology closure | 1,598 | 0 / 24 | 24 / 24 | — |
| CausalLine hybrid | 2,991 | 0 / 24 | 24 / 24 | **68.3%** |
| CausalLine lazy | 1,893 | 0 / 24 | — | — |

- `R = C_IREC / min(C_B0, C_B2)`: mean **0.784**, max **1.000**,
  **24 / 24 cells at or below 1.00.**
- `R_CausalLine = C_CL / min`: mean **2.472**, max 4.333.
- The 0.8× target is met in 8 / 24 cells — all of scenario C.
- B2's task column is 24/24 only because of defect A's fix.

Two honest notes:

1. **IREC preserves less work than CausalLine hybrid here** (61.9% vs 68.3%).
   IREC discards any event whose recomputed bytes differ, which is more
   conservative than CausalLine's attribution. This is a real trade and it runs
   the other way on the real-model runs (§`IREC_LIVE_VALIDATION.md` §5).
2. **The scripted matrix cannot exercise IREC's second mechanism at all.**
   `unchanged` is **0 in all 24 cells**: with a deterministic scripted client,
   an event whose prompt changed always produces a different output. Every
   scripted saving comes from the prompt-equality splice. The output-equality
   cutoff only shows value against real models, which is why the live campaign
   was necessary and why no scripted number should be quoted as evidence for it.

---

## 7. Reproducing

```
python -m pytest tests/ -q                               # 623 passed, 1 skipped
python -m src.eval.mixed_validation_report               # per-run real-model tables
```

Defect A's size is measured by calling `run_baseline_recovery` twice on one
trace with `extended=False` and `extended=True`. Defect B's size on the
scripted matrix comes from `recover()`'s own `replay cost by attempt` note; on
the real-model corpus it is the sum of `pipeline_tokens()` over
`*-CausalLine.jsonl`, `*-esc1.jsonl` and `*-esc2.jsonl` in
`data/runs/mixed56-*/`. The corrected scripted comparison is
`docs/research/prototype/cutoff_replay.py` (`run_cp` and `run_cl`) over the 24
cells.
