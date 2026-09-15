# agent-recovery

Research project by a 3-person B.Tech group. One month to a submittable
conference paper.

## What this project is

Multi-agent LLM systems can be attacked (prompt injection, poisoned tool
output, poisoned memory). Existing work focuses on *detecting* that an
agent was compromised. We start **after** detection.

Given "the Researcher agent is compromised", current practice throws away
everything that agent produced and everything downstream of it. That is
wasteful and often wrong: an agent producing four outputs may have only
one contaminated output, and a downstream agent that merely *received*
that output may never have *used* it.

**Core claim: exposure is not influence.**

Our system finds which specific pieces of work were actually influenced by
contaminated information, preserves the rest, and recomputes only the
affected part from the nearest trusted checkpoint.

## Read before working

- `docs/00-idea.md` — the original full project description
- `docs/01-scope.md` — what is in scope this month, who owns what
- `docs/02-architecture.md` — components and how they connect
- `docs/03-open-issues.md` — known weak points; **read before proposing designs**
- `docs/04-experiments.md` — attack scenarios, baselines, metrics
- `docs/05-decisions.md` — running log of decisions and their reasons
- `docs/06-limitations.md` — what the system does not do, and what the
  measurements actually say; **read before quoting a number**
- `docs/07-completion-report.md` — integration sprint results
- `docs/08-final-report.md` — Final Push (Phases A–D) results, and the
  **current** claims-you-can-make / claims-you-cannot lists; supersedes `07`
  where they disagree
- `docs/09-real-llm-evaluation.md` — the real-LLM mode: NVIDIA models, generated
  test suites, observed ground truth, and **exactly how far the external-validity
  limitation moves**; read §9 before quoting a real-LLM number
- `docs/10-remediation.md` — the phase-gated remediation pass: the comparator
  gap, the nested removability check, carrier clearances, and a fresh campaign
  with every delta explained. **Supersedes `08` on the numbers it re-measures**,
  and its §9 is the current claims-you-can / claims-you-cannot list

## Repo layout

```
src/common/       shared models, config, LLM clients (Gemini, NVIDIA), prompts
src/tracing/      event logging, call graph, event graph, checkpoints
src/provenance/   source IDs, influence edges, counterfactual checking
                  + removability, carrier resolution, upstream candidates
src/recovery/     contaminated region, recovery planner, selective replay
src/eval/         attack injection, baselines, metrics, economics, run harness
                  + real-LLM mode: llm_scenarios, real_llm, real_campaign
src/risk/         attack-probability model (per-channel pa)
data/             traces, results, generated suites (gitignored except samples)
paper/            LaTeX / drafts
docs/             everything above
```

## Ground rules

- Python 3.11+
- Each person owns their folder. Do not edit another person's folder
  without telling them. Shared files (`src/common/`) change only by
  agreement.
- Every non-obvious design decision goes in `docs/05-decisions.md` with a
  one-line reason. The paper needs these.
- Ask before adding a dependency.
- Branch per chunk of work (`feat/event-logger`), merge within ~3 days,
  delete the branch.
- Prefer boring, readable code. This is a research prototype that has to
  be explained in 8 pages, not a product.

## Vocabulary (use these words consistently, in code and paper)

- **event** — one recorded operation (message, tool call, tool response,
  memory read/write, agent output)
- **source** — an incoming unit of information, given an ID (S1, S2, ...)
- **exposure** — a source was present in an agent's context
- **influence** — a source demonstrably changed the agent's output
- **contaminated** — influenced (directly or transitively) by a malicious source
- **clean** — established as not influenced
- **recovery set** — the set of events to invalidate and recompute
- **unsafe preservation** — we called something clean that was actually
  contaminated. This is the dangerous error and must always be reported.
- **carrier** — an event whose output is a copy of an earlier event's, so its
  verdicts are *inherited* rather than established. A carrier record is a
  pointer to an upstream verdict, never a verdict of its own (D-067).
- **removability** — whether redacting a source actually removes its information
  from the prompt. Leave-one-out is only sound when it does (D-066).

## Two evaluation modes — do not mix their numbers

- **scripted** (`src/eval/experiment.py`, `campaign.py`) — `ScriptedClient`,
  whose usage rule we wrote. Ground truth is **known by construction**, per
  (source, event) pair. Deterministic, free, and the only place a per-pair
  accuracy number is possible. Every number in `docs/07` and `docs/08` is from
  here. **Do not weaken it.**
- **real-LLM** (`src/eval/real_llm.py`, `real_campaign.py`) — hosted models via
  `src/common/nvidia.py`, on generated scenarios. Ground truth is **observed**:
  canary-token presence plus the pipeline's own code-path records. Narrower,
  non-deterministic, single-model. See `docs/09`.

A number from one mode does not belong in a table with a number from the other.
Before quoting any real-LLM row, check `payload_landed` — a run the model
ignored has nothing to preserve unsafely, and its zero is arithmetic.

## API keys

All keys are environment-only, loaded from `.env` (gitignored). Never hard-code
one, never print one, never write one into a trace, a result file or a commit.
`GEMINI_API_KEY` for the original live pipeline; `NVIDIA_API_KEY_1..3` for the
real-LLM mode. All NVIDIA traffic goes through `src/common/nvidia.py` — do not
add an API call anywhere else.
