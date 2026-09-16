# CausalLine

**Selective recovery for compromised multi-agent LLM systems.**

Research prototype for an 8-page paper. Python 3.11+, no third-party
dependencies in the core.

---

## Quickstart

**Offline — no API key, no quota, nothing to sign up for.** This runs the whole
scripted evaluation, which is where every headline number in this repository
comes from.

```bash
git clone <this repo> && cd CausalLine
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install pytest

python -m pytest tests/              # 327 passed, 7 skipped
python -m src.eval.experiment        # the full method comparison, ~1 min
```

**With one API key — the real-LLM mode.** A free key from
[build.nvidia.com](https://build.nvidia.com) is enough; the second and third
slots are a fallback chain for long campaigns, not a requirement.

```bash
cp .env.example .env                 # Windows: copy .env.example .env
# put your key in NVIDIA_API_KEY_1

python -m src.common.nvidia --doctor           # checks setup, says what to do next
python -m src.eval.real_campaign --plan --n 3  # free: what it would do and cost
python -m src.eval.real_campaign --n 3         # live: generate, run, score
```

`--doctor` is the one to run when something is wrong. It reports whether your
`.env` was found, how many keys loaded, whether each model actually answers,
and what to type next — and it never prints a key.

> **Before quoting any real-LLM number, read `docs/09-real-llm-evaluation.md`
> §9.** Those runs use a narrower, observed ground truth and a single model.
> They do not replace the scripted numbers and must not be pooled with them.

---

## 1. The problem: everyone stops at detection

A multi-agent LLM system can be attacked through prompt injection, poisoned
tool output, or poisoned memory. There is a large and growing literature on
**detecting** that this happened. There is almost nothing on what to do next.

Current practice, when told *"the Researcher agent is compromised"*, is to throw
away everything that agent produced and everything downstream of it. That is
wasteful, and it is often simply wrong:

- an agent that produced four outputs may have had exactly **one** contaminated
- a downstream agent that *received* a poisoned output may never have **used** it

Both of those are discarded anyway, because the blast radius is computed from
the agent graph rather than from what actually happened.

### The core claim

> **Exposure is not influence.**

A source being present in an agent's context is not the same as that source
having changed the agent's output. CausalLine measures the difference, preserves
the work that was only *exposed*, and recomputes only the work that was actually
*influenced*.

---

## 2. What is novel here

Three things, in decreasing order of how much we would defend them.

**(a) Recovery is treated as a causal problem over a recorded trace, not a
graph-reachability problem over agents.** Contamination propagates along
*influence edges established by evidence*, never along "A talked to B". The
whole system exists to make that distinction cheap enough to be worth making.
Every baseline we compare against is a reachability closure of some kind, and
the gap between them and us is the paper's result.

**(b) The planner optimises exactly what the verifier checks.** Recovery
planning is a cost-minimising set cover, and verification is a taint check.
Those were two different conditions for most of this project's life, which
produced plans that were cheap and unverifiable. They are now one condition, by
construction, and a test pins it. (§4, Step 2/Step 4.)

**(c) Redundant sources are removed as atomic units.** Counterfactual influence
is leave-one-out, and leave-one-out is blind to over-determination: when two
sources carry the same fact, removing either alone changes nothing and **both**
are reported clean. Where the trace *records* that two sources are redundant, we
merge them into one unit that is removed together and never split. This is the
one place we knowingly depart from naive counterfactual testing, and §7 explains
why the remainder of the problem is a complexity result rather than a TODO.

We are **not** claiming a new detector, a new attack, or an optimality result.
Detection is a socket with two real occupants, and the attacks are standard.

---

## 3. Vocabulary

Used identically in the code and the paper. The first two are the whole point.

| term | meaning |
|---|---|
| **event** | one recorded operation (message, tool call, tool response, memory read/write, agent output) |
| **source** | an incoming unit of information, with an ID (`S1`, `S2`, …) |
| **exposure** | a source was present in an agent's context |
| **influence** | a source *demonstrably changed* the agent's output |
| **contaminated** | influenced, directly or transitively, by a malicious source |
| **clean** | established as *not* influenced |
| **recovery set** | the events to invalidate and recompute |
| **unsafe preservation** | something we called clean that was actually contaminated — the dangerous error, always reported |

---

## 4. The approach, end to end

The system runs in two phases: a **traced execution**, then a **recovery**
triggered by a detector verdict.

```
                          ORIGINAL RUN
  user → Planner → Researcher → Coder → [Reviewer] → Executor
           │           │           │                     │
           └───────────┴─────┬─────┴─────────────────────┘
                             ▼
                    trace.jsonl  +  checkpoints  +  content store
                    (events, sources, exposures, influence edges)

                             │   detector says "S4 is malicious"
                             ▼
   Step 0  contamination closure      what is provably tainted?
   Step 1  safe frontier              where can each agent rewind to?
   Step 2  greedy set cover           cheapest actions covering the closure
   Step 3  selective replay           recompute the set, splice the rest
   Step 4  verify                     is the new trace clean, and did the task work?
              │
              └── on failure: escalate (selective → agent_restart → restart_all)
```

### 4.1 Tracing: what gets recorded, and why

Every operation becomes an `event`. Every incoming unit of information becomes a
`source` with an ID. When an agent is called, we record:

- its **exposures** — every source in its context, used or not
- its **prompt and rendered source block**, stored separately so a
  counterfactual can redact a known span rather than guessing where the source
  list ends
- its **output**, by content reference

Exposure is recorded unconditionally and cheaply. Influence is *not* recorded
here, because establishing it costs a model call — which is the entire economic
problem this project is about.

### 4.2 Establishing influence: two stages, cheap then expensive

We cannot see inside an LLM call, so influence is **estimated**. The estimator
is deliberately two-tier:

1. **Self-report** (one cheap call): ask the agent which inputs it used. This is
   a *claim*, not evidence. A positive claim is accepted; a negative claim is
   **never** accepted on its own, because that is the direction that produces
   unsafe preservations.

2. **Targeted counterfactual** (expensive, evidence): re-issue the exact prompt
   with the suspect source redacted, and compare **decision signatures** — never
   raw text. Text comparison is useless here: a measured 8-of-8 textual noise
   floor on the live model means a text flip carries zero information. The
   comparators read structure (chosen library, format codes, control flow, JSON
   shape, tool arguments).

Counterfactuals are spent only where they change a decision, and three
mechanisms reduce how many are needed:

- **Group testing** — remove a whole group in one call; a clean group of eight
  costs one call instead of eight. Measured 15% fewer calls, with a floor of
  ~4 candidates below which halving cannot pay.
- **Atomic units (§6)** — recorded-redundant sources are removed together.
- **SPRT** — a sequential test that aborts early once evidence is decisive.
  Inert on a 19-event trace; **fires** on 27 events.

### 4.3 Step 0 — contamination closure

Given the detector's flagged sources, walk the influence edges (never the
temporal parents, never the agent graph) to get every event transitively
influenced. An unchecked pair is conservatively treated as influenced: the
fallback always costs work, never safety.

### 4.4 Step 1 — safe frontier

For each agent, the latest checkpoint whose **causal past is disjoint from the
taint**. This is not "the most recent checkpoint before the attack": a
checkpoint can sit chronologically after contamination and still be causally
clean, and a cross-agent *domino pass* catches the reverse case, where a kept
event was influenced by an event we are about to discard.

### 4.5 Step 2 — the recovery plan

Choosing the cheapest set of actions is a **minimum-cost cut on a contamination
DAG**, which is NP-hard (weighted hitting set). We do not solve it exactly; we
use the standard greedy approximation, minimising `cost(action) / targets_covered`
with a cap at the cost of restarting everything.

The action vocabulary is `replay`, `invalidate`, `restart(agent)`,
`restart_all`, `isolate`. Costs are measured in tokens, not guessed.

**The covering target is the contamination closure itself.** This is the
correction described in §2(b): the cover used to be "cut every
MaliciousSource → FinalOutput path", while verification required
`Taint(new_graph)` to be empty. A tainted event on no source→output path
satisfied the first and failed the second, so plans were systematically
rejected and every influencing run escalated. Making the closure the covering
target means a selective plan satisfies Step 4 *by construction*.

### 4.6 Step 3 — selective replay

Re-run the pipeline, splicing in the stored output of every event **not** in the
invalidation set and genuinely recomputing the rest. Splicing is what makes this
cheaper than a restart; the invariant that a spliced event is byte-identical to
its original is asserted, not assumed.

### 4.7 Step 4 — verification and escalation

A recovery is accepted only when the recomputed trace has an empty taint set,
no dangling references, and the task still succeeds. On failure the invalidation
set widens: `selective → agent_restart → restart_all → exhausted`.

**This is why safety survives total detection failure.** At a simulated detector
miss rate of 1.0 — the detector flags nothing — CausalLine still records **0%**
unsafe preservation, because the task-level check fails and the ladder climbs.
The baselines have no equivalent backstop and record 50%.

---

## 5. Results

Work preserved vs **B1** (agent-level taint, the baseline we claim to beat),
oracle detector, 30 repetitions, 95% CIs, `data/results/campaign.json`:

| scenario | CausalLine | B1 | gain |
|---|---|---|---|
| A influencing | 42.8% ± 4.1% | 21.1% | **+21.8** |
| A exposed-only | 96.0% ± 4.8% | 21.1% | **+74.9** |
| B influencing | 63.2% ± 0.0% | 57.9% | **+5.3** |
| B exposed-only | 95.6% ± 3.1% | 57.9% | **+37.7** |
| C influencing | 63.2% ± 0.0% | 52.6% | **+10.5** |
| C exposed-only | 96.5% ± 3.2% | 52.6% | **+43.9** |

**Three influencing rows are lower than they were before 15-09-2026, and that is
a safety fix showing up where a safety fix should.** A carrier clearance used to
be written into the trace as `clean / structural / 1.0` on the strength of no
upstream verdict at all; it now inherits what is actually recorded, and the
Coder's contaminated memory write stopped being preserved. One more event
recomputed out of 19 is 5.3 points. Two cells also became deterministic — their
answer no longer depends on which self-report claims the seed happened to get
wrong. Every delta and its cause: `docs/10-remediation.md` §8.2.

The exposed-only column is where the claim pays off most directly: those are
incidents where a poisoned source was *present but unused*, and everything a
reachability baseline discards there is discarded for nothing.

**Zero event-level unsafe preservations** across all 24 campaign cells × 30
repetitions, under four detectors — including a blind control that flags nothing
and a real transformer classifier — and under an adversarial self-reporter that
lies about which source moved its output.

Scenarios: **A** poisoned web page, **B** poisoned tool output, **C** poisoned
memory / compromised inter-agent message. Baselines: **B0** full restart, **B1**
agent-level taint, **B2** topology closure.

---

## 6. Atomic units: the redundancy fix

Leave-one-out cannot see over-determination. With five Researcher findings in
the Coder's context, each individually unnecessary because the other four carry
the same poisoned fact, every finding is reported clean — the contamination
chain silently breaks before the Executor.

Where the trace **records** the redundancy, we merge the sources into one unit
that is removed together and never split, including inside the recursive
halving. Two recorded shapes qualify:

| shape | rule |
|---|---|
| **direct** | A's producing event was influenced by B, and both are in this context (a summary beside its own inputs) |
| **shared ancestor** | A's and B's producing events share an influencing source that is **not** in this context (two findings derived from the same poisoned page) |

The second shape is the one that mattered. The direct shape alone fired in 2 of
48 configurations and moved nothing, because the Coder sees findings and no web
pages — no finding is derived from another.

Measured on the long workflow, A-influencing / oracle:

| | before | after |
|---|---|---|
| work preserved | 11.1% (losing to B1's 14.8%) | **37.0%** (+22.2 over B1) |
| escalations | 1 (`agent_restart`) | **0** (`selective`) |
| unsafe preservation | 0 | 0 |

25 merged units over 82 sources, firing in 11 of 48 configurations, with **zero**
change to any short-workflow cell.

---

## 7. What this does not do

Stated plainly, because several of these were once claimed on numbers that
turned out to be artifacts. Full detail in `docs/06-limitations.md` and
`docs/08-final-report.md`.

- **The analysis does not pay for itself.** `A/N` is 1.23 at the short workflow
  length under a proportional token cost model, and above 1 everywhere measured.
- **Savings do not scale with workflow length.** Measured flat (1.23 → 1.24)
  across a 42% longer trace. An earlier flat-cost model made it look like it
  *degraded*; that was an artifact of pricing calls instead of tokens.
- **Checkpoint GC frees nothing**, at any checkpoint density. Its precondition
  is a prefix provably free of influence, which a run that did useful work never
  has.
- **The attack-probability gate contributes nothing.** Per-node `P` varies, but
  the aggregate saturates to 1.000 at every length measured.
- **Real-model evidence is one channel and five pairs.** A single live run
  against `gemini-3.6-flash` scored 60% agreement with nonce-token ground truth
  and 0 unsafe disagreements — both errors were false positives. Everything else
  is a scripted client.
- **Coincidental redundancy is not handled.** Two sources stating the same fact
  with no recorded link between them are not merged and are not caught.

That last one is not an oversight, and it is worth stating precisely. It is the
known limit of **single-variable counterfactual testing**, and it is why Halpern
and Chockler's actual-causality framework exists. Their AC2 condition quantifies
over *contingencies*: `X = x` is a cause of `φ` when some setting of a subset of
the other variables makes changing `X` change `φ`. Leave-one-out is the special
case where that subset is empty — exactly the case that fails under
over-determination, where two symmetric causes mean neither is a but-for cause.
Finding a witness subset is Σ₂-complete in general, which is the formal statement
of "testing every subset is exponential". A trace-based system can be *sound*
about recorded redundancy and only ever *heuristic* about the rest.

---

## 8. Install and run

The core — tracing, provenance, recovery, and the scripted evaluation — imports
nothing outside the standard library.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install pytest

python -m pytest tests/            # 327 passed, 7 skipped
```

The 7 skips are the Lasso fallback tests, which need numpy. With the analysis
extra installed the suite is **333 passed, 1 skipped**. Both are enforced by CI
in separate jobs, so an unguarded third-party import in the core fails the build.

```bash
pip install -r requirements.txt          # numpy, matplotlib
pip install -r requirements-lock.txt     # exact versions used for the numbers
```

Everything below runs offline against a scripted agent — no API key, no quota:

```bash
python -m src.eval.experiment --detector oracle   # one matrix, all methods
python -m src.eval.campaign                       # 96 cells x 30 reps (~9 min)
python -m src.eval.economics                      # cost model + required figures
python -m src.eval.contract data/runs/ci.jsonl    # trace contract checker
```

The remediation pass (`docs/10-remediation.md`) adds four more, all offline:

```bash
python -m src.eval.action_census          # which recovery actions actually run
python -m src.provenance.scripted_noise   # noise floor of the scripted client
python -m src.eval.robustness             # repeats + control run: what they cost
python -m src.eval.selfreport_value       # what the cheap stage actually buys
python -m src.provenance.upstream TRACE S15   # where a flagged source came from
```

Live experiments need `GEMINI_API_KEY` (copy `.env.example` to `.env`; it is
gitignored). The free tier is **20 requests/day** and one validation channel
costs exactly 20, so a live pass runs one channel per day and results accumulate
across days:

```bash
python -m src.eval.token_validation --dry-run          # costs nothing
python -m src.eval.token_validation --offline          # harness check
python -m src.eval.token_validation --channels web     # ~20 live requests
```

### Real-LLM mode

A second evaluation mode where hosted models drive the agents and the test
scenarios are generated rather than hand-written. It **extends** the scripted
evaluation above; it does not replace it, and every number in §5 is still the
scripted one. Full account: `docs/09-real-llm-evaluation.md`.

Set `NVIDIA_API_KEY_1..3` in `.env`. Three keys are a fallback chain — a key
that answers 429 cools and the next takes over, a key that answers 401 leaves
the rotation — not a way of getting more throughput. One key is enough to run
everything, just less resiliently.

```bash
python -m src.common.nvidia --models              # the registry, free
python -m src.common.nvidia --smoke               # one live call per model
python -m src.eval.llm_scenarios --space          # the 240-point design space, free
python -m src.eval.real_campaign --plan --n 6     # what it would cost, free
python -m src.eval.real_campaign --n 6            # generate, run, score
```

**We choose the structure, the model writes the words.** Scenarios are drawn
without replacement from an enumerated space — injection channel × influencing
or exposed-only × attack style × workflow length × decoy count × source
redundancy — so two tests differ because their causal graph differs, not
because their prose does. The model supplies the payload text, the decoys and a
paraphrase of the task.

**Ground truth is mechanical, never generated.** Every influencing payload
carries a canary token and asks the agent to repeat it, so *the token is in this
output* is a substring test on bytes rather than any model's opinion. The
generating model's own predictions are recorded with `authoritative: false` and
scored as a separate result. Where the token cannot settle a pair, the run says
so instead of guessing.

**Read `docs/09` §9 before quoting a real-LLM number.** Two of the three
specified models were unreachable at measurement time (one retired, one
unresponsive) and nothing was substituted, so these are single-model numbers;
and a run whose payload the model ignored has zero unsafe preservations for
reasons that have nothing to do with the method.

---

## 9. Layout

```
src/common/       shared models, config, LLM clients (Gemini, NVIDIA), prompts
src/tracing/      event logging, call graph, event graph, checkpoints
src/provenance/   source IDs, influence edges, counterfactual checking
src/recovery/     contaminated region, recovery planner, selective replay
src/eval/         attack injection, baselines, metrics, economics, run harness
                  + real-LLM mode: llm_scenarios, real_llm, real_campaign
src/risk/         attack-probability model (per-channel pa)
data/             traces, results, generated suites (gitignored except samples)
paper/            LaTeX / drafts
docs/             design, decisions, limitations, results
```

Read `docs/` before changing anything:

| file | read it before |
|---|---|
| `03-open-issues.md` | proposing a design |
| `05-decisions.md` | asking why something is the way it is |
| `06-limitations.md` | quoting any number |
| `08-final-report.md` | making any claim — it carries the current can/cannot lists |
