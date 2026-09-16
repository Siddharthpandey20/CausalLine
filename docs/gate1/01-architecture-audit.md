# Gate 1 — Phase 1: architecture audit

Traced by following the execution path, not by reading docstrings. Every claim
below was checked with `grep` across `src/` and confirmed against the code.

---

## 1. Where investigation actually starts

**Not** in `recovery/causalline.py`. `recover()` plans, replays, verifies and
escalates — by the time it runs, the investigation is already paid for.

The real sequence is in `src/eval/real_llm.py::run_generated` (and its scripted
twin `src/eval/experiment.py::run_cell`):

```
run_pipeline(...)                 -> trace          [pipeline tokens, N]
label_malicious(...)              -> planted
build_detector(...).flag(trace)   -> flagged
if refine and flagged:
    refine_for_verdict(...)       <-- THE INVESTIGATION. All of A is spent here.
recover(trace, flagged, ...)      -> plan, replay, verify, escalate
```

**There is no gate between `flagged` and `refine_for_verdict`.** The only
condition is `if refine and flagged:`. That is precisely the hole this work is
about: the decision to spend `A` is never made, it is assumed.

## 2. Where analysis tokens are spent

Inside `refine_for_verdict` (`src/provenance/estimator.py`), in three places,
all logged to the trace with a `UsagePurpose`:

| purpose | what it is | when |
|---|---|---|
| `self_report` | ask the agent which sources it used | eager: every model event of every run. lazy (D-089): once per event the frontier reaches |
| `counterfactual` | re-issue a redacted prompt, compare signatures | per candidate pair, or per group |
| `replay` | the recovery itself | after planning — this is `R`, not `A` |

`trace.analysis_tokens()` = self_report + counterfactual. `trace.pipeline_tokens()`
= `N`. The split is already clean; no change needed for accounting.

## 3. How candidates are selected

`_unchecked_in_region(trace, region.sources)` inside a `while True:` frontier
expansion:

```
region = contaminate(trace, flagged)      # recomputed EVERY iteration
pending = _unchecked_in_region(trace, region.sources)
if not candidates: break
work one event at a time, group its sources
```

**This is the single most important fact for the SPRT hypothesis.** Candidates
are *not* drawn from a fixed pool. A clean verdict upstream shrinks the region
and can remove downstream candidates entirely, so:

- the candidate sequence is **adaptive** — what you check next depends on what
  you found;
- it is **ordered** — forward through the trace, deliberately (upstream first);
- the population is **non-stationary** — the denominator of "the contamination
  fraction f" changes as the walk proceeds.

## 4. How the estimator's stages interact

```
HybridAttributor(mode=...)          during the pipeline (eager) or not at all (lazy)
  -> self-report claim per (source, event)
     positive claim + not audited  -> accepted, no counterfactual
     negative claim                -> needs evidence
group_test / merge_derived_units    batch several sources into one call
  -> counterfactual on the group
     group clean -> every member cleared in ONE call
SPRTState.observe(bool)             one Bernoulli observation per settled source
```

So the cost per candidate is not constant: group testing can settle several
sources per call, and a self-report positive settles one for free.

## 5. Where SPRT exists

`src/recovery/sprt_investigate.py`. `SPRTState` is constructed in
`refine_for_verdict` and `.observe()` is called after each verdict;
`decision()` returning `abort_restart` breaks the loop and leaves the rest
`unchecked` (which the walk contaminates — expensive in preserved work, never
unsafe).

## 6. What `config_for(analysis_tokens, restart_tokens)` does

Returns `SPRTConfig(f_star=clamp(1 - A/N), f_star_high=f_star + 0.15)`. It
converts the cost model into the two hypotheses. `margin=0.15` is an explicit
indifference band and is **a parameter, not a measurement**.

## 7. Is `sprt_config` connected?

**Now yes, as of D-087, but by a different route than the obvious one.**
`sprt_config=` is still passed by no caller. Instead `refine_for_verdict`
derives it internally when `sprt_config is None`, via
`_sprt_config_from_trace`, so every caller gets it rather than one.

Before D-087: `config_for` had no caller outside its own tests, and every
investigation this project ever ran used `f_star = 0.3 / 0.7`.

## 8. What the replay-vs-restart gate does

`planner.greedy_cover`:

```python
if sunk + spent + best.cost > cap and best.kind != "restart_all":
    return [restart_all]        # cap = restart_all_cost(trace) = pipeline tokens
```

`spent` is accumulated **replay** cost; `sunk` is 0 unless
`count_sunk_analysis=True` (D-088, off by default and correctly so). This is
**Gate 2** and it is sound: at this point `A` is sunk and cancels.

## 9. What is available BEFORE expensive investigation

Everything structural, for free:

- the full event graph, call graph, and `derived_from` links;
- `flagged` — which sources the detector named;
- every exposure edge and its channel;
- per-event replay cost (`policy.event_cost`) and `restart_all_cost`;
- checkpoints and the safe frontier;
- the three baselines' discard sets (`b0/b1/b2`) — all pure graph functions.

## 10. Cheap signals that already exist and predict footprint

| signal | where | cost | callers |
|---|---|---|---|
| `structural_prior(trace, flagged)` | `sprt_investigate.py:289` | free | **NONE** |
| `b2_topology_closure` | `baselines.py` | free | baselines only |
| `run_probability(trace)` — this is **P** | `risk/attack_model.py:249` | free | economics sweep only |
| `should_investigate(P)` | `economics.py:433` | free | economics sweep only |
| `ex_ante_decision(A, N, f_prior)` | `economics.py:127` | free | **tests only** |

`structural_prior` is the cost-weighted B2 closure — an explicit upper bound on
`f`, written for `ex_ante_decision`, and **never called by anything**.

### What P actually is

`P = 1 - PROD_i (1 - pa_i)^{m_i}` over exposure-edge counts per channel, with
`pa` from Zou et al. 2025 (27.1% indirect injection, 5.7% direct).

**Two properties that matter for using it as a gate, both already documented
against itself:**

1. It counts **exposure, not influence** — deliberately, since P is the
   probability an attack *attempt* succeeded.
2. **It saturates.** At ~64 exposure edges per run it returns 1.000. The
   docstring of `should_investigate` says so outright: "on our own traces this
   gate never fires".

So P as currently defined answers *"was this run attacked at all?"*, not
*"how much of it is contaminated?"* — and those are different questions. It is
also monotone in exposure count, which means a long workflow with many benign
retrievals scores higher than a short one with a single decisive injection.

## 11. Metrics and experiment infrastructure

- **scripted** (`experiment.py`, `scripted.py`): `ScriptedClient` computes the
  true (source, event) influence relation by leave-one-out. Ground truth is
  **exact, by construction**, deterministic and free. `ground_truth_events()`
  is the yardstick.
- **real-LLM** (`real_llm.py`): observed ground truth via canary token.
- **fan-out** (`fanout_*.py`, D-091): localized contamination, `f` as a dial.
- `economics.py` already has `SweepPoint`, `f_star_ex_ante`,
  `ex_ante_decision`, `ex_post_decision` and a `SunkCostError` guard that
  refuses to let `A` into the ex-post comparison.

---

## Summary of the hole

The components for Gate 1 **all exist and none are connected**:

```
                     EXISTS   WIRED
ex_ante_decision       yes     no    (tests only)
structural_prior       yes     NO    (zero callers)
should_investigate     yes     no    (sweep only; saturates)
config_for             yes     yes   (D-087, internal)
SPRT abort             yes     yes
Gate 2 (replay cap)    yes     yes
```

The task is therefore not to invent a mechanism but to find out **which cheap
estimate of `f` is good enough to gate on**, and whether gating helps or just
adds a new way to be wrong.
