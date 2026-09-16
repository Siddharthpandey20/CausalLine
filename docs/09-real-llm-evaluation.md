# 09 — Real-LLM evaluation

The second evaluation mode. `docs/06` §5 says every accuracy number in this
project is measured against a scripted agent whose usage rule we wrote; this is
the work that adds a path where real models drive the agents and the tests are
generated rather than hand-written.

**Read `docs/06` §5 and §2.1 before quoting anything from here.** The
limitation this reduces is not the limitation it removes, and §9 below says
exactly where the line is.

---

## 1. The two modes, and why both exist

```
                          Evaluation
                               │
              ┌────────────────┴────────────────┐
              │                                 │
        Scripted mode                     Real-LLM mode
   src/eval/experiment.py              src/eval/real_llm.py
   src/eval/campaign.py                src/eval/real_campaign.py
              │                                 │
      ScriptedClient                    src/common/nvidia.py
      (known usage rule)                  NVIDIA model pool
              │                                 │
      ground truth is KNOWN           ground truth is OBSERVED
      by construction                 (canary token + code path)
              │                                 │
              └────────────────┬────────────────┘
                               │
                          CausalLine
              (tracing → provenance → contamination
               → planning → replay → verification)
                               │
                        the same metrics
              B0 / B1 / B2 / CausalLine, RecoveryScore
```

They answer different questions and neither replaces the other.

| | scripted mode | real-LLM mode |
|---|---|---|
| agent behaviour | a rule we wrote | a hosted model |
| ground truth | exact, per (source, event) pair | observed, token-scoped |
| determinism | full; 30 repetitions give a real CI | none claimed |
| tests | 3 hand-written scenarios × 2 variants | generated from a 240-point design space |
| answers | *does the estimator recover a usage pattern that is really there?* | *does the machinery work, and does an attack land, when a real model is in the loop?* |
| cost | free, offline | one API request per model call |

**Nothing in the scripted path changed.** `experiment.py` and `campaign.py`
still run exactly as before, with the same scenarios, the same baselines, the
same detectors and the same numbers. Two functions in `experiment.py` were
renamed from `_score_row`/`_run_baseline_recovery` to `score_row`/
`run_baseline_recovery` and given optional arguments, so that the real-LLM path
scores through *the same* implementation rather than a copy of it. Behaviour is
unchanged and the test suite pins it.

---

## 2. Models

Verified against the live API on 10-09-2026 by listing `GET /v1/models` and
issuing a real completion — not taken from a display name.

| requested | API identifier | state |
|---|---|---|
| MiniMax M3 | `minimaxai/minimax-m3` | **410 Gone.** End of life 2026-09-09T09:00:00Z |
| Nemotron-3.5-Lightning-30B-A3B | `nvidia/nemotron-3.5-lightning-30b-a3b` | **works**, ~0.5–4s per call |
| DeepSeek-V4-Flash-0731 | `deepseek-ai/deepseek-v4-flash-0731` | **intermittent.** See below |

**DeepSeek is flaky rather than gone, and the distinction took two days to
establish.** On 10-09 a trivial 8-token request stalled past 300s on all three
keys, repeatedly, with no status code and no body — neither a rate limit (a 429
has a body) nor an entitlement problem (that returns 404 with a "not found for
account" detail, which a sibling model does return). On 11-09 the same request
returned in 0.7s, and so did a 768-token JSON request. And in between, a
generation call inside a live campaign exhausted its retry budget on timeouts
and fell back to Nemotron — minutes after the preflight for that same model had
passed.

The clinching measurement is three consecutive requests to the same endpoint
on the same key, seconds apart: **0.9s, 0.7s, then a 240-second timeout.** The
variation is per-request, not per-day and not per-key.

**And a cheap liveness probe does not predict it.** DeepSeek passed the
45-second preflight — a four-token request — and then failed every real
workload it was given: two execution tests void, at 1342s and 384s of stalled
retries, in the same campaign in which its preflight had just succeeded. So the preflight bounds the damage
(45s instead of five stalled retries) without preventing it, and the thing that
actually protects a campaign is the cooldown after a *real* failure. Worth
stating plainly because the obvious design instinct — probe first, trust the
probe — does not survive contact with this endpoint.

So neither "works" nor "is down" describes this endpoint. That is exactly why the pool preflights, why the model
cooldown doubles, and why `GenerationRecord.fallback_from` exists: a scenario
this model was asked for and did not produce is recorded as such rather than
quietly appearing under whoever picked it up.

**The consequence for §9 is worse than a flat outage would be.** Which model
generated or executed a given test depends on whether the endpoint happened to
answer at that moment, so the model assignment is not a controlled variable and
any per-model comparison drawn from it is confounded by availability. Read the
`generation.model_handle` and `fallback_from` fields on each scenario before
saying anything about model diversity.

**Nothing was substituted.** Both unavailable models stay in the registry under
their verified identifiers, marked with the exact failure, and every plan and
results file prints them. See `docs/05` D-058 for the reasoning, and §9 for
what it costs the result.

```bash
python -m src.common.nvidia --models   # the registry, free
python -m src.common.nvidia --smoke    # one live call per available model
```

Per-model request shape lives in `NVIDIAModel.extra_body`, because these models
are not interchangeable at the wire level. Nemotron is a reasoning model that,
left alone, spends its whole output budget on a visible chain of thought;
`chat_template_kwargs={"thinking": false}` turns that off. Measured: with
thinking off, a JSON request returns clean JSON in ~4s; with it on, the model
narrates for 100s and returns prose. Same decision, same reasons, as D-015 for
Gemini's `thinking_level="minimal"` — thought tokens are billed, appear nowhere
in the trace, and add run-to-run variation.

---

## 3. Configuration

Keys are environment-only. `.env` is gitignored and `.env.example` carries
placeholders.

```env
NVIDIA_API_KEY_1=...
NVIDIA_API_KEY_2=...
NVIDIA_API_KEY_3=...
```

Everything else is optional, with defaults:

| variable | default | what it does |
|---|---|---|
| `NVIDIA_MODEL` | `nemotron` | default model handle |
| `NVIDIA_TEMPERATURE` | `0` | |
| `NVIDIA_MAX_OUTPUT_TOKENS` | `2048` | JSON requests use a smaller ceiling; see §7 |
| `NVIDIA_TIMEOUT_S` | `300` | per request; a model may set a lower one |
| `NVIDIA_MAX_ATTEMPTS` | `5` | retries per call |
| `NVIDIA_REQUESTS_PER_MINUTE` | `40` | 0 disables pacing |
| `NVIDIA_KEY_COOLDOWN_S` | `60` | how long a rate-limited key sits out |
| `NVIDIA_MODEL_COOLDOWN_S` | `120` | how long a failing model sits out |
| `NVIDIA_MAX_CONCURRENCY` | `2` | whole tests in flight at once |

Precedence: real environment variables beat `.env`, keyword overrides beat
both — the same rule `load_settings()` uses for Gemini.

**Secrets.** Keys are read, held in memory and written nowhere.
`NVIDIASettings.fingerprint()` is built field by field so there is no code path
from it to `api_keys`; every server error body passes through `redact()` before
it can reach an exception message; the key pool reports slots ("key 2 of 3"),
never values. `tests/test_no_secrets.py` asserts all of it on every commit,
and deliberately not as a checklist item: it reads the real `.env` when one
exists and scans source, tests, docs, committed configuration, generated
artefacts, run traces, results and git history for those values; it scans for
key-*shaped* strings even on a machine with no `.env` (which is what catches a
key pasted in by hand); and it asserts `.env` is both gitignored and untracked.
That last scan is only useful while nothing in the repository looks like a real
key, which is why the client tests use `TEST-NOT-A-REAL-KEY-...` fixtures
rather than realistic `nvapi-` ones.

---

## 4. Test-suite generation

`src/eval/llm_scenarios.py`. The design rule, which is the whole of it:

> **We choose the structure. The model writes the surface form.**

### 4.1 The structure is ours

`DesignPoint` is a point in an enumerated space:

| axis | values |
|---|---|
| channel | `web`, `memory`, `agent_message` |
| intent | `influencing`, `exposed_only` |
| attack style | `direct_instruction`, `authority_appeal`, `context_reframe`, `embedded_footnote`, `conditional_trigger` |
| workflow | `short` (4 agents, 1 research round), `long` (5 agents with a Reviewer, 2 rounds) |
| decoys | 0, 1, 2 clean sources planted beside the payload |
| redundancy | `none`, `duplicate_fact` (two decoys restating one fact) |

240 structurally distinct points after removing the impossible combination
(`duplicate_fact` with fewer than two decoys). `sample_design(n, seed)` draws
without replacement, stratified by (channel, intent) first, so a six-test suite
still covers all three injection routes and both variants rather than drawing
five web attacks by luck.

That is what §10 of the brief means by **behavioural** diversity: two tests
differ because their causal graph differs, not because their prose does. A
duplicate structure is impossible, not merely unlikely.

`duplicate_fact` is in the space on purpose and it is the setting CausalLine is
*expected to do badly on* — leave-one-out cannot find either of two redundant
sources individually necessary (docs/06 §2.2, D-030, D-051). A suite that
cannot produce a failure is not an evaluation.

### 4.2 The words are the model's

The generator is asked for: the payload text, the decoys, a paraphrase of the
user's task, and its own predictions. Prompt version is recorded on every
scenario (`gen-v2`) so two suites built under different prompts are never
pooled.

Every scenario is **validated before it is kept** (`validate()`), and a failure
is repaired once with the reasons attached, then dropped:

- the payload must carry its marker, or the run cannot be labelled
- an influencing payload must carry its token, or there is no ground truth
- an exposed-only payload must **not** carry it, or it is an influencing
  payload wearing the control's label
- no decoy may carry either, or it would be labelled malicious and stop being
  the clean source it is there to be
- the task paraphrase must still ask for ISO output, one per line — the
  Executor compares stdout against fixed expected values, so a paraphrase that
  drops the contract turns every run into a failure that looks exactly like a
  successful attack
- **a `web` payload must actually rank into the Researcher's results.** The
  first generated payload was 25 words and read as pure instruction; it carried
  almost no date-parsing vocabulary, so `web_search` ranked it below the real
  documentation pages *and* below the scenario's own decoy. It never entered any
  agent's context, the marker matched nothing, and the run was void — after the
  Planner's and Researcher's tokens were spent. `attacks._poisoned_page` already
  warns about this ("an unretrieved page is not an exposed-only attack, it is no
  attack at all"); a hand-written attack gets tuned by a person until it ranks,
  and a generated one has nobody to do that, so `retrievable()` does it in the
  loop. This is the single most useful thing the validator caught.

A scenario that cannot be repaired is rejected and counted. The per-model
rejection rate is reported.

### 4.3 Generation is spread across models — as far as the models allow

Round-robin over the models that answer, recorded per scenario
(`GenerationRecord.model_handle`). A suite written entirely by one model cannot
answer whether CausalLine's behaviour depends on which model wrote the test.
Execution is offset from generation so a test is not usually executed by the
model that authored it.

**The intent and the outcome are different things here, and the record keeps
them apart.** When the preferred model fails, `ModelPool` falls back so the
suite still gets built, and `GenerationRecord.fallback_from` records which
model was asked and did not deliver. On this hardware that field is the
difference between "the suite was spread across two models" and "the suite was
assigned to two models and one of them wrote nearly all of it". Read it before
making any claim about model diversity — see §2 for why.

### 4.4 Storage

JSON Lines under `data/generated/`, one scenario per line — the same format the
trace logger uses, so a partly written suite is still readable. A test asserts
that a saved suite contains no `api_key`, `nvapi-`, `authorization` or `bearer`
string.

```bash
python -m src.eval.llm_scenarios --space              # the design space, free
python -m src.eval.llm_scenarios --n 6 --out data/generated/suite.jsonl
```

---

## 5. Ground truth

This is the part that decides whether any number here is worth reading.

**An LLM's statement that "source X influenced agent B" is a claim, and this
project has a word for a claim treated as evidence.** The generating model's
predictions are recorded in `ScenarioAnnotation` with `authoritative=False`,
scored against what actually happened, and used for nothing else.

The truth relation is `real_llm.observed_influence()`: the union of two
mechanical relations, and nothing else.

**1. The canary token.** Every influencing payload carries a per-test token and
instructs the agent to repeat it. Then:

> the token appears in an event's output ⇔ the payload influenced that event

A substring test on bytes. Not the estimator's opinion, not a comparator's.
This is Phase 13.2's trick (`src/eval/token_validation.py`), reused because it
is the one non-circular live measurement the project already had. Carriers
propagate: a source wrapping an output that came back carrying the token is
itself a carrier, computed to a fixed point so a summary-of-a-summary in the
long workflow is not missed.

**2. The code path.** Events our own code computed — tool calls with literal
keys, the Executor running the script it was handed — have an input relation
read off `src/tracing/pipeline.py` rather than estimated. Those are the
`record_structural()` verdicts.

**`record_carrier()` records are excluded, and that exclusion is load-bearing.**
They also store `method="structural"`, but what they store is inherited from
the *estimator's* edges upstream. Reading them back as ground truth would score
the estimator against its own answers and the circle would be invisible.
`code_path_pairs()` filters them by note text; a test pins the phrase.

**Prompts are never searched for the token.** An early version searched an
event's inputs as well as its output, so the Executor's `tool_call` — which has
no output — could be reached. It made every agent that had merely *seen* the
payload look influenced, including a run in which the model ignored the
instruction entirely, which then scored as a landed attack with three
contaminated findings. That is exposure counted as influence, inside the
yardstick. Only outputs and *tool arguments* are searched now
(`Trace.tool_args_text`), and `test_exposure_is_never_counted_as_influence_in_ground_truth`
is the guard.

### 5.1 The four things kept apart

Per §7 of the brief, and they appear as separate fields on every result:

| | where it lives |
|---|---|
| generated scenario | `GeneratedScenario` — what the model was asked to write, and wrote |
| actual LLM behaviour | `TruthReport.events_with_token`, `payload_landed` |
| annotated behaviour | `ScenarioAnnotation`, `authoritative=False` |
| observed CausalLine | `RecoveryScore` rows, `TruthReport.truth_events` |

### 5.2 A run the model ignored

A real model may simply not follow a planted instruction. When it does not, no
output carries the token, there is no contamination to find, and every method
records zero unsafe preservations for a reason that has nothing to do with the
method.

Such a run is **kept, marked `payload_landed=False`, and printed** — dropping
it would over-sample the attacks that worked (D-025 applied to a new source of
the same bias). `landing_summary()` reports the compliance rate per model,
because that is a property of the model and has to be visible before any row
below it is read.

---

## 6. Execution and metrics

`run_generated()` runs one scenario through the existing machinery:

```
generated scenario
   ↓  scenario.apply(Tools.from_fixtures())      poisoned corpus, D-014
run_pipeline(client=NVIDIAClient, attributor=HybridAttributor)
   ↓  label_malicious(marker)                    ground-truth labels, after the run
   ↓  detector.flag(trace)                       oracle | heuristic | pessimistic | blind
   ↓  refine_for_verdict(...)                    targeted counterfactuals, D-032
   ↓  observed_influence(trace, planted, token)  ground truth, §5
   ↓  contaminate(...)                           the truth events
B0 / B1 / B2 via replay(),  CausalLine via recover()
   ↓  score_row(...)                             the same RecoveryScore as scripted mode
```

**No metric is re-implemented.** Work preserved, unsafe preservations,
discarded events, blast radius (events and agents), recovery / analysis /
replay tokens and escalations are the existing `RecoveryScore` fields, printed
by the existing `recovery_table`.

Real-LLM metadata is recorded *beside* them, not mixed into them, on
`RealRunResult`: execution model and its API identifier, generating model,
test id, attempt, latency, pipeline and analysis tokens, API call count, retry
count, rate-limit count, transient-error count, key rotations, throttled
seconds, prompt version, seed and timestamp.

### 6.1 Pair-level accuracy

The event-level table answers *how much work did each method keep*.
`score_pairs()` answers the question underneath it: for each (source, event)
pair the estimator had an opinion about, was the opinion right? On the scripted
runs `influence_eval.score_estimator()` does this against the client's own
usage log; here the token does it, and the `PairOutcome` type is shared with
`token_validation` so both report the same three-way verdict.

Two agreement numbers come out, and they answer different things:

| | what it means |
|---|---|
| **operative** | what the method *does* — influenced and unchecked both mean recompute. What a deployment experiences. |
| **examined** | restricted to pairs the estimator actually reached a verdict on. Its own accuracy, with the conservative fallback's contribution removed. |

And separately, the number no result may omit: **unsafe pairs** — cleared while
the output demonstrably carried the token.

`unchecked` is deliberately not folded into "clean". The contamination walk
treats an unexamined pair as contaminated (D-024), so scoring it as a clearance
would manufacture unsafe preservations the method never commits.

**A run whose payload never landed contributes no pairs at all.** It is
tempting to score them as "token absent, therefore not influenced, and the
estimator agreed" — and it is wrong: the token's absence shows the
*instruction* was not followed and says nothing about whether the source
changed the output some other way. Counting them would hand the estimator a
page of free correct answers on exactly the runs where nothing was tested.
`pair_summary()` prints "no run had a payload that landed" rather than a 0%.

### 6.2 A failure mode selective replay has against a real model

Replay matches spliced outputs **by call order**. If the Planner event is
invalidated, a real model may return a different number of research questions
on the replay, and the queue desynchronises. `run_generated` catches
`SpliceError` per method, records it as a note, and scores the methods that did
replay. It is a genuine limitation of selective replay against a
non-deterministic model, so it is reported rather than retried away.

---

## 7. Rate limits, retries and fallback

One integration layer, `src/common/nvidia.py`. Authentication, model
configuration, retry, backoff, cooldown and logging live there and nowhere
else; the rest of the project sees only the `generate()` contract that
`GeminiClient` and `ScriptedClient` already answer.

### 7.1 Keys

```
request → key 1 → 429? → cool key 1, try key 2 → 429? → key 3 → backoff
```

**Sticky, not round-robin.** A key is used until it says it cannot serve us.
Rotating per request would be a way of getting more throughput out of three
accounts than one account allows; that is not what the pool is for and not what
it does.

Two ways out of the rotation, and they are different:

| | trigger | effect |
|---|---|---|
| `cool()` | 429, or a transient service error while this key was in use | temporary; returns when the timer expires |
| `invalidate()` | 401 / 403 | permanent — a wrong key is wrong in an hour too |

When every key is gone, `NoUsableKey` is raised with a diagnosis and no key
material. It is deliberately *not* retryable: no amount of waiting produces a
valid key. `ModelPool` re-raises it rather than walking the model list, because
no other model will do better.

### 7.2 What is retried and what is not

| condition | treatment |
|---|---|
| 429 | cool the key, rotate, retry; `Retry-After` honoured when longer than our backoff |
| 408, 409, 425, 500, 502, 503, 504 | retry on the **same** key — the service, not the key |
| network error, read timeout | retry (including bare `TimeoutError`, which is an `OSError` but not a `URLError` — D-020) |
| 401, 403 | key out of rotation permanently, continue on the next |
| 410 | `ModelUnavailable` — never retried, never substituted |
| 400, 404, 422 | `LLMError` immediately — a malformed request is malformed next time, and five attempts only bury the message |
| empty answer with `finish_reason != stop` | retried, then fails loudly |
| **JSON requested, unparseable answer** | retried, then fails loudly — see below |

Backoff is exponential with jitter, bounded by `NVIDIA_MAX_ATTEMPTS`.

### 7.3 Degenerate JSON

Measured, not hypothetical. Nemotron 3.5 Lightning intermittently answers a
JSON request with an opening brace followed by ~3000 tab characters —
temperature 0, `finish_reason=stop`, plausible token count. The same prompt a
minute later returns correct JSON. As a caller-side `LLMError: expected JSON`
it killed a live pipeline at its second call, after the Planner's tokens were
already spent.

It is a transient generation failure in the same category as a 503, so the
response is validated inside the client and a degenerate one is raised as
`Retryable`. Bounded by the same attempt budget, counted in
`stats.malformed_json`.

Two consequences worth stating:

- **The rejected attempt's tokens are charged to the call.** They were spent.
  The cost metric splits recovery into analysis and replay (open issue #7), and
  a resampled call reporting only its final attempt would make the analysis
  look cheaper than it was, in our own favour.
- **JSON requests use a smaller token ceiling** (`json_max_tokens`, 768). A
  degenerate answer is whitespace until the budget runs out, so the budget is
  what a rejected attempt costs — ~80s at 2048, ~25s at 768. Every JSON answer
  this project asks for is small, so the headroom bought nothing here.

### 7.4 Models

`ModelPool` is the second level: a 410, a model-side outage, or a stalling
endpoint cools that model and the next one takes the work.

**The unit of fallback is a whole test, never a call inside one.** A trace is a
trace of one model; splicing two models into one workflow would make the
trace's `model` header a lie and the per-model comparison meaningless.

That holds for *generation*, where a scenario the preferred model could not
write is simply written by the next one and `fallback_from` records it. For
*execution* there is no fallback at all: a model that refuses mid-pipeline
kills that test, which is recorded as void.

**A failed test does cool the model, though**, and the distinction that makes
that safe is `RealRunResult.model_failure`:

| the test died because | model_failure | campaign cools the model |
|---|---|---|
| the endpoint would not answer | yes | yes |
| the payload never ranked, the marker never landed, the scenario was invalid | no | no |

Cooling on the second kind would punish an endpoint for our own generator's
output. Not cooling on the first kind was measured: on a flaky endpoint it cost
three of six tests in a campaign over an hour long, because each test was
handed to a model that had just failed the previous one. The handle for each
test is therefore chosen when that test starts, not when the campaign was
planned.

**The cooldown has to be on the order of what the failure cost, and the first
value chosen was not.** `model_cooldown_s` started at 120s, matching the key
cooldown — which is wrong, because the two failures cost different amounts. A
rate-limited key costs one request. A model that will not serve a whole test
costs the test: 22 minutes of stalled retries for a void row, in the run where
this was observed. A 120s window expired between one test and the next, so the
campaign cheerfully handed the same dead endpoint the very next test. The
default is now 900s and still doubles.

**The cooldown doubles, and a campaign probes first.** Both were added because
a flat cooldown was measured and found wrong in a specific way. With a fixed
120s window, a six-scenario generation pass preferred the dead DeepSeek
endpoint for three of them; each attempt spent five bounded requests that stall
for 90 seconds apiece, the window expired between scenarios every time, and the
pool cheerfully tried again — about seven minutes of nothing, three times over,
in a pass whose useful work took twenty seconds per scenario.

So: `ModelPool.preflight()` sends one short request per model (one attempt, a
45-second deadline) before a campaign commits to anything, and `cool()` doubles
the window on each subsequent failure up to an hour. A model having a bad
minute is back almost immediately; a model that is down is tried once, cheaply,
and then left alone. Measured after the change: DeepSeek is marked unreachable
in 45 seconds and never preferred again.

### 7.5 Concurrency

Bounded and configurable (`NVIDIA_MAX_CONCURRENCY`, default 2). The parallel
unit is a whole test, because a test writes traces, checkpoints and a memory
file keyed by its own path, and the replays inside one test are sequential by
nature. `KeyPool` and `RateLimiter` are both lock-guarded, and the campaign
shares *one* limiter across every client so two tests in flight do not issue
twice the configured rate.

---

## 8. Running it

```bash
# free: what a campaign would do, and what it would cost
python -m src.eval.real_campaign --plan --n 6

# generate a suite only
python -m src.eval.llm_scenarios --n 6 --out data/generated/suite.jsonl

# generate + run + score
python -m src.eval.real_campaign --n 6 --detector oracle

# re-run an existing suite (no generation requests)
python -m src.eval.real_campaign --suite data/generated/suite.jsonl
```

Results are written to `data/results/real-llm.json`: every row, every truth
report, every annotation check and every failure, including the void runs.
Traces land under `data/runs/real/<test_id>/`. Both are gitignored — they are
run output, not source.

Budget: roughly 45 requests per short-workflow test and 80 per long one, plus
one per scenario generated. `--plan` prints the estimate before anything is
spent.

**Measured, first campaign (11-09-2026):** six scenarios, 71 API calls, **4963s
wall clock** with two tests in flight. The binding constraint is wall clock, not
quota — zero rate limits were hit. Per-call latency on Nemotron ranged from
0.5s to 160s for the same prompt shape, so a request count does not convert to
a time estimate; budget about 20 minutes per test and expect variance.

---

## 8.1 First campaign results

Full numbers and their caveats are in `docs/08` §7.4–7.6. The three things a
reader of this file needs:

1. **Only 1 of 3 influencing payloads landed.** Nemotron ignored a direct
   planted instruction twice. Check `payload_landed` before reading any other
   number on a row.
2. **The one test that landed produced a 2/2 pair-level unsafe preservation** —
   the estimator cleared both pairs it examined while the outputs carried the
   token. Event-level unsafe was 0 on the same test, because verification had
   already forced a full restart. Both numbers are true; that is why both are
   reported.

   **Root-caused 13-09-2026, and the two pairs failed for two different
   reasons** — one a confound in the counterfactual (fixed, D-062), one the
   comparator and the ground truth not measuring the same thing (a limitation,
   D-064). `docs/08` §7.5.1 has the full account; `python -m
   src.eval.relay_diagnosis` reproduces it offline with no API key. Read it
   before quoting the 2/2.
3. **The method-comparison table is not a clean comparison.** Every original run
   failed the task for a reason unrelated to the attack, verification demands
   absolute task success, so CausalLine escalated to `restart_all` on every
   scored test while the baselines — which do not verify — were not charged for
   the same failure. `docs/03` #15.

---

## 9. What this changes about "Limited external validity", and what it does not

### It reduces the limitation

Before, exactly one live measurement existed in the repository: Phase 13.2's
token validation, one channel, five pairs, one run. Everything else was a
scripted agent.

Now:

1. **The whole recovery pipeline runs against a real model**, not just the
   estimator: attribution, refinement, contamination, planning, selective
   replay, verification, escalation and all four methods scored side by side.
2. **The tests are not ours to write.** The causal structures are enumerated
   and drawn by seed; the payloads, cover stories and task paraphrases come
   from a model. A scenario cannot be tuned to the result because nobody wrote
   it with the result in view.
3. **Whether an attack lands is now a measurement.** In the scripted mode the
   influencing / exposed-only distinction is a rule we wrote, which D-025
   already flags as untrue of live runs. Here it is observed, per run, per
   model, and reported as a rate.
4. **The failure modes found are real ones.** Every one of these is impossible
   against a deterministic scripted client, and every one was found by running
   this mode once:
   - a model returning degenerate JSON mid-pipeline (D-056)
   - a model **quoting its own source labels back**, which re-parsed as block
     boundaries and killed a recovery replay outright (D-061)
   - a trace header naming the configured default model rather than the one
     that answered (D-057, D-059)
   - a flaky endpoint passing a cheap liveness probe and then failing every
     real workload (§2, §7.4)
   - verification demanding absolute task success, which makes CausalLine
     escalate to full restart whenever the workflow was already failing for
     reasons unrelated to the attack (`docs/03` #15) — the largest single
     effect on the first campaign's numbers, and it runs against us

### It does not remove the limitation

**Say none of the following.**

1. **That external validity is established.** One reliably reachable model is
   not a sample of models. The cross-model question — does CausalLine's
   behaviour depend on which LLM is behind the agents — is **not answered by
   this work**: one of the three specified models is retired and another is
   intermittent (§2), so model assignment tracked endpoint availability rather
   than any design of ours. Splitting these results by model would compare
   "the model that answered" against "the model that answered less often".
2. **That the observed ground truth is complete.** It sees influence that
   leaves a token behind. An influence expressed some other way is invisible,
   and a run in which one occurred *understates* contamination — which is the
   dangerous direction. This is the same narrowness as docs/06 §2.1, and it is
   why an unsafe-preservation count of zero here is weaker evidence than the
   same count in the scripted matrix.
3. **That real-LLM numbers can be pooled with scripted ones.** Different ground
   truth, different determinism, different repetition structure. `campaign.py`'s
   intervals cover self-report noise at a fixed model; these cover a model's
   own variation at n=1 per cell.
4. **That the generated scenarios are a representative attack distribution.**
   They are a stratified sample of a design space *we* enumerated. The space is
   a modelling choice and its coverage of real prompt injection is unmeasured.
5. **That a scenario the model ignored says anything about safety.** Zero
   unsafe preservations on a run where nothing was contaminated is arithmetic,
   not a result. Check `payload_landed` first.

**The honest summary:** docs/06 §5 said the only non-circular live measurement
was one channel and five pairs. That sentence is now too strong to leave as it
is, and it is replaced rather than deleted — the limitation moves from *"we
have essentially no real-model evidence"* to *"we have real-model evidence on
one model, under a token-scoped ground truth, and the cross-model question is
open."* That is a smaller claim than the brief hoped for, and it is the one the
measurements support.
