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
