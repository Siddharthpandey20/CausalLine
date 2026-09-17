# 02 — Architecture

## Pipeline (one flow, four subsystems)

```
detector says "sources S3, S7 are malicious" (+ a confidence for each)
        |
   [tracing]      trace store: events, graphs, checkpoints
        |
  [provenance]    influence edges  ->  contaminated region
        |
   [recovery]     invalidation set -> replay -> verification
        |
   workflow continues
```

**The detector's output is source-level, and this line used to say "agent X
compromised".** That was never what the interface carried: `Verdict` in
`src/eval/detectors.py` is a map from *source id* to confidence, and nothing
in it can express "this agent is compromised". The two are not
interchangeable — an agent-level verdict would name a whole agent's output as
suspect, which is baseline B1, while a source-level verdict names an incoming
unit of information and leaves the question of what it influenced to this
system. Corrected 15-09-2026 (Phase 5); see `docs/10-remediation.md`.

### Timing: this is post-hoc, batch recovery

**Recovery begins after the workflow has finished, on a complete trace.** It is
not an online monitor and there is no mid-execution path: nothing pauses a
running workflow, quarantines an agent mid-turn, or re-plans while later agents
are still working. `Verdict.detected_at` and `latency_events` record *when* the
alarm would have fired, and they are used to make the batch problem harder (more
work exists downstream of the injection), not to drive an interrupt.

Why this is the right scope rather than a gap we ran out of time for: the claim
this project is testing is **exposure is not influence**, and establishing
non-influence needs counterfactual replay of an event that has already produced
an output. Before the event completes there is nothing to re-run and nothing to
compare, so the central measurement is not available online. An online variant
would be a different method with a different evidence base, not this one with a
lower latency. It is named in Future work.

## The testbed workflow (fixed for all experiments)

```
User -> Planner -> Researcher -> Coder -> Executor
                      |            |
                   web, db,     code tool,
                   memory       memory
```

Task type: something with a checkable outcome, e.g. "research a library
and write a working script that does X". Checkable outcomes matter — they
give you a task-success metric that does not depend on text matching.

## Three representations

**Call / topology graph** — who called whom, who used which tool. Static,
derived from the pipeline definition. Useful for context and for the
coarse baseline. Cannot by itself determine contamination.

**Event graph** — every operation as a node, with parent links. This is
the execution history. An event:

```python
Event(
    id,            # e.g. "e0042"
    agent_id,      # "researcher"
    kind,          # message | tool_call | tool_response | memory_read |
                   # memory_write | agent_output | plan | decision
    parents,       # [event ids]
    inputs_ref,    # references into the content store
    exposures,     # [source ids] present in this agent's context (D-011)
    output_ref,
    tool_id,       # optional
    timestamp,
)
```

**Provenance / influence graph** — the important one. Nodes are sources
and outputs; edges mean *this source influenced this output*. Edges carry
how they were established and how confident we are.

```python
InfluenceEdge(
    source_id,     # "S3"
    target_event,  # "e0042"
    method,        # self_report | counterfactual | assumed
    confident,     # bool
)
```

Exposure is recorded separately from influence. Every source in an
agent's context is an exposure. Only some exposures become influence
edges. **The gap between those two sets is the entire contribution.**

## Establishing influence

Two-stage, cheap-then-check:

1. **Self-report.** Ask the agent to state which inputs it used. Fast,
   free-ish, unreliable on its own. Produces candidate edges.
2. **Counterfactual replay.** Remove the suspect source, re-run that one
   event, compare outputs. Same -> no influence. Different -> influence.
   Slow but it is evidence rather than a claim.

Run counterfactual only where it matters: on sources the detector flagged,
and on events expensive enough that avoiding a rerun pays for the check.
Everywhere else, fall back to `assumed` (i.e. treat as influenced).

Anything we cannot establish confidently is treated as **contaminated**.
Security first.

## Contamination propagation

```
mark malicious source
   -> walk influence edges only (never plain topology edges)
   -> transitive closure = contaminated region
```

An event downstream of a contaminated event is contaminated **only if an
influence edge connects them**. That is the whole difference from the
baseline.

## Recovery

```
contaminated region
   -> invalidation set (contaminated events + anything depending on them)
   -> earliest safe checkpoint covering that set
   -> replay only invalidated events, in dependency order
   -> splice new outputs back into the trace
   -> verify
   -> on failure: escalate to agent-level, then full restart
```

Memory writes made by contaminated events must be rolled back too.

## Checkpoints and storage

Do not store everything. Store:

- **always:** event metadata (ids, parents, agent, tool, refs) — small
- **always:** source content (needed for replay) — moderate
- **at checkpoints:** full agent state — the expensive part
- **never:** intermediate token-level detail

Checkpoint policy for v1: after every agent boundary. Measure the overhead
and report it. If it is bad, that measurement is itself a paper finding.

## Shared data models

Live in `src/common/`. Agreed in week 1, then frozen. Everything else
depends on them, so a change here breaks all three people at once.

## Execution-time provenance is independent of attribution

**Execution-time provenance capture is independent of attribution. Attribution
is a consumer of recorded execution facts and may be run inline or post-hoc.**

Verified across chain and fan-out topologies, all three attack channels (web,
memory, agent_message), benign and attacked, with attribution installed and
absent: `tests/test_provenance_independence.py`.

### Execution facts — identical whether or not attribution runs

| field | written by |
|---|---|
| event id, kind, agent id, ordering | the pipeline, as it performs the operation |
| `parents` | the pipeline |
| `exposures` | the pipeline |
| `tool_id` | the pipeline |
| source id, kind, `derived_from`, `origin_event` | the pipeline |
| **structural** check records (`record_structural`) | the pipeline, read off the code path |

### Attribution-derived — permitted to differ, and expected to

| field | written by | note |
|---|---|---|
| influence edges | the attributor / `refine_for_verdict` | the estimator's output |
| `self_report` check records | the attributor | a claim, not evidence |
| `counterfactual` check records | the estimator | evidence |
| `assumed` check records | the fallback | never a clearance |
| **carrier** check records (`record_carrier`) | the pipeline, but **inheriting an upstream verdict** | see below |

### The one that looks like an execution fact and is not

`record_carrier` writes records with `method="structural"`, but a carrier record
is **a pointer to an upstream verdict, not a verdict of its own** (D-067). It is
written during execution and depends on what attribution has concluded so far,
so it legitimately differs between a run with attribution and a run without.
`src/provenance/carriers.py` re-resolves these pointers at read time for exactly
that reason.

**The two kinds are distinguished only by `CARRIER_NOTE` ("carries output of")
appearing in the record's notes.** Counting them together is what produced the
false report in `docs/gate1/final_cost_direction.md` §6.1 that the pipeline's
provenance depends on attribution; split correctly, the code-path structural
records are identical (16 = 16) and only the carrier records differ.

### A testbed caveat, not an architecture property

In the scripted evaluation the attributor is handed the **same `ScriptedClient`
instance** as the pipeline, because `ScriptedClient._answer_self_report` answers
truthfully by looking the audited call up in its own `by_prompt` table. Sharing
the instance means the attributor's calls advance that client's state, so the
stored prompt/output *content* differs between the two runs. Giving attribution
its own client makes the harness fabricate ("I used nothing" about everything)
and degrades recovery across every seed tested. A real model shares an endpoint,
not a state table, so this coupling does not exist in production.
