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

---

## D-027  The `check` record lands, and `structural` joins the method vocabulary
Date: 06-09-2026
Decided by: proposed with the recovery work, needs group sign-off --
this adds to the shared trace format (D-006, D-008)
Choice: two additive schema changes, both of them what D-024 asked for.

**1. `CheckRecord`, written for both outcomes.** The record D-024 specified,
with three fields beyond its sketch: `verdict` (`clean` | `tainted`),
`confidence`, and the two `signature_before` / `signature_after` strings plus
the `comparator` that produced them. `unchecked` is deliberately *not* a
storable verdict -- it is the absence of a record, and giving it a second
spelling would create two ways to say the same thing. `Trace.checked(event,
source)` is the lookup and returns all three values.

`contaminate()` no longer infers the checked set from the influence edges.
That inference was the defect: a check finding no influence recorded nothing,
so a cleared pair and an unexamined pair were the same absence. A trace with
no `check` records now clears nothing, which is the correct reading of
"nothing was examined" -- and `metrics.all_exposure_pairs()` is deleted with
it, along with the `--assume-all-checked` flag, because there is no longer
anything to assume. That flag was the D-025 point 3 footgun; on a trace with
no analysis it reported a poisoned run as 100% preserved.

**2. `structural` is a fourth influence method**, and it is the strongest of
the four. Roughly half the events in a run -- tool calls, tool responses,
memory reads and writes -- are computed by our own code, so which inputs
reached them is read off the code path rather than estimated. Two sub-cases,
and getting the second wrong would have been unsafe:

  * *computed from literals.* The database keys and memory keys are constants
    in `pipeline.py`. Nothing in the agent's context reached them, so every
    exposure on those events is genuinely `clean`, free, and certain.
  * *carriers.* A hand-off message, or a tool call whose query was lifted out
    of an earlier output, produces no new content: its text is a function of
    one upstream event's output. The tempting reading is "it consulted no
    source, so it is clean", and that is wrong in the dangerous direction --
    it would mark a message that is a verbatim copy of contaminated output as
    clean. A carrier inherits the influence set of the event it copies
    (`record_carrier`). The copy relation is real influence; only the *reading
    of fresh sources* is absent.

Measured, on the stub-driven scenario A run: check coverage goes from 0 pairs
to 29 of 64 with structural attribution alone, and work preserved under our
method goes 44% -> 72%. The remaining 35 pairs are the model-written events,
which is what Phase 2's estimator is for.
Reason for doing it as records rather than as fields on `InfluenceEdge`: an
edge is a positive claim and there is no edge to hang a negative on. D-024
argued this; nothing found since disagrees.
Consequence: `Trace.validate()` now rejects a `clean` check sitting beside an
influence edge for the same pair, two verdicts on one pair, and a check
naming a pair that was never an exposure. All three are ways for an analysis
run against the wrong trace to produce confident-looking nonsense.

---

## D-028  The content store, and checkpoints that carry refs instead of text
Date: 06-09-2026
Decided by: proposed with the recovery work, needs group sign-off
Choice: build the content store D-010 deferred. A run is now a directory of
three files, not two (extending D-018):

```
data/runs/run1.jsonl              events, sources, influence, checks, usage
data/runs/run1.checkpoints.jsonl  agent state and memory snapshots
data/runs/run1.content.jsonl      prompts, source blocks, outputs, tool state
```

D-010 was right to wait and is now spent: it said no store until selective
replay needed event content, and replay needs three things the trace did not
keep -- the prompt that produced an event (to re-issue it with one source
redacted), the output an event produced (to splice a preserved event back in
without re-invoking the model), and tool/memory state (to detect a live object
still pointing at something invalidated).

**Refs are content-addressed**, `ref = "c" + sha256(text)[:16]`, and that is
load-bearing rather than tidy. Two consequences we rely on:

  * D-016 re-sends every source once per question, so identical text is stored
    many times over. Content addressing makes that one record.
  * "did this preserved event's output change" becomes a 16-character
    comparison. Phase 5 asserts that every spliced event's output matches its
    original log entry byte for byte, and with content addressing that
    assertion is a ref equality rather than a string diff.

**Checkpoints now store `{event_id -> content ref}`, not `{event_id -> text}`.**
D-022 measured checkpoints at 56% of stored bytes and named the cause: a
checkpoint carries every output produced so far, so under the v1 policy the
*k*th checkpoint re-serialises all *k-1* previous outputs, and payloads grow
with the square of run length while the trace grows linearly. Refs make each
output appear once, in the content store, however many checkpoints name it.

Measured twice, on two 18-event runs, because the ratio is made of output
length and the two runs differ in exactly that:

```
                          stub run        cassette run (real model text)
checkpoints with refs        2,814 B                  5,278 B
checkpoints inline           5,072 B (1.8x)          15,520 B (2.9x)
content store               16,825 B                 29,132 B
checkpoint share                 8%                     10%   was 56% (D-022)
```

The stub figure is a floor and D-022 already said why: canned stub text is a
fraction of the length of what the model actually writes. 2.9x is the number to
quote, and it will grow further with run length, because the term being removed
is quadratic in event count and linear in output size.

On quoting a number from a cassette replay, since D-019 forbids exactly that:
this one is admissible and it is worth being precise about why. D-019's rule
protects against a number that describes *a request that never happened*.
Nothing here describes a request. These are byte counts of files written now,
holding text the model genuinely produced, and the arithmetic does not depend
on when the request was made. The tokens and latency in that same trace are
replayed values and remain unquotable. The distinction is per-number, not
per-run.
Consequence for open issue #8: the overhead split is three-way, and content is
50% of stored bytes on the cassette run. That is the honest place for it -- it
is real model output that replay genuinely needs, not tracing overhead we could
drop. The thing we *can* claim to have dropped is the quadratic term.

---

## D-029  One prompt format, and surgery takes the block rather than the prompt
Date: 06-09-2026
Decided by: forced by a near-miss while building the counterfactual stage
Choice: the rendered-source format moves out of `GeminiPipeline._source_block`
into `src/common/prompts.py`, and the redact/replace functions take **both**
the prompt and the rendered source block, never the prompt alone.

Reason, and this one is worth reading before touching that module. Three
things have to agree on the format: the pipeline renders sources into a
prompt, a counterfactual re-issues that prompt with one source removed, and
selective replay re-issues it with one source's content *replaced* by a
recomputed value. While the pipeline owned the format privately this was fine,
because nothing read a prompt back.

The first version of the redactor took a prompt and found the source's span by
scanning for the next header, treating the last source as running to the next
blank line. That is wrong on two of our own prompts. The Coder's prompts put
instructions *after* the source list ("Decide the approach in at most three
sentences"), and a Researcher finding can contain a blank line -- so the last
source either swallowed the trailing instruction or was truncated at its own
paragraph break. A truncated redaction is the bad one: it removes part of a
source, leaves the rest in the prompt, and reports that the source was
removed. The counterfactual then runs, the answer comes back unchanged for the
obvious reason, and the verdict is recorded as *evidence of non-influence*.
That is an unsafe preservation manufactured by a parser, and it is
indistinguishable in the trace from a real result.

So no boundary is ever inferred. The pipeline stores the rendered block in the
content store next to the prompt, surgery happens inside the block where
boundaries are unambiguous, and the result is spliced back by exact substring
replacement that refuses to proceed if the block does not appear in the prompt
exactly once. `render_sources()` also parses its own output before returning
it, so a source whose content contains something that looks like a block
header fails while the prompt is being built rather than during a replay days
later.
Note: the format itself is byte-identical to what the pipeline emitted before,
so `data/cassettes/run1.jsonl` still replays. That was checked, not assumed --
a format change here would have silently invalidated the only real model
output we have.

---

## D-030  A scripted agent, and ground truth by leave-one-out rather than by rule
Date: 06-09-2026
Decided by: the counterfactual estimator scoring 85% unsafe preservation, and
being right to
Choice: offline development runs against `src/eval/scripted.py`, a deterministic
agent whose answer is a **pure function of the sources available to it**. Ground
truth influence is not a rule about which sources it consulted; it is leave-one-
out on that function:

    used(s)  <=>  substance(all sources) != substance(all sources - s)

Reason. Phase 2 cannot be developed against the API: the free tier is 20
requests a day (D-017) and one analysed run costs dozens. The existing stub in
`harness.py` could drive the pipeline but could not test the estimator, because
it returned the same text for the same prompt -- so removing a used source and
removing an unused source both changed nothing, and a counterfactual check
appeared to work perfectly. It would have gone on appearing to work after being
broken.

The instructive part is the first fix, which was wrong. The scripted agent
declared a source "used" when its rule said the agent had read it: a directive,
a format code, an API call, a `task/` key. Scored against that label, the
counterfactual estimator reported 85-100% unsafe preservation and looked
catastrophically broken. It was not. Those sources had been *consulted* and had
changed nothing, because the answer's substance collapsed many inputs into two
values, so "clean" was the correct verdict for most of them. The label was
measuring exposure and calling it influence -- the exact confusion this project
exists to separate, reintroduced in the test harness, where it would have been
diagnosed as an estimator failure.

So the ground truth is now definitional rather than stipulated. It is
CLAUDE.md's own wording -- "a source demonstrably changed the agent's output" --
computed exactly, by re-deriving the answer without each source. After the
change the same estimator scores 1.00 precision and 1.00 recall on all four
attack variants, with the same code.

Two limits, and both belong in the paper rather than in a comment. This measures
the estimator against a scripted agent, so it says the estimator recovers a
usage pattern that really is there, not that real models use sources this way.
And leave-one-out **under-reports on redundant inputs**: when two sources supply
the same fact, neither is individually necessary, so both are called unused.
That is a real property of single-source counterfactuals, not an artefact of the
fixture; the alternative is testing every subset, which is exponential. The
poisoned sources in scenarios A and B carry a directive no clean source carries,
so they stay individually necessary and detectable -- which is a fact about our
scenarios, not a general guarantee.

---

## D-031  Format codes leave the prose vocabulary and get a case-sensitive facet
Date: 06-09-2026
Decided by: a false clean that survived the leave-one-out fix
Choice: `PROSE_TERMS` no longer contains `%y, %m, %d, %b`. `ProseComparator`
gains a `formats` facet built from the `FORMAT_CODE` regex, which is what
`CodeComparator` already used for the same job.

Reason. After D-030 one false negative remained, and it was the same source on
every Researcher event: the page documenting format codes. Removing it changed
the finding from "Format codes documented: %B, %Y, %b, %d, %m, %y" to "%B, %Y,
%d, %m", and the signature did not move.

The cause is that `_present()` matches substrings case-insensitively, so `%Y`
and `%y` are one token. They are not one decision -- a four-digit year and a
two-digit year are different answers -- and a finding that lost `%y` while
keeping `%Y` produced a byte-identical facet. The vocabulary was not merely
crude, which is a property it is allowed to have; it could not represent the
thing it listed.

This is a change to a comparator made after seeing an influence result, so it is
worth being explicit about why it is not the tuning D-026 forbids. The
pre-registered rule governs *exclusion*: a facet is dropped when its floor on
unchanged re-sends is non-zero, and never on the basis of what it says about
influence. This adds a facet, and it adds it because the old one was
unimplementable as documented, not because a result was unwelcome. The direction
matters too: the fix makes the comparator strictly more sensitive, which moves
verdicts towards "influenced" -- the safe direction, more recomputation and
fewer unsafe preservations.
Consequence, and it is a real gap: `prose` has **no measured floor**. The eight
noise trials measured decision facets only, so the `formats` facet's stability is
unknown and `Calibration.is_calibrated("prose")` is false. The estimator counts
this as `uncalibrated:prose` on every run that uses it. A prose floor needs a
live measurement and cannot be had from the data on disk.

---

## D-032  Attribution runs after detection, on the flagged region only
Date: 06-09-2026
Decided by: measuring the inline cost against the targeted cost
Choice: the default cost model is self-report inline during the run, then
`refine_for_verdict()` spending counterfactuals only on pairs the detector's
verdict makes relevant. Inline counterfactual attribution stays available as an
ablation, not as the method.

Reason. Attributing every exposure inline costs one counterfactual per pair
whether or not that pair could ever matter. Measured on the scripted agent,
across the four attack variants:

    condition              self-report calls   counterfactual calls
    counterfactual only            0                   35
    hybrid, inline                 6                27-30
    self-report + targeted         6                  0-5

with zero unsafe preservations in all three. The inline hybrid barely improves
on brute force, because every negative claim still needs verifying and the agent
claims few positives. The saving is not in choosing *which* claims to verify; it
is in not asking about pairs that no incident implicates.

The targeted pass is a frontier expansion over the contaminated region: examine
an unchecked pair whose source is currently contaminated, record the verdict,
recompute the region, repeat. A pair that comes back clean stops the walk rather
than seeding more checks, so the cost scales with the contaminated region rather
than with the trace. This is docs/02's own instruction -- "run counterfactual
only where it matters" -- and it is also what makes docs/03 issue #7's collapse
condition avoidable: analysis cost now sits below the rerun cost it avoids
instead of above it.
The reason this is sound rather than merely cheap: the estimator still never
sees the label. Flagged ids arrive as an argument and choose *where to look*,
never what the answer is. Pairs left unexamined stay `unchecked`, which the
contamination walk treats as contaminated, so running out of budget costs
preserved work and never safety.

---

## D-033  The Planner's task goes in as a rendered source, not as inline text
Date: 06-09-2026
Decided by: the self-report question asking the Planner about an id it had
never seen
Choice: `GeminiPipeline.run()` renders the task through `_source_block()` like
every other input, instead of interpolating it into the prompt.

Reason. The task was the Planner's only input, so inlining it looked harmless.
It was not: the self-report question is built from the event's exposures and
asks "did [S1] change what you wrote", while the Planner's prompt contained the
task text and no `[S1]` anywhere. The agent is being asked about a label it was
never shown, and a counterfactual has no span to redact -- `redact_in_prompt`
raises `SourceNotInPrompt`, which the estimator correctly turns into
"influenced, could not be examined", so the Planner's one pair was permanently
unresolvable.
Now every model-written event's inputs are addressable the same way, and the
Planner's pair resolves like any other. Cost: one more rendered block per run,
and the prompt reads slightly more formally.

---

## D-034  Detection is a socket, and the perfect detector gets a name
Date: 06-09-2026
Decided by: `compare()` defaulting to the true labels
Choice: `src/eval/detectors.py` defines a `Verdict` and a `Detector` protocol.
`metrics.compare()` takes a verdict, defaulting to `Oracle`, and every `Score`
records which detector produced it.

Reason. The specification is explicit that we do not build a detector -- it is a
"pluggable, replaceable black box" -- so the missing piece was never a detector,
it was the socket. `compare(trace, malicious=planted)` passed the planted source
ids straight through, which is the perfect-detector assumption docs/01-scope.md
declares out of scope, present in every number the project had produced and
named in none of them. It is now `Oracle`, it prints its own name under every
table, and it carries the note "an upper bound, not a measurement".

A verdict carries three things beyond the source list, because CausalLine's
sensitivity to each is a separate question. **Confidence**, so `MaliciousSources`
is a threshold rather than a set. **Misses**, because a missed source is the only
detector failure that can cause an unsafe preservation -- and that failure must
be reported as the detector's, not the method's, which is why `Score` carries
`detector_missed` and the table prints it apart from the unsafe count.
**Latency**, as the event at which the alarm fires, because a slow detector means
more work exists downstream by the time recovery starts, and that is exactly the
condition under which selective recovery should beat restarting. A method
evaluated only at zero latency is evaluated where it has least to prove.

`Blind` -- flags nothing -- is in the set for a reason that is not symmetry: it
is the only condition that proves the scoring can report failure at all. Under
it every method preserves everything and the truly contaminated events show up
as unsafe preservations. A metric that never comes out badly is not measuring
anything.
Ground truth is readable in exactly two modules now: `metrics.py`, which scores
after the fact, and this one, because a simulated detector is by definition a
function of the truth and there is nothing else to simulate it from. The rule
that keeps it honest is the boundary: a detector reads the label and emits a
Verdict, and everything downstream reads the Verdict.

---

## D-035  Ground truth walks true influence, not the estimator's influence
Date: 06-09-2026
Decided by: noticing that `unsafe = 0` was still guaranteed
Choice: `ground_truth_events()` takes `true_influence`, an explicit (source,
event) relation, and `contaminate()` takes an `influence` override so the walk
can be run over it. The circularity flag now asks whether that relation was
supplied.

Reason. metrics.py already warned that ground truth and our method walking the
same influence edges makes them identical by construction. The fix looked like
it was "make the edges estimates, so they can be wrong" -- and that is not
enough. If ground truth reads the estimator's edges, it inherits the estimator's
mistakes: a source the estimator wrongly called clean is absent from *both*
walks, so the event it contaminated is missing from the answer key too, and the
unsafe preservation is not counted because ground truth agreed with the error.
The guarantee survives the introduction of estimation untouched.

So ground truth now walks the influence relation computed by leave-one-out on the
scripted agent (D-030), with every other exposure pair treated as known-clean,
because with the truth in hand nothing is unexamined. The method still walks its
own estimated edges. The two disagree exactly where the estimator was wrong,
which is the only arrangement in which the number means anything.
The old flag was `bool(trace.influence) and method == "ours"` -- true of every
trace the project could produce, so it would have printed CIRCULAR over real
results forever. It is now `method == "ours" and not independent_truth`.

---

## D-036  Every prompt ends with its source block, and the trace is checked for it
Date: 06-09-2026
Decided by: the scripted agent reading the Coder's trailing instructions as part
of a memory source
Choice: the source block goes last in every prompt the pipeline builds, and
`src/eval/contract.py` fails a trace where parsing sources from the prompt
disagrees with parsing them from the stored block.

Reason. D-029 removed the boundary hazard from the *redaction* path by doing
surgery inside the stored block. It did not remove it from every reader of a
prompt, and there is now another reader: the scripted agent parses the prompt it
is handed. Its view of the Coder's `style/output` memory ran "...print one result
per line.\n\nDecide the approach in at most three sentences", because a source's
content extends to the next header and the last source's extends to the end of
whatever it was embedded in.

With the current wording this was harmless, and that is the worst kind of
harmless. The trailing instructions contain no library name, no format code and
no directive, so nothing moved. An instruction mentioning ISO or `%Y` would have
made the last source appear to supply it -- and the leave-one-out ground truth
would have agreed, because it is computed by the same reader. Ground truth and
estimate would have been wrong together, which is the one failure mode this
evaluation is built to rule out.

Putting the block last makes the boundary unambiguous by construction rather than
by discipline, and the contract check makes it a property the trace is tested for
instead of a convention someone has to remember.
Cost, and it is a real one: `data/cassettes/run1.jsonl` no longer replays. It had
already stopped replaying at D-033 and nobody noticed, which is its own small
lesson -- the check that would have caught it is the one now added. The cassette
is still readable as recorded model output; it needs re-recording against the
current prompts, and that costs 6 requests. See open issue #11.

---

## D-037  What facet exclusion costs, measured
Date: 06-09-2026
Decided by: isolating it, after it produced the only unsafe preservation we have
Choice: keep the pre-registered exclusion rule, and report this number beside
every result that depends on it.

Reason. D-026's exclusion rule drops any facet whose floor on unchanged re-sends
is non-zero, and `Calibration`'s docstring states plainly that this is the unsafe
direction: a removed source that would only have moved an excluded facet leaves
the signature unchanged, and the check returns `clean`. That was an argument.
It now has a measurement.

Same runs, same estimator, only the calibration differing:

    calibration                       unsafe preservations across A/B x infl/exp
    excludes strategy, dependency                    1  (S11 -> e0013, A-exp)
    excludes nothing                                 0

One unsafe preservation, and it is entirely attributable to the exclusion: with
`strategy` restored the pair is caught and nothing else in the matrix moves. The
mechanism is exactly the predicted one -- removing the format-codes finding flips
the Coder from "candidate format strings" to "infer the format", which is the
`strategy` facet and nothing else.

The rule stays, and the reason is not stubbornness. `strategy` has a measured
0.75 floor on `gemini-3.6-flash`: it moves on three of four *unchanged*
re-sends. Keeping it would make almost every counterfactual answer "influenced"
whatever was removed, which does not buy safety -- it buys a check that has
stopped being a check, and the method silently becomes the conservative fallback
it is supposed to improve on. So the honest statement is that this facet is
unusable on this model and one real influence hides behind it, not that
exclusion is free.
Note the asymmetry in where the numbers come from: the 0.75 floor is measured on
the live model, the 1 unsafe preservation is measured on the scripted agent. They
are not the same instrument and the comparison is indicative, not exact.

---

## D-038  CausalLine recovery: verified frontier, greedy cover, splice replay
Date: 06-09-2026
Decided by: proposed with the recovery work, needs group sign-off
Choice: four things, all of them what the algorithm spec asked for and what
the repository did not have.

**1. Safe Frontier is causal, then made cross-agent consistent.**
`safe_checkpoint_for()` picked the latest checkpoint before the earliest
invalidated event by topological position. That ignores whether the
checkpoint's own causal past is clean, and it ignores an event that sits on
the clean side of agent B but was influenced by something on the tainted
side of agent A -- an orphan. `safe_frontier()` in `src/recovery/planner.py`
picks, per agent, the latest checkpoint whose influence-ancestors are
disjoint from Taint, then pulls frontiers backward until no kept event is
influenced by an event another agent is about to throw away. The pull is
monotone and bounded by |V|; exceeding that bound is an assertion, not a
comment. Chronological parents are deliberately excluded from CausalPast:
including them would make every post-taint checkpoint look unsafe and
collapse the step to B1.

**2. Action selection is greedy set cover, not an exact cut.**
Minimum-cost cut over a contamination DAG is NP-hard (weighted
feedback-vertex / hitting-set). The planner enumerates replay, invalidate,
restart(agent), isolate(agent), restart_all, and picks min cost / paths
broken, capped at restart_all. Isolate is priced at 2× restart_all so the
greedy will not disable an agent the task still needs. We do not claim
optimality.

**3. Replay splices logged bytes; it never asks the model to reproduce them.**
D-026 measured a 100% text-level noise floor. `SplicingClient` returns the
logged output for every event not in the invalidation set and refuses to
call the inner client otherwise. Equality is a content-ref comparison
(D-028). Replayed prompts have flagged sources redacted. The recovered run
is a new file; the original is not overwritten.

**4. Scenario C is a handoff hook, not a corpus edit.**
A and B stay data. C appends a Researcher→Coder message after the real
findings. The planted string is labelled after the run, by marker. Raising
`NotImplementedError` is no longer acceptable: a third of the experiment
matrix was a hole.

Baselines B0/B1/B2 share the same replay engine and differ only in the
invalidation set. That is the fair comparison: same splice/re-invoke
machinery, different discard policy.

**5. Tainted events are sinks when influence never reaches the Executor.**
The Executor runs the Coder's script without that script being wrapped as
a source in its context, so `MaliciousSource → FinalOutput` paths in
E_influence stop at the Coder. When Taint is non-empty and no such path
exists, the planner treats each tainted event as a sink that the greedy
cover must hit. Exposed-only (Taint empty) still selects nothing.
Verification re-runs Taint on a sealed post-recovery graph: spliced
events keep their original edges, replayed events drop edges from
flagged sources, and pipeline-computed handoffs are not allowed to
re-import the old flagged edges. Live memory is stale only when its
*value* still matches an invalidated write, not when the recovered
event happens to reuse the same id.
