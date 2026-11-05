# docs — what is here, and which of it is still true

Twenty-nine documents accumulated over the project. Nine of them were reachable
from nothing at all — not from code, not from `CLAUDE.md`, not from each other —
so a new reader could not find them and, worse, could not tell whether what
they said still held. This index is the fix. It is the only document in `docs/`
whose job is to describe the others.

**The rule that matters:** a later document supersedes an earlier one *on the
numbers it re-measures*, never wholesale. The earlier measurement usually stays
correct about the system as it was, and that system is part of the record. So
"superseded" below means *do not quote this figure*, not *this document is
wrong*.

## Start here

| If you want | Read |
|---|---|
| **the whole project in one document** | [`overview.md`](overview.md) |
| what the project is and why | [`00-idea.md`](00-idea.md), then the root [`README.md`](../README.md) |
| the current architecture | [`02-architecture.md`](02-architecture.md) |
| **the primary experimental result** | [`14-workload-varied-validation.md`](14-workload-varied-validation.md) |
| **which statistic that result supports** | [`16-six-investigations.md`](16-six-investigations.md) sections 3-5 |
| **what we may and may not claim** | [`10-remediation.md`](10-remediation.md) §9 |
| what the system does not do | [`06-limitations.md`](06-limitations.md) |
| why a design is the way it is | [`05-decisions.md`](05-decisions.md) (D-001..D-094) |
| to run it on another machine | [`../COLLEGE_SETUP.md`](../COLLEGE_SETUP.md) |

## Current — the live reference set

| Document | What it is |
|---|---|
| [`overview.md`](overview.md) | the whole project in one document: problem, positioning, method, architecture, results, the research journey, the negative results, and the claims-we-can/cannot lists. Overlaps the root `README.md` §1–7 by design — that is the front page, this is the briefing |
| [`00-idea.md`](00-idea.md) | the original project description |
| [`01-scope.md`](01-scope.md) | scope and ownership |
| [`02-architecture.md`](02-architecture.md) | components and how they connect |
| [`03-open-issues.md`](03-open-issues.md) | known weak points; **read before proposing a design** |
| [`04-experiments.md`](04-experiments.md) | attack scenarios, baselines, metrics |
| [`05-decisions.md`](05-decisions.md) | the decision log; every non-obvious choice and its reason |
| [`06-limitations.md`](06-limitations.md) | what the system does not do; **read before quoting a number** |
| [`09-real-llm-evaluation.md`](09-real-llm-evaluation.md) | the real-LLM mode, and how far it moves external validity |
| [`10-remediation.md`](10-remediation.md) | the phase-gated remediation pass; **§9 is the current claims list** |
| [`12-issue20-correction.md`](12-issue20-correction.md) | the ingestion-edge defect and every figure it moved |
| [`13-mixed56-validation.md`](13-mixed56-validation.md) | the fixed-workload control: 5 seeds x 3 regimes, isolates model non-determinism |
| [`14-workload-varied-validation.md`](14-workload-varied-validation.md) | **the primary result**: 15 runs, 15 distinct attack placements. **Read `16` section 3 before quoting its small regime** |
| [`15-cost-direction-reopened.md`](15-cost-direction-reopened.md) | the cost direction reopened: why the closure-scoped mechanism was rejected, and the retraction of the 3.6-point figure |
| [`16-six-investigations.md`](16-six-investigations.md) | **six investigations into what is actually wrong**: the verification failures diagnosed, restart vs splice, the small-regime recalculation, a safety-constrained comparison, the detector band, and the 100-seed cost result. Supersedes `14` on how its small regime reads and `07`/`08`/`10` on how `work_preserved` is compared |

## research/ — the IREC line, under validation

A cost-reduction direction investigated in November 2026: remove attribution as
a separate stage and let the recovery replay perform the counterfactual
(**IREC**, Interventional Replay with Early Cutoff). **Verdict: CONDITIONAL
GO.** Integrated behind a flag on branch `feat/irec`, off by default; current
CausalLine remains the production strategy.

| Document | What it is |
|---|---|
| [`research/IREC_FINAL_DECISION.md`](research/IREC_FINAL_DECISION.md) | **start here.** The GO/NO-GO, the acceptance criteria one by one, the economic verdict against both targets, and the three conditions on promoting the default |
| [`research/IREC_LIVE_VALIDATION.md`](research/IREC_LIVE_VALIDATION.md) | the 15-placement real-model campaign: 0 unsafe, 100% recall, 15/15 verified, R = 0.76 / 0.63 / 0.95, and the nondeterminism measurement that replaces assumption A3 |
| [`research/IREC_COST_ACCOUNTING.md`](research/IREC_COST_ACCOUNTING.md) | **two accounting defects, fixed and measured.** Baselines replayed on the wrong fixtures in long workflows (task success 6/18 → 18/18); escalated runs charged only their last replay (1.63× understated). **Read before quoting any cost figure from `07`–`14`** |
| [`research/IREC_SAFETY_AND_NOVELTY.md`](research/IREC_SAFETY_AND_NOVELTY.md) | the exact-equality safety argument, the complete-mediation census, the side-effect audit, the bound's scope limits, and what is actually new |
| [`research/FINAL_COST_OPTIMIZATION_RESEARCH.md`](research/FINAL_COST_OPTIMIZATION_RESEARCH.md) | the original research report that proposed IREC. Its §6 ratios are **offline predictions**; the live measurements supersede them |
| [`research/RELATED_WORK_AND_SOURCES.md`](research/RELATED_WORK_AND_SOURCES.md) | the literature inventory behind the novelty assessment |
| `research/prototype/` | the isolated harness. Nothing in `src/` imports it |

## Superseded on their numbers, kept for the record

Correct measurements of the system as it then stood. Quote the replacement.

| Document | Superseded by, and on what |
|---|---|
| [`07-completion-report.md`](07-completion-report.md) | `08` generally; `12` on every CausalLine `work_preserved` figure |
| [`08-final-report.md`](08-final-report.md) | `10` on the numbers it re-measures; `12` on `work_preserved` ; **`16` section 4 on how `work_preserved` may be compared across methods** |
| [`11-phase5-report.md`](11-phase5-report.md) | `12` on `work_preserved`. **Also see `15` on its "3.6 points" figure** |
| [`real_mixed_model_60_agent_experiment.md`](real_mixed_model_60_agent_experiment.md) | `13` on its results sections 6–12. §1–5 (architecture, Phase 0 API findings, fault log) are current and referenced by code |

## Gate 1 — a falsified line of work, kept deliberately

Gate 1 asked whether the system could decide *before* spending analysis whether
to investigate or restart. **It is not part of the contribution and must not be
presented as one.** These documents are retained because a reviewer may ask why
not, and because two of them are the only record of how the hypotheses died.

| Document | Verdict |
|---|---|
| [`gate1/01-architecture-audit.md`](gate1/01-architecture-audit.md) | where investigation actually starts; traced from the execution path |
| [`gate1/02-experiments.md`](gate1/02-experiments.md) | H1 per-node compromise probability — **falsified**, the aggregate saturates to 1.000 |
| [`gate1/hypothesis_search_environmental.md`](gate1/hypothesis_search_environmental.md) | H1 and H2 both survive environmentally, 94% over 32 cases, neither dominates |
| [`gate1/h2_adversarial_break_test.md`](gate1/h2_adversarial_break_test.md) | H2 — **falsified**; 64% on an adversarial suite, below the gate it replaced, minimal counterexample 5 events |
| [`gate1/real_llm_falsification.md`](gate1/real_llm_falsification.md) | H2 fails on the real model |
| [`gate1/targeted_real_llm_break_test.md`](gate1/targeted_real_llm_break_test.md) | the targeted real-model break test |
| [`gate1/next_hypothesis_search.md`](gate1/next_hypothesis_search.md) | **inconclusive, informationally underspecified** — the gate rule adds two 99–100% correlated terms |
| [`gate1/cost_without_quality_loss.md`](gate1/cost_without_quality_loss.md) | what qualifies as cheaper-at-equal-quality. **See `15`: its chain-matrix disqualification does not survive replication** |
| [`gate1/final_cost_direction.md`](gate1/final_cost_direction.md) | "Outcome B". §6.1 carries its own correction. **See `15`** |

## Local-LLM frontier — the self-hosted measurements

Four documents, referenced by nothing, and the only place several numbers the
rest of the project quotes actually come from. Current as measurements.

| Document | What only this document records |
|---|---|
| [`local_llm_frontier/01-benchmark-and-config.md`](local_llm_frontier/01-benchmark-and-config.md) | what the machine can do: the ~4 tok/s CPU figure and the flat-concurrency finding that sets `--max-concurrency 1` |
| [`local_llm_frontier/02-gpu-campaign.md`](local_llm_frontier/02-gpu-campaign.md) | the GPU validation and repeated campaign; CPU runs kept separate and labelled |
| [`local_llm_frontier/03-token-economics.md`](local_llm_frontier/03-token-economics.md) | where the tokens go, and the condition under which recovery would beat restart |
| [`local_llm_frontier/04-localized-contamination.md`](local_llm_frontier/04-localized-contamination.md) | the first run where `A/N + f` came out below 1.00, and the first delivered selective recovery. §5 lists what it does not show |

## Elsewhere in the repository

| Path | What it is |
|---|---|
| [`../README.md`](../README.md) | the project overview and the offline commands |
| [`../CLAUDE.md`](../CLAUDE.md) | working rules, vocabulary, the two evaluation modes |
| [`../COLLEGE_SETUP.md`](../COLLEGE_SETUP.md) | running on another machine, local Ollama first |
| [`../paper/`](../paper/) | the conference draft |
| [`../minor_report/`](../minor_report/) | the mid-term evaluation report (a different argument; not the paper) |

## Two evaluation modes, never pooled

Repeated here because it is the easiest mistake to make with these documents.
**scripted** ground truth is known by construction, per (source, event) pair,
deterministic and free. **real-LLM** ground truth is observed — canary presence
plus the pipeline's own code-path records — narrower and non-deterministic. A
number from one does not belong in a table with a number from the other, and
before quoting a real-LLM row, check `payload_landed`: a run the model ignored
has nothing to preserve unsafely, and its zero is arithmetic.
