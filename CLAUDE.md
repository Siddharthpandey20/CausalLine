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

## Repo layout

```
src/tracing/      event logging, call graph, event graph, checkpoints
src/provenance/   source IDs, influence edges, counterfactual checking
src/recovery/     contaminated region, recovery planner, selective replay
src/eval/         attack injection, baselines, metrics, run harness
data/             traces, results (gitignored except small samples)
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
