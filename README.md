# CausalLine

Selective recovery for compromised multi-agent LLM systems.

Existing work on prompt injection, poisoned tool output and poisoned memory
concentrates on *detecting* that an agent was compromised. CausalLine starts
after detection. Told "the Researcher is compromised", current practice throws
away everything that agent produced and everything downstream of it. An agent
that produced four outputs may have had one contaminated, and a downstream
agent that merely *received* that output may never have *used* it.

**The claim under test: exposure is not influence.** The system finds which
specific pieces of work a malicious source actually influenced, preserves the
rest, and recomputes only the affected part from the nearest trusted
checkpoint.

This is a research prototype for an 8-page paper, not a product.

---

## Install and run

Python 3.11+. The core has **no third-party dependencies**: tracing,
provenance, recovery and the scripted evaluation import nothing outside the
standard library.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install pytest                 # that is genuinely all the suite needs

python -m pytest tests/            # 206 passed, 7 skipped
```

The 7 skips are the Lasso fallback tests, which need numpy. With the analysis
extra installed the suite is **212 passed, 1 skipped**. Both numbers are
enforced by CI, in separate jobs, so an unguarded third-party import in the
core fails the build rather than quietly raising the floor.

For the economics figures and the fixed-budget Lasso fit:

```bash
pip install -r requirements.txt        # numpy, matplotlib
# or, to reproduce a published number exactly:
pip install -r requirements-lock.txt
```

Everything below runs offline against a scripted agent. No API key, no quota.

```bash
python -m src.eval.experiment --detector oracle   # one matrix, all methods
python -m src.eval.campaign                       # 96 cells x 30 reps (~20 min)
python -m src.eval.economics                      # cost model + required figures
python -m src.eval.contract data/runs/ci.jsonl    # trace contract checker
```

The live experiments need a `GEMINI_API_KEY`. Copy `.env.example` to `.env` and
fill it in; `.env` is gitignored. The free tier is **20 requests/day** and one
validation channel costs exactly 20, so a live pass runs one channel per day:

```bash
python -m src.eval.token_validation --dry-run              # costs nothing
python -m src.eval.token_validation --offline              # harness check
python -m src.eval.token_validation --channels web         # ~20 live requests
```

---

## Layout

```
src/tracing/      event logging, call graph, event graph, checkpoints
src/provenance/   source IDs, influence edges, counterfactual checking
src/recovery/     contaminated region, recovery planner, selective replay
src/eval/         attack injection, baselines, metrics, economics, run harness
src/risk/         attack-probability model (per-channel pa)
data/             traces, results (gitignored except small samples)
paper/            LaTeX / drafts
docs/             design, decisions, limitations, results
```

Read `docs/` before changing anything: `03-open-issues.md` before proposing a
design, `06-limitations.md` before quoting a number, `05-decisions.md` for why
something is the way it is.

---

## Where the numbers stand

Work preserved against B1 (agent-level taint), oracle detector, 30
repetitions, 95% CIs. Higher is better; B1 is the baseline we claim to beat.

| scenario | CausalLine | B1 | gain |
|---|---|---|---|
| A influencing | 44.2% ± 4.4% | 21.1% | +23.2 |
| A exposed-only | 96.0% ± 4.8% | 21.1% | +74.9 |
| B influencing | 65.1% ± 1.0% | 57.9% | +7.2 |
| B exposed-only | 95.6% ± 3.1% | 57.9% | +37.7 |
| C influencing | 66.5% ± 1.1% | 52.6% | +13.9 |
| C exposed-only | 93.7% ± 6.3% | 52.6% | +41.1 |

**Zero event-level unsafe preservations** for CausalLine across all 24 campaign
cells, under four detectors including a blind control and a real classifier,
and under an adversarial self-reporter. Safety also survives total detection
failure: at a simulated miss rate of 1.0 CausalLine still records 0% unsafe,
where B1 and B2 record 50%.

The margin is largest on exposed-only incidents, which is the
exposure-is-not-influence claim paying off directly.

### What we do not claim

Stated here rather than buried, because several of these were once claimed on
numbers that turned out to be artifacts:

- **The analysis does not pay for itself.** A/N is 1.23 at the short workflow
  length under a proportional token cost model, and above 1 everywhere
  measured. We can say exactly what would have to change (docs/07 §6).
- **Savings do not scale with workflow length.** Measured across a 42% longer
  trace, A/N is flat (1.23 → 1.24), not improving. An earlier flat-cost model
  made it look like it *degraded*; that was an artifact of pricing calls
  instead of tokens.
- **Checkpoint GC frees nothing**, at any checkpoint density. Its precondition
  is a prefix provably free of influence, which a run that did useful work
  never has (docs/05 D-050).
- **The attack-probability gate contributes nothing.** Per-node `P` varies, but
  the aggregate `run_probability` saturates to 1.000 at every length measured.
- **Real-model evidence is one channel, five pairs.** A single live run against
  `gemini-3.6-flash` scored 60% agreement with nonce-token ground truth, with
  0 unsafe disagreements — both errors were false positives. Every other number
  in this repository comes from a scripted client.
- **Counterfactual attribution has a redundancy blind spot.** When a summary
  and its own inputs are both in an agent's context, single-source
  leave-one-out reports that neither influenced anything. It gets worse as
  workflows lengthen, and subset testing is exponential.

---

## Vocabulary

Used consistently in the code and the paper:

- **event** — one recorded operation
- **source** — an incoming unit of information, with an ID (S1, S2, …)
- **exposure** — a source was present in an agent's context
- **influence** — a source demonstrably changed the agent's output
- **contaminated** — influenced, directly or transitively, by a malicious source
- **clean** — established as not influenced
- **recovery set** — the events to invalidate and recompute
- **unsafe preservation** — something called clean that was actually
  contaminated. The dangerous error. Always reported.
