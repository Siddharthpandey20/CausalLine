# 01 — Scope

One month. Three people. Target output: a conference paper with a working
prototype and real numbers.

## Guiding principle

Build **every component thin and connected end-to-end**, and evaluate
**one** claim deeply. The deep claim is: *exposure is not influence, and
exploiting that preserves substantial work at acceptable risk.*

A system paper needs the whole pipeline to exist. It does not need every
part to be state of the art.

## In scope

- Testbed: one fixed multi-agent pipeline
  (Planner -> Researcher -> Coder -> Executor) with web, database, code,
  and memory tools
- Event logging for every operation, with parent links
- Call/topology graph and event graph derived from the log
- Source IDs on all incoming information
- Influence edges from source to output, obtained two ways:
  self-reported attribution (cheap) and counterfactual replay (checked)
- Contamination propagation along influence edges only
- Recovery planner: contaminated set -> invalidation set -> checkpoint choice
- Selective replay from checkpoint, with results spliced back
- Recovery verification, with escalation to coarser recovery on failure
- Conservative fallback: unknown provenance is treated as contaminated
- Three attack scenarios, three baselines, four metrics (see `04-experiments.md`)

## Out of scope (say so explicitly in the paper)

- Attack **detection** — we assume a detector exists and is correct
- Cross-session / long-lived shared memory across separate workflows
- Adaptive attackers who know our method and try to evade it
- More than one pipeline topology
- Proving minimality of the recovery set (we claim *small and safe*,
  measured — not *minimal*, proved)
- Human agents, tool sandboxing, or runtime enforcement

These become "Future work". Listing them protects you from reviewers.

## Ownership

Divide by folder so git conflicts stay rare.

| Person | Folder | Owns |
|---|---|---|
| TODO | `src/tracing/` | pipeline, event logger, graphs, checkpoints |
| TODO | `src/provenance/` | source IDs, influence edges, counterfactual check |
| TODO | `src/recovery/` | recovery planner, selective replay, verification |
| whoever frees up first | `src/eval/` | attack injection, baselines, metrics |

Shared data models live in `src/common/` and are agreed in week 1, then
frozen. Changing them mid-project is the main way this schedule breaks.

## Timeline

**Week 1 — trace layer**
Pipeline runs end to end. Every operation is logged as an event with
parents. Call graph and event graph build from the log. Checkpoints
serialize agent state at event boundaries.
*Exit test:* run the pipeline, get a trace file, rebuild both graphs from it.

**Week 2 — provenance and contamination**
Sources get IDs. Influence edges get produced by self-report and verified
by counterfactual replay. Marking a source malicious yields a contaminated
region.
*Exit test:* inject a bad source, get back a set of contaminated events
that is smaller than "everything downstream".

**Week 3 — recovery**
Planner picks the invalidation set and the checkpoint. Selective replay
re-runs only affected events. Verification re-checks the new outputs.
*Exit test:* a poisoned run recovers to a clean result without a full restart.

**Week 4 — evaluation and writing**
Run all scenarios and baselines. Produce tables. Write.
Writing actually starts day 8, not week 4 — draft sections as parts land.

## Open decisions

- [x] LLM API: Gemini
- [x] Which Gemini model for the agents: `gemini-3.6-flash`, temperature 0 (D-004)
- [x] Rate limit / quota tier: free tier is 20 requests/day/model, measured. The plan does not fit in it — see D-017, needs a group decision this week
- [ ] Target conference and deadline: to be assigned by our professor
- [ ] Definition of "recovery succeeded": TODO — see 05-decisions.md
- [ ] Number of runs per scenario: TODO (target ~30)