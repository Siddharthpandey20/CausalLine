# Local LLaMA Experimental Frontier — Execution Brief

## Objective

Add a **second real-LLM experimental frontier using the local LLaMA model**.

The existing NVIDIA/API-based frontier is the current control and MUST remain unchanged.

Do not replace, refactor, delete, or alter its experiment logic or existing results.

The new frontier should answer:

> Does CausalLine's influence → contamination → selective recovery pipeline still work when the agents are powered by a real local LLM, and how does it compare with B0/B1/B2?

---

## Phase 1 — Measure the local LLaMA capacity FIRST

Before running the actual research campaign, determine the maximum **safe concurrency** of the local LLaMA setup.

Do not guess the concurrency.

Run a controlled concurrency sweep, increasing gradually (e.g. 1, 2, 4, 8, 16, ... as appropriate for the machine).

For each level measure:

- successful calls
- failures
- timeouts
- OOM/runtime failures
- throughput
- p50/p95 latency
- GPU utilization
- GPU VRAM usage
- RAM usage

Repeat important concurrency points so that the selected value is not based on a one-off result.

Determine:

- `C_safe` — highest reliably stable concurrency
- `C_saturated` — where additional concurrency stops giving useful throughput
- `C_failure` — where instability begins

The actual campaign MUST operate at or below `C_safe`.

Save the benchmark results separately from the existing experiment artifacts.

---

## Phase 2 — Add Local LLaMA as another LLM backend

Add the smallest clean integration necessary for the existing pipeline to use the local LLaMA model.

The architecture should effectively become:

    existing pipeline
       ├── NVIDIA/API LLM  ← existing frontier, untouched
       └── Local LLaMA     ← new frontier

Reuse the existing experiment, tracing, provenance, attribution,
recovery, verification and metric infrastructure wherever possible.

Do not duplicate the whole pipeline.

Record the exact local model/runtime configuration used.

---

## Phase 3 — Real-LLM campaign

Run a sufficiently large campaign using Local LLaMA, constrained by the measured safe concurrency.

Keep the existing scenarios:

- A — malicious external/web content
- B — poisoned memory
- C — malicious inter-agent message

Keep the existing baselines:

- B0
- B1
- B2
- CausalLine

Test at minimum:

- exposed-only contamination
- genuinely influencing contamination
- multi-hop contamination
- multiple contaminated sources
- redundant sources

If the existing framework supports it cleanly, also increase workflow complexity
with longer/branching multi-agent topologies.

Do not weaken the existing experiment definitions just to make LLaMA work.

---

## Phase 4 — Measure the actual algorithm, not just "tests passed"

For cases with independent ground truth, report:

### Influence attribution

- TP
- FP
- FN
- Precision
- Recall
- F1

### Contamination identification

- contamination precision
- contamination recall
- contamination F1
- predicted contamination size
- actual contamination size
- blast radius

### Recovery safety

- unsafe preservation count
- unsafe preservation rate
- residual contamination
- pair-level false negatives
- verification failures
- escalations

### Recovery effectiveness

For B0/B1/B2/CausalLine:

- work preserved
- invalidated work
- replayed work
- recovery success
- final task success

### Cost

Measure separately:

- analysis cost
- recovery/replay cost
- total CausalLine cost
- baseline recovery cost
- time/inference cost where token accounting is unavailable

Then determine whether selective recovery actually saves cost compared with
coarse recovery/full restart.

Do NOT claim CausalLine is cheaper unless the measurements support it.

---

## Phase 5 — Important robustness experiment

Explicitly test **redundant information**.

Example:

    Source A ──┐
               ├──→ same downstream decision
    Source B ──┘

Test removal of:

- A
- B
- A + B

Determine whether the influence attribution mechanism misses jointly
redundant sources.

Report the failure if it occurs.

Do not hide it.

---

## Phase 6 — Real-LLM nondeterminism

Because LLaMA is a real nondeterministic model:

- record model/configuration/seed/temperature where applicable
- do not treat every textual difference as contamination
- use the existing decision/code/JSON/tool signatures where appropriate
- distinguish natural model variability from contamination-induced changes

Do not claim byte-identical replay when the model itself is nondeterministic.

---

## Phase 7 — Compare with the existing NVIDIA/API frontier

Do NOT overwrite or modify the existing results.

Produce a separate comparison:

    Existing NVIDIA/API frontier
                vs
        Local LLaMA frontier

Compare:

- attribution quality
- contamination identification
- unsafe preservation
- work preserved
- recovery success
- task success
- analysis cost
- recovery cost
- total cost
- escalation rate

If LLaMA disagrees with the existing results, report the disagreement rather
than tuning the experiment to force agreement.

---

## Phase 8 — Statistical reporting

For every important result report:

- number of runs `n`
- mean/percentage as appropriate
- variance where meaningful
- confidence interval where appropriate

Do not manufacture statistical significance from insufficient samples.

Prefer repeated runs (target >=30 per important design point where practical).

---

## Files / artifacts

Keep the Local LLaMA experiments isolated, e.g.:

    data/results/local_llama/

and documentation under:

    docs/local_llm_frontier/

Create only the minimum documentation necessary to record:

1. concurrency benchmark
2. experiment configuration
3. campaign results
4. failure analysis
5. comparison with the existing frontier
6. final scientific conclusions

---

## Strict "DO NOT CHANGE" rules

DO NOT:

- delete the NVIDIA/API frontier
- modify existing NVIDIA/API experiment definitions
- overwrite existing campaign results
- change existing baselines
- change CausalLine's evaluation criteria just for LLaMA
- remove safety checks
- weaken assertions
- inject ground-truth labels into the recovery algorithm
- modify existing results after seeing LLaMA results
- silently change metrics
- hide failed runs
- hide negative results

Ground truth is for evaluation only.

---

## Checkpoint requirement

Work incrementally:

### Checkpoint 1
Measure and report local LLaMA concurrency capacity.

### Checkpoint 2
Integrate the local LLaMA backend and run a small end-to-end smoke campaign.

### Checkpoint 3
Verify that all existing tests still pass and the NVIDIA/API frontier is
unchanged.

### Checkpoint 4
Run the real LLaMA campaign.

### Checkpoint 5
Produce the final metrics and comparison.

At every checkpoint, report what was actually measured before proceeding.

---

## Final output

The final report must answer these questions plainly:

1. What is the safe LLaMA concurrency?
2. How many real-LLM runs were completed?
3. How accurate is influence attribution? (Precision/Recall/F1)
4. How accurately does CausalLine identify contamination?
5. How much work does CausalLine preserve versus B0/B1/B2?
6. How many unsafe contaminated events were preserved?
7. Does recovery actually restore task success?
8. What does recovery cost?
9. Is selective recovery cheaper than coarse/full recovery, and under what conditions?
10. Does redundancy break attribution?
11. Do longer/multi-agent workflows change the result?
12. How do LLaMA results compare with the existing NVIDIA/API results?
13. Which conclusions are supported by the new experiment?
14. Which conclusions remain unsupported?

The final report must clearly separate:

    what was tested
    what was measured
    what was observed
    what was not established

The goal is not to produce a positive result.

The goal is to determine whether the CausalLine hypothesis survives
large-scale testing on a real local LLM.