# 05 — Decisions

Running log. Append, never rewrite. Every entry: what we chose, what we
rejected, why. In week 4 the paper's method section gets written from this
file, and you will not remember the reasons otherwise.

Format:

```
## D-004  LLM API and model
Date: 26-08-2026
Decided by: all three
Choice: Gemini API for all agents in the testbed.
Reason: available to us at low cost. Model tier TBD.
```

---

## D-001  Scope: full pipeline, shallow depth, one deep result
Date: TODO
Decided by: all three
Choice: build every component thin and connected end to end; evaluate the
exposure-vs-influence claim deeply.
Rejected: cutting to a single component; building every component deeply.
Reason: a system paper needs the whole pipeline to exist, but one month
cannot support depth everywhere. One measured claim is enough for
acceptance.

---

## D-002  Terminology: "small safe recovery set", not "minimum"
Date: TODO
Decided by: all three
Choice: avoid claiming minimality anywhere in code, docs, or paper.
Rejected: "minimum recovery set".
Reason: minimality is not provable when influence edges are estimates.
Claim only what is measured.

---

## D-003  Definition of "recovery succeeded"
Date: TODO
Decided by: TODO
Choice: TODO — decide in week 1, do not defer
Rejected: exact output matching
Reason: replay is non-deterministic; identical state is not achievable.
Candidates to pick from: (a) task-level success on a checkable outcome,
(b) semantic equivalence judged by an LLM with a fixed rubric, (c) both,
reporting agreement between them.

---

## D-004  LLM API and model
Date: 26-08-2026
Decided by: all three
Choice: Gemini, `gemini-3.6-flash`, temperature 0. Model name is read from
`GEMINI_MODEL` in `.env` and recorded in every trace header, so a run's
numbers can always be tied to the model that produced them.
Amended 26-08-2026, same day: the original choice was `gemini-2.5-flash`,
which returns 404 for API keys created now -- the API's own error names
`models/gemini-3.6-flash` as the replacement. Recorded rather than quietly
edited because it is a finding in its own right: a one-month project can have
its model retired underneath it mid-schedule. Any run traced before this
amendment carries the old model name in its header and is not comparable with
runs after it. Consequence for docs/04 run hygiene: if we have already
collected numbers on a scenario, all methods on that scenario must be re-run
on the new model, not just the ones we happen to re-run next.
Reason: Flash tier keeps the budget survivable. Counterfactual replay
multiplies call count -- one flagged source on one event is one extra call,
and docs/04 asks for three repeats across ~30 runs per scenario -- so the
per-call price is the constraint that matters, not the per-call quality.

---

## D-005  Target conference and deadline
Date: TODO
Decided by: TODO
Choice: TODO
Reason: TODO — page count decides how much evaluation is required.

---

## D-006  Shared data models frozen
Date: TODO
Decided by: all three
Choice: `src/common/` models agreed in week 1, then changed only by
agreement of all three.
Reason: all three subsystems depend on them; a mid-project change breaks
everyone at once and burns days.

---

<!-- append new decisions below -->

## D-007  Automatic parent links are same-agent only
Date: 26-08-2026
Decided by: proposed with the trace layer, needs group sign-off
Choice: `TraceLogger.log_event(parents=None)` links the new event to the
previous event logged by the same agent. Cross-agent links (Planner ->
Researcher) must be passed explicitly; `parents=[]` marks a root event.
Rejected: linking to the previous event overall (whichever agent produced it).
Reason: chronological adjacency is not derivation. Auto-linking across
agents would manufacture edges that look like data flow, and contamination
that propagates along a manufactured edge is exactly the baseline error we
claim to avoid. Guessing wrongly here would flatter the baseline and could
also hide a real edge.

---

## D-008  Trace file format: one JSONL file per run, tagged records
Date: 26-08-2026
Decided by: proposed with the trace layer, needs group sign-off
Choice: one `.jsonl` per run under `data/runs/`. Each line is a JSON object
tagged with `"record"`: `meta`, `source`, `event`, or `influence`. Lines are
flushed as written.
Rejected: separate files per record type; a single JSON document written at
the end; SQLite.
Reason: append-only means a crashed or attacked run still leaves a readable
trace, and one file per run keeps a run self-contained for `data/runs/`
hygiene (docs/04). Both graphs rebuild from this one file. Revisit only if
trace size becomes a measured problem (open issue #8).

---

## D-009  Source content is stored inline; event content is stored by reference
Date: 26-08-2026
Decided by: proposed with the trace layer, needs group sign-off
Choice: `Source.content` holds the text. `Event.inputs_ref` / `output_ref`
hold opaque content-store keys, not text.
Rejected: inlining event content too; referencing source content too.
Reason: matches the storage policy in docs/02 — source content is always
needed for counterfactual replay, event content is only needed at
checkpoints. The content store itself is not built yet; refs are opaque
strings until it is.

---

## D-010  No content store yet; refs stay opaque
Date: 26-08-2026
Decided by: all three
Choice: `Event.inputs_ref` and `Event.output_ref` stay opaque strings. No
content store is built until selective replay actually needs event content.
Rejected: building the content store in week 1 alongside the trace layer.
Reason: nothing in weeks 1-2 reads event content. The graphs are built from
metadata, and counterfactual replay reads `Source.content`, which is stored
inline (D-009). Building a store now would be guessing at the interface
replay wants. The field names are already in the frozen schema, so adding
the store later does not change the models.
Consequence: `data/runs/*.jsonl` is not yet enough to replay a run. It is
enough to rebuild both graphs, which is the week-1 exit test.

---

## D-011  Exposure is a field on Event, recorded at logging time
Date: 26-08-2026
Decided by: all three
Choice: `Event.exposures` holds the source ids present in the agent's
context for that operation. Written by whoever runs the agent, at the moment
the call is made.
Rejected: a separate exposure record; deriving exposure inside
`src/provenance/` after the run.
Reason: exposure is an observation about the prompt, not an analysis result,
and it cannot be reconstructed once the run is over. It also is not a
provenance detail -- the exposure/influence gap *is* the paper's claim, so it
belongs in the trace next to the operation it describes. Keeping it on Event
means a trace file alone contains both sets, and figure 2 (exposure graph vs
influence graph, docs/04) can be drawn from one file.

---

## D-012  Work is counted in events, never in sources
Date: 26-08-2026
Decided by: all three
Choice: the unit for every metric in docs/04-experiments.md is the **event**.
An agent output appears twice in a trace -- as the event that produced it
(e0006) and as the source a later agent consumed (S6). Those are one piece of
work. The source carries `Source.derived_from = "e0006"` and is never counted.
`Trace.work_units()` is the single definition; metrics start there rather than
each deciding for itself.
Rejected: counting sources; counting both; counting only agent_output events.
Reason: "work preserved" has to mean recomputation avoided, and recomputation
happens per event. A source derived from an event costs nothing to reproduce
once that event exists, so counting it would inflate work preserved -- for us
*and* for the baselines, but unevenly, because our method preserves more of
exactly the derived kind. That is a silent way to manufacture our own result.
`Trace.validate()` now rejects two sources wrapping one event.
Note: `derived_from` is also the edge contamination crosses between agents.
If e0006 is contaminated then S6 is contaminated, and any event S6 influences
is contaminated. Without the field that link lives only in the call graph,
which is exactly what we refuse to propagate along.
Note: the same rule applies to tokens. Replay cost is charged to the event
that is re-run; wrapping its output for a consumer costs nothing extra.

---

## D-013  Gemini over stdlib HTTP, no SDK dependency
Date: 26-08-2026
Decided by: proposed with the pipeline, needs group sign-off
Choice: `src/common/llm.py` posts to the `generateContent` REST endpoint with
`urllib.request`. No `google-genai`, no `requests`, no `python-dotenv`.
Rejected: the `google-genai` SDK; the deprecated `google-generativeai`
package that happens to be installed on one of our machines.
Reason: the ground rules say ask before adding a dependency, and this needs
about sixty lines. It also keeps retry, timeout and token extraction in code
we can read, which matters because rate-limit behaviour under counterfactual
replay is something we have to measure and report, not just survive.
Revisit if we need streaming, function calling, or multimodal input; the
transport is one method (`GeminiClient._post`) and swapping it is contained.
Note: `google-generativeai` is deprecated upstream. If we ever do adopt an
SDK it must be `google-genai`.

---

## D-014  Web and database tools read fixtures, not the live internet
Date: 26-08-2026
Decided by: proposed with the pipeline, needs group sign-off
Choice: the web tool ranks a canned corpus in `src/tracing/fixtures/`; the
database tool reads a fixture dict. `src/eval/` injects poisoned pages by
passing a modified corpus to `Tools`, without touching tool code.
Rejected: a real search API.
Reason: two of our claims depend on it. Ground truth is known by
construction only if we author what the tools return (open issue #3), and
counterfactual replay only means anything if re-running an event without one
source reproduces everything else exactly -- a live search result that
changes between the original call and the replay would silently look like
influence. Reproducibility here is a requirement, not a convenience.
Consequence: state plainly in the paper that tool outputs are a fixed corpus.
A reviewer will otherwise assume live retrieval and ask about drift.

---

## D-015  Thinking minimised (thinkingLevel "minimal")
Date: 26-08-2026
Decided by: proposed with the pipeline, needs group sign-off
Choice: every call sets `thinkingConfig.thinkingLevel = "minimal"`. The
setting is recorded in the trace header, and `thoughts_tokens` is recorded
per call whatever the setting says.
Rejected: leaving the Flash model's default thinking on.
Reason: thinking tokens are billed and would inflate the cost metric with
work that is invisible in the trace, and variable-length internal reasoning
is a second source of run-to-run variation on top of the one open issue #2
already forces us to handle. `thoughts_tokens` is still recorded per call, so
if we turn thinking back on for a scenario the cost stays separable.
Amended 26-08-2026, same day: the mechanism changed, the decision did not.
`thinkingBudget: 0` is what gemini-2.5-flash accepted; gemini-3.6-flash
rejects it with 400 INVALID_ARGUMENT, which is what broke the first live run
after the model change (D-004). Bisecting the request body one field at a
time found `thinkingBudget` to be the only offending field -- JSON response
mode is fine. This model takes `thinkingLevel` instead, one of minimal, low,
high. "minimal" returned 0 thought tokens on both a trivial prompt and a
realistic Researcher prompt, so the intent of this decision survives intact.
Measured, for the paper: with thinking left at its default, "Reply with the
word ok." cost 98 tokens of which 90 were thoughts. Roughly 92% of the spend
on that call would have been invisible in the trace. That is the size of the
distortion this decision avoids, and it is worth one line in the cost section.
Consequence if a future model will not go to zero: the cost metric would
carry tokens that appear nowhere in the event graph. It stays *separable*
because `UsageRecord.thoughts_tokens` is recorded per call regardless, so the
honest move then is to report thought tokens as their own column rather than
fold them into the totals. Do not quietly leave them in.
`python -m src.common.llm --smoke` now checks the request contract with one
call, so the next model change costs one call to diagnose, not a whole run.

---

## D-016  One API call per Researcher finding
Date: 26-08-2026
Decided by: proposed with the pipeline, needs group sign-off
Choice: the Researcher answers each planned question in its own call,
producing one `agent_output` event per finding.
Rejected: one call returning all findings as a list.
Reason: work preserved is counted per event (D-012), so replay cost has to be
countable per event too. If one call produced four findings, "the cost of
recomputing one finding" would be undefined, and that number is half of open
issue #7. It also makes selective replay real rather than notional: replaying
one contaminated finding is one call, not a re-run of all four.
Cost: more prompt tokens overall, since the sources are re-sent per question.
That overhead is real and belongs in the cost table rather than being hidden.

---

## D-017  Free-tier quota is 20 requests per day, and the plan does not fit in it
Date: 26-08-2026
Decided by: NOT DECIDED -- needs all three, this week
Choice: open. The measurement is not open, and it is the reason this needs a
decision now rather than in week 3.

Measured against the live API on 26-08-2026:

```
quotaId    GenerateRequestsPerDayPerProjectPerModel-FreeTier
quotaValue 20
model      gemini-3.6-flash
```

Per **day**, per model, per project -- not per minute. Rejected requests
appear to count against it, so a burst of 429s makes it worse rather than
better.

The arithmetic against docs/04:

```
one pipeline run                       6 requests  (planner 1, researcher 3, coder 2)
  -> 3 runs per day, absolute ceiling
30 runs x 3 scenarios                540 requests  original runs only
B0 full restart baseline             540 requests  it re-runs everything
counterfactual replay                 1 request per flagged source per event,
                                      x3 repeats for non-determinism (docs/04)
```

Even before counterfactual checks -- the thing the paper is actually about --
that is well over 1000 requests, or 50+ days of free-tier quota. We have one
month, and week 2 is where the call count starts multiplying.

Options, for the group to choose between:
1. Enable billing on the API project. Costs money; makes the plan as written
   feasible. Flash tier pricing is low, and D-015 already removed thinking
   tokens, which were 92% of spend on a trivial call.
2. Cut the experiment: fewer runs per scenario, fewer scenarios, or
   counterfactual checks on a sampled subset with the sampling reported.
   Cheaper, and honest if we say so, but it weakens the headline numbers and
   open issue #3 already wants ~30 labelled runs per scenario.
3. Spread runs across several API projects or keys. Works, but it is quota
   evasion, it makes runs non-comparable across keys, and it is not something
   to put in a paper.

Recommendation: option 1, with option 2's sampling as the fallback if the
budget is refused. Whatever is chosen, write the token and request counts into
the paper -- open issue #7 asks for cost numbers, and "the method needed N
requests" is exactly the kind of number a reviewer wants.

Consequence for the code, already applied: the client now separates the
per-minute limit (retryable, honours the server's suggested delay) from the
per-day limit (fatal, `QuotaExhausted`). Backing off against a daily cap wasted
152 seconds and several requests before this.

---

## D-018  Checkpoint payloads live in a sidecar file, not in the trace
Date: 26-08-2026
Decided by: proposed with the trace layer, needs group sign-off
Choice: two files per run.

```
data/runs/run1.jsonl              events, sources, influence, usage
data/runs/run1.checkpoints.jsonl  agent state and memory snapshots
```

Rejected: checkpoints as another record type inside the trace; one directory
of numbered checkpoint files.
Reason: it is the storage policy from docs/02-architecture.md made literal.
Metadata is always kept and is small; full agent state is "the expensive
part" and is kept only at checkpoints. Keeping them in one file would mean
every tool that reads a trace pays to parse state it does not want, and the
overhead measurement open issue #8 asks for would need the two separated
anyway. `overhead()` reports the split directly.
Measured on an 18-event run: trace 11.1 kB, checkpoints 4.0 kB, so
checkpoints are 26% of stored bytes at the v1 policy of one per agent
boundary. That is the number to re-measure on real runs and report.
Consequence: D-008's "one file per run" now reads "one trace file per run".
A run is self-contained in a directory, not in a file. Both graphs still
rebuild from the trace file alone, which is what the week-1 exit test asks.

---

## D-019  Record/replay cassettes for development, never for results
Date: 26-08-2026
Decided by: proposed with the pipeline, needs group sign-off
Choice: `src/common/cassette.py` records live responses to a JSONL cassette
and replays them offline. Cassettes are committed. A replayed run writes
`"cassette": "replay"` into its trace header.
Rejected: everyone spending their own quota to see a real trace; mocking
responses by hand.
Reason: 20 requests a day across three people (D-017) does not allow each of
us a real trace to develop against. One recorded run replays indefinitely for
free, and it is real model output rather than something we invented, so
provenance and recovery are built against text the model actually produced.

**The rule, and it is not negotiable:** no number from a replayed run goes in
the paper as a measurement. Token counts replay faithfully because they are
the counts the model returned, but the request did not happen, and
`latency_s` and `attempts` are recorded values that describe the original
call. Every table in docs/04 comes from runs with no `cassette` key in the
header. The header marking exists so that this is checkable rather than
remembered.
Note: replay needs no API key at all, so a teammate can clone the repo and
work immediately. A cassette miss raises rather than falling through to a
live call -- silently spending the day's quota on a changed prompt is exactly
the failure this is meant to prevent.

---

## D-020  Socket-level errors are retryable; timeout raised to 120s
Date: 31-08-2026
Decided by: proposed with the first successful live run, needs group sign-off
Choice: `GeminiClient._post` catches `OSError` after `urllib.error.URLError`
and converts it to `Retryable`. `Settings.timeout_s` goes 60 -> 120.
Rejected: leaving the timeout at 60 and relying on retries.

Reason: the first live run of the full pipeline died four calls in, on the
third Researcher finding, with a bare `TimeoutError` and a stack trace. A read
timeout that happens *after* the connection is established is an `OSError` but
**not** a `URLError`, so the `except urllib.error.URLError` clause never saw
it. Two things followed from that one gap, and both are worse than the timeout
itself:

  * it bypassed the retry loop entirely -- `max_attempts=5` was configured and
    never used, because `with_retry` only retries `Retryable`
  * it bypassed the `except (LLMError, RuntimeError)` handler in
    `pipeline.__main__` too, so the run printed a traceback instead of the
    partial-trace report that D-008's append-only format exists to make
    possible. `OSError` is now in that tuple as well.

The timeout goes up rather than the retry count because of D-017: under a
20-requests-per-day quota, **waiting is free and retrying costs a request**.
120s is far past any plausible generation time for this model at
`max_output_tokens=2048` -- the call that stalled had produced ~700 output
tokens in ~4s on the two calls either side of it, so this was a network stall,
not slow generation.

Cost of the lesson, for the record: the aborted attempt spent 5 requests (4
answered, 1 timed out) of that day's 20.

Consequence for the cost metric, and it is a real one: **a timed-out request
costs quota but contributes no tokens to the trace.** The server generated an
answer we never received. So "requests spent" and "calls recorded in the
trace" are not the same number, and open issue #7 wants the first.
`UsageRecord.attempts` captures retries within a call that eventually
succeeded; a call that fails outright records nothing at all. When we report
request counts in the paper, count attempts, and say that failed attempts are
included.
Note while fixing: `load_settings()` restates every default a second time
alongside the `Settings` field defaults, so changing the dataclass default
alone does nothing. Both were updated and a comment now says so. Worth
collapsing if it bites anyone again.

---

## D-021  A `--record` run is a live run; only `"cassette": "replay"` disqualifies
Date: 31-08-2026
Decided by: proposed with the first successful live run, needs group sign-off
Choice: refine D-019's header rule. Paper numbers may come from a trace whose
header says `"cassette": "record"`. They may never come from one that says
`"cassette": "replay"`.
Rejected: D-019's literal wording, "every table comes from runs with no
`cassette` key in the header".

Reason: D-019 was written before a recording run had ever succeeded, and its
rule reads on the presence of the key rather than on its value. Taken
literally it disqualifies the very run that produces the cassette -- a run in
which every request was genuinely made, every token genuinely spent, and every
latency genuinely measured. Recording is a side effect of that run, not a
substitute for it. Enforcing the rule as written would have meant spending
another 6 requests to re-run the identical pipeline with recording off, which
buys nothing and costs a third of a day's quota.

The thing D-019 is actually protecting against is a number that describes a
request that never happened. That is exactly and only the `replay` case. The
distinction is checkable in the header either way, which was D-019's real
point.
Unchanged: `latency_s` and `attempts` from a replayed run mean nothing, a
cassette miss still raises rather than falling through to a live call, and no
replayed number goes in a table.

---

## D-022  Checkpoint overhead re-measured on a real run: 56%, not 26%
Date: 31-08-2026
Decided by: measurement, no choice to make
Choice: none. D-018 asked for this number to be re-measured on real runs
rather than on the fake pipeline, so here it is.

Measured on `data/runs/run1.jsonl`, the first complete live run, 18 events,
4640 tokens, v1 checkpoint policy of one per agent boundary:

```
trace         12,493 B
checkpoints   16,080 B   across 4 checkpoints
checkpoint share   56% of stored bytes
```

D-018 measured 26% on a run of the same event count. The gap is entirely real
model output: a checkpoint carries `outputs`, the accumulated text of every
event so far, and the fake pipeline's stub text is a fraction of the length of
what the model actually writes. Checkpoints therefore grow with the *square*
of run length under the v1 policy -- each one re-serialises every output
before it -- while the trace grows linearly.

Consequence: 56% is the number to quote for open issue #8, not 26%, and the
v1 policy is the thing to name as the cause. Do not fix it yet; the quadratic
growth is only worth engineering away if run length grows past this testbed's
18 events, and "we measured the naive policy and it cost 56%" is a more useful
sentence in the paper than a policy tuned before anyone needed it.

---

## D-023  A fourth option for D-017: run the bulk locally
Date: 31-08-2026
Decided by: NOT DECIDED -- this adds an option to D-017, it does not close it
Choice: open. D-017 offers three ways out of the quota problem (enable
billing, cut the experiment, juggle keys). There is a fourth that is not in
that list and should be, because it is cheaper than option 1 and more honest
than options 2 and 3.

**Run the bulk of the experiment on a local model, and keep the hosted model
for a spot-check subset.**

A local model served on the machine has no request quota at all. The 1000+
requests D-017 computes stop being a budget problem and become a wall-clock
problem, which we can absorb -- week 4 is evaluation and the runs are
scriptable.

Why this is consistent with what we already decided rather than a reversal:
D-004 chose the Flash tier explicitly because "the per-call price is the
constraint that matters, not the per-call quality". That reasoning points at
a local model more strongly than it points at Flash. We are not measuring how
good an agent is. We are measuring whether influence can be separated from
exposure, and that claim is about the method, not about the model's ability.

Costs, stated plainly because this is the part that needs group agreement:

- A reviewer will ask whether the result holds on a frontier model. The
  answer has to be a measured subset, not an assertion -- run one scenario on
  the hosted model and report both, rather than claiming it generalises.
- A small model may be incoherent enough that its choices are noise rather
  than judgement, which would make ground truth meaningless. This has to be
  gated before committing: check that the local model can actually complete
  the Coder role -- produce a script the Executor runs to the correct output
  -- and that its decisions look like decisions.
- Numbers from two models are not comparable. D-004's rule already covers it:
  all methods on a scenario run on the same model, or none of them do. The
  model name is already in every trace header, so this stays checkable.
- No new dependency. A local server is spoken to over HTTP the same way the
  Gemini client already is, with stdlib `urllib` (D-013).

Recommendation: put this in front of the group alongside D-017's other three.
It does not need to win -- if billing is approved, take billing, it is
simpler. It matters because D-017 is currently framed as pay-or-cut, and
cutting the experiment weakens the paper while this does not.
Note: this also removes the pressure that produced open issue #10's cost
objection. A noise floor of 20 replays is a full day of hosted quota and
about a minute locally, so the measurement stops being something we ration.

---

## D-024  The trace cannot tell "checked and clean" from "never checked"
Date: 31-08-2026
Decided by: proposed with the contamination walk, needs group sign-off --
this touches the shared trace format (D-006, D-008)
Choice: add one additive record type, `check`, recording that a
(source, event) pair was examined:

```
{"record": "check", "source_id": "S5", "target_event": "e0007",
 "method": "counterfactual"}
```

Rejected: inferring it from the influence edges, which is what the code does
today and what this entry exists to replace.

Reason: a counterfactual check that finds **no** influence records nothing.
So an absent influence edge means one of two opposite things -- "we tested
this pair and it came back clean" or "nobody ever looked" -- and the
conservative fallback (docs/02: anything not confidently established is
treated as contaminated) has to assume the second. Every cleared pair is
therefore re-contaminated by the very policy that is supposed to protect us.

Measured on `data/runs/fake.jsonl`, seeding S5:

```
inferred from edges   6 of 17 events contaminated, 11 preserved (65%)
with checks recorded  3 of 17 events contaminated, 14 preserved (82%)
wrongly discarded     e0007, e0008, e0009
```

Seventeen points of the headline metric, thrown away by a missing record.
Those three events were each tested against S5 and cleared -- that is exactly
why they carry no edge -- and we discard them anyway.

The error is in the safe direction: it over-contaminates, so it costs work
preserved and can never cause an unsafe preservation. That is why it is a
defect rather than a disaster, and why it was survivable long enough to go
unnoticed. It would have shown up in the paper as our method looking worse
than it is, which is the kind of bug nobody goes looking for.

Why a separate record rather than a field on `InfluenceEdge`: an edge is a
positive claim, and there is no edge to hang a negative on. Recording the
*examination* keeps one source of truth -- examined plus an edge means
influenced, examined without an edge means cleared, no examination at all
means unknown and the policy decides. `UsageRecord` is the precedent for
adding a record type after the week-1 freeze without touching the three
models D-006 protects.

Until this lands, `contaminate(checked=None)` infers the checked set from the
edges and says so in its docstring. `src/eval/` must pass an explicit
`checked` set when scoring, or every number it produces understates the
method.

---

## D-025  Attack variants are intents, and validity is checked on the trace
Date: 31-08-2026
Decided by: proposed with attack injection, needs group sign-off
Choice: three things about how runs are built and counted.

**1. "influencing" and "exposed only" name what an attack was built to do,
never what happened.** docs/04 asks for two variants of each scenario as
though we control which occurs. We do not. Whether a model uses a page it was
shown is its decision, and the premise of this project is precisely that
exposure does not settle influence -- we cannot assume our way to the answer
we are trying to measure. Which variant actually occurred is read afterwards
from the influence edges.
Consequence, and it is the one that protects the numbers: a run built as
exposed-only that turns out to be influencing is a valid data point and stays
in. Dropping it would silently over-sample the case that flatters us, which
docs/04 already warns against in the same paragraph that asks for the split.

**2. An attack that never reached the trace is not a run.** Validity is
established by finding the planted marker in the finished trace, not by
checking the corpus beforehand. A pre-flight query is a guess: the query the
Researcher issues is built from the Planner's questions and does not exist
until the run happens. Checking a fixture against your own query tells you it
*can* be reached, not that it *was* -- and the failure is silent, because the
run completes and the trace looks entirely normal while measuring nothing.
Found the honest way: the first version of `reaches()` passed against a query
we invented, and the same page was then never retrieved by the pipeline.
`src/eval/harness.py` now raises when the marker is absent.

**3. Never assert that unexamined exposures were checked.** The D-024 stopgap
`all_exposure_pairs()` claims every exposure was examined and cleared. Applied
to a trace where no influence analysis has run -- where *every* exposure
lacks an edge -- it reports the poisoned run as 100% preserved, 0 events
contaminated, 0 unsafe. A textbook unsafe preservation, invented out of an
assumption rather than a mistaken measurement. It defaults to off and the
harness refuses it outright on a trace with no influence edges.

Observed while building, and worth keeping in the paper: with no influence
analysis run, the influencing and exposed-only variants of scenario A score
**identically** (72% preserved, both). That is correct. The conservative
fallback cannot tell them apart, because telling them apart is exactly what
counterfactual analysis is for. It is a clean demonstration of what the
expensive half of the method buys, and it is the number to put beside the
analysed result rather than a bug to fix.

---

## D-026  Counterfactual comparison is semantic. Text comparison is unusable.
Date: 31-08-2026
Decided by: forced by measurement, needs group sign-off on the comparator
Choice: a counterfactual check compares the **decision** an output encodes,
never the text of the output. Text-level comparison is removed from the
method, not kept as a cheap first pass.
Rejected: string equality; normalised string equality; "repeat 3 times and
see if the wording changed" (issue #2's original answer).

Reason: measured, on the live model, 31-08-2026. Eight re-sends of one
identical recorded request at temperature 0 (open issue #10):

```
exact / whitespace / alphanumeric   floor 100%   8 of 8 answers distinct
decision (which library)            floor   0%   1 distinct across 9 samples
```

At a 100% floor a counterfactual flip carries **zero** information: remove a
source, re-run, the text differs -- and it would have differed anyway. Every
influence edge established that way would have been noise wearing the shape
of evidence, and the exposure-vs-influence gap, which is the whole paper,
would have been measured with an instrument whose needle moves on its own.

The same eight samples put the decision-level floor at 0%. All nine answers
(recorded plus eight) chose standard-library `datetime` with candidate format
strings. The choice is stable; only the prose around it moves.

docs/03 issue #2 already said comparison should be "semantic / behavioural".
This upgrades that from a preference to a requirement, and attaches numbers
to both ends of it.

Consequences, none of them optional:

- Every event kind needs a defined comparator before counterfactual replay
  can produce a single edge. `decision` -> which library/approach was chosen.
  `agent_output` for code -> what the executed script prints, which the
  Executor already computes. `agent_output` for prose findings -> the open
  one, and the hardest; an LLM judge is the obvious candidate and it is
  itself an instrument with a noise floor that would then need measuring.
- Report the floor beside every influence result, under the comparator that
  produced it. A flip means nothing without the churn it is read against.
- The comparator must be fixed **before** looking at the trials. The one used
  here is a hardcoded list of library names written in advance and not
  adjusted afterwards; tuning a comparator until it reports stability would
  manufacture exactly the result we are trying to test.
- Temperature 0 is not determinism on a hosted model. Anything in docs/04
  that assumes replay reproduces text is wrong and needs rewriting.

Caveat, stated because it limits the claim: 8 trials, not 20 -- the daily
quota ran out. A 0% decision-level floor on 8 trials is still consistent with
a true rate near 30%. The 100% text-level floor needs no such caveat; it is
8 out of 8 and it is not going to improve with more samples.
