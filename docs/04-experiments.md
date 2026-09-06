# 04 — Experiments

The paper lives or dies here. Design the tables first, then fill them.

## Attack scenarios

Three, roughly 30 runs each. We author each attack, so ground truth is
known by construction.

**A. Injected tool output.** The Researcher's web tool returns a page
containing an injected instruction. One of the Researcher's outputs is
built on it.

**B. Poisoned memory entry.** A memory entry written earlier is malicious.
An agent reads it mid-workflow.

**C. Malicious inter-agent message.** A compromised agent sends a crafted
message to the next agent in the chain.

For each scenario, generate two variants:

- **influencing** — the downstream agent actually uses the poisoned content
- **exposed only** — the downstream agent receives it but does not use it

The exposed-only variant is where our method should beat the baselines.
Report the split; do not silently over-sample the favourable case.

## Baselines

| Name | Behaviour |
|---|---|
| B0 full restart | discard everything, re-run the whole workflow |
| B1 agent-level taint | discard the compromised agent's outputs and all downstream agents |
| B2 topology closure | discard everything reachable in the call graph |
| **Ours** | discard only events connected by influence edges |

B1 is the honest representation of current practice and is the baseline to
beat. B2 is the strict upper bound.

## Metrics

**Work preserved (%)** — fraction of clean events kept rather than
recomputed. Higher is better. This is the headline number.

**Recovery cost (tokens)** — tokens to get back to a valid state, split
into: analysis cost + replay cost. Compare against B0's cost of a full
re-run. Must include the cost of counterfactual checks; hiding that is
the easiest way to look good dishonestly.

**Unsafe preservation rate (%)** — fraction of runs where we preserved an
event that was truly contaminated. Lower is better. **Always report this.**
A reviewer will ask; leading with it is far stronger than being caught.

**Recovery success rate (%)** — fraction of runs where the workflow
reaches a correct final outcome after recovery, judged by task success,
not text similarity.

Secondary: storage overhead (bytes and % of workflow tokens), wall-clock
time, break-even point where analysis cost equals rerun cost.

## Main result table (design it now)

| Scenario | Method | Work preserved | Recovery tokens | Unsafe pres. | Success |
|---|---|---|---|---|---|
| A influencing | B0 / B1 / B2 / Ours | | | | |
| A exposed-only | B0 / B1 / B2 / Ours | | | | |
| B influencing | ... | | | | |
| B exposed-only | ... | | | | |
| C influencing | ... | | | | |
| C exposed-only | ... | | | | |

## Ablations (cheap, add credibility)

- self-report only, no counterfactual — how much accuracy is lost?
- counterfactual only, no self-report — how much extra cost?
- conservative fallback off — how much does unsafe preservation rise?

## Figures

1. Pipeline diagram with the poisoned source marked (explains the idea)
2. Exposure graph vs influence graph on one real run, side by side —
   this is the figure that sells the paper
3. Bar chart: work preserved by method and scenario
4. Scatter or line: analysis cost vs replay saved, with break-even marked

## Run hygiene

- Fix temperature at 0, log model name, version, seed, and date
- Log every run to `data/runs/` with a config hash
- Repeat each comparison 3 times to guard against non-determinism
- Cap spend per scenario; log token counts per run from day one
- Never re-run a scenario after tables are drafted without re-running all
  methods on it
