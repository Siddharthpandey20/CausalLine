# Issue #20 — the correction, and every number it moves

> **This document supersedes every CausalLine `work_preserved` figure in
> `docs/07`, `docs/08`, `docs/10`, `docs/11` and `docs/gate1/` on the cells it
> re-measures.** Nothing else in those documents changes: no baseline number
> moves, no recovery-success rate moves, and no unsafe-preservation count
> moves. The originals are left in place rather than edited away — they were
> correct measurements of a system with a defect, and the defect is part of the
> record.

## 1. The defect

`contaminate()` walks two relations:

```
influence      source -> event    this source changed this output
derived_from   event  -> source   this source IS that event's output
```

A third was never recorded. **A source does not appear from nowhere.** Some
event retrieves it — a `tool_response` returning web pages, a `memory_read`
returning a stored value, a hand-off `message` carrying a planted instruction —
and that event's own `output_ref` stores the retrieved text verbatim. The
source is then logged with `origin_event` pointing back at it.

But the source does not exist when that event is logged. So it is not in the
event's `exposures`; so `record_structural()` writes no record for the pair; so
the contamination walk never considers it. **The event sits outside the
recovery region holding the payload, and every method preserves it.**

The verdict on those events was not wrong. It was *absent*, which is worse:
every consumer looks for a wrong verdict, and none looks for a missing one.

## 2. How general it is

Not memory-specific. One cause, every ingestion point — measured on the chain
pipeline before the fix:

| scenario | channel | event | holds | in region? |
|---|---|---|---|---|
| A | web | `e0005` researcher/`tool_response` | S3 | **no** |
| B | memory | `e0012` coder/`memory_read` | S14 | **no** |
| C | agent_message | `e0011` researcher/`message` | S13 | **no** |

and on the 56-agent mixed workflow as **5 unsafe preservations** in the large
regime — five `memory_read` events holding the poisoned policy text.

## 3. The fix

`record_ingestion()` in `src/provenance/attribution.py` records a **structural,
confident influence edge** from each materialised source to the event that
materialised it. It is called at every ingestion site in both pipelines.

Two alternatives were rejected on evidence:

- **a check record** — `Trace.validate()` rejects one naming a source that was
  never in the event's context, and that invariant is right: a check claiming
  to have examined what the agent never saw is a fabrication.
- **`derived_from`** — `validate()` enforces one wrapping source per event, so
  a two-key memory read would fail it.

It is **precise, not blanket**. In the mixed workflow the injected note beside
a clean document gets no edge, because its content never enters the tool
response's output. A "contaminate the origin event" rule would have discarded
tool responses holding nothing but clean data — more conservative, and wrong.

The change is in provenance **recording**. `contamination.py`, `causalline.py`,
`planner.py`, `gate1.py`, `baselines.py` and the task checker are untouched.

## 4. What moved, exactly

Scripted matrix, 96 cells, 30 repetitions, same seeds, before and after.

**10 cells moved. All of them CausalLine. All downward.**

| detector | scenario | variant | published | corrected | change |
|---|---|---|---:|---:|---:|
| heuristic | A | influencing | 42.8% | **38.2%** | -4.6 pts |
| heuristic | C | influencing | 63.2% | **57.9%** | -5.3 pts |
| oracle | A | exposed_only | 95.6% | **90.4%** | -5.3 pts |
| oracle | A | influencing | 42.8% | **38.2%** | -4.6 pts |
| oracle | B | exposed_only | 95.6% | **90.4%** | -5.3 pts |
| oracle | B | influencing | 63.2% | **57.9%** | -5.3 pts |
| oracle | C | exposed_only | 96.5% | **91.2%** | -5.3 pts |
| oracle | C | influencing | 63.2% | **57.9%** | -5.3 pts |
| pessimistic | B | exposed_only | 63.2% | **57.9%** | -5.3 pts |
| pessimistic | B | influencing | 63.2% | **57.9%** | -5.3 pts |

The headline figure most often quoted — CausalLine under the oracle detector on
influencing runs — moves from **56.4% to 51.3%**.

### What did NOT move

| quantity | before | after |
|---|---|---|
| cells where any baseline's `work_preserved` changed | — | **0** |
| cells where `recovery_success_rate` changed | — | **0** |
| cells recording any unsafe preservation | 8 | **8** (identical set, all baselines under weak detectors) |
| CausalLine cells with any unsafe preservation | 0 | **0** |

Baselines are unchanged because B1 and B2 already discarded these events: they
work from `origin_event` downstream, so the ingestion event was always inside
their discard set. The defect only ever cost *CausalLine*, by letting it keep
something it should not have.

## 5. How to read the direction of the change

CausalLine preserves **less** work than previously published, and that is a
correction rather than a regression. The hard constraint is *preserved correct
work must not be sacrificed*. Nothing correct was sacrificed: the events now
discarded were the ones holding the attack. The earlier figure counted
contaminated work as preserved.

The two quantities are not independent. **Any mechanism that preserves more
work is mechanically a candidate for preserving something it should not**, so
`work_preserved` and `unsafe_preservations` have to be read together, and a
gain accompanied by a non-zero unsafe count is not a gain. D-086 made the same
point about preserved versus *delivered* work; this is its safety-side twin.

The sharpest illustration is the 56-agent large regime, which read

| | work preserved | unsafe |
|---|---:|---:|
| B2 | 14.5% | 0 |
| CausalLine, **before** the fix | **31.7%** | **5** |
| CausalLine, after the fix | 14.5% | 0 |

**The entire +17.2-point margin was the defect.** Had the campaign stopped at
that run, the headline would have been "CausalLine beats the topology closure
by 17 points under heavy contamination" — false in the worst available way, a
safety failure reported as a performance gain. That number must not be quoted
anywhere.

## 6. Regression coverage

`tests/test_issue20_memory_provenance.py`, 17 tests:

| | case |
|---|---|
| A | minimal memory-read poisoning, built directly on `TraceLogger` |
| B | chain scenarios A, B and C, all three channels |
| C | the 56-agent workflow's memory channel, all three regimes |
| D | a clean memory read, and an unrelated event, staying clean |
| E | poisoned memory read with no call-graph ancestry to the poison |

They assert the general property — *no event may store a flagged source's
content and sit outside the recovery region* — rather than any scenario's
event ids. Every id is read back from the object that created it, so a topology
change cannot make them vacuous. Case A includes a **negative control** that
fails if the fix ever becomes a no-op.

`tests/test_mixed.py` additionally cross-checks that the chain pipeline and the
mixed pipeline agree, since they were fixed together.
