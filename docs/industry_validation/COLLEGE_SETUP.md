# Running the industry-validation campaign on the college machine

No Python file needs editing. Everything is configuration.

---

## 1. Three commands

```bash
# 1. write the default config and create the artifact root
python -m experiments.industry_validation setup

# 2. check THIS machine, at THIS configured scale, inside THESE quotas
python -m experiments.industry_validation preflight --config configs/industry_validation.json --probe

# 3. run the campaign
python -m experiments.industry_validation run --config configs/industry_validation.json
```

Resume after any interruption with **the same command plus the run id**:

```bash
python -m experiments.industry_validation run --config configs/industry_validation.json --run-id <RUN_ID>
```

Progress of a run, at any time:

```bash
python -m experiments.industry_validation status            # the latest run
python -m experiments.industry_validation status --run-id <RUN_ID>
```

---

## 2. Setup, once

```bash
git clone <this repo> && cd "Agent Recovery Algo"
git checkout exp/industry-workflow-validation

python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux

python -m pip install -r requirements.txt   # numpy, matplotlib, pytest only
cp .env.example .env                        # then fill in the keys you have

ollama serve &                              # in its own terminal
ollama pull llama3:latest                   # or llama3.2:3b on a small GPU

python -m pytest tests/ -q                  # expect: 749 passed, 1 skipped
python -m experiments.industry_validation setup
```

The core has **zero third-party dependencies**; `requirements.txt` covers the
plotting and test tooling only. The campaign config is **JSON, not YAML**, so
nothing extra is installed to read it.

---

## 3. The one thing you must edit

`configs/industry_validation.json` ships with provider limits marked
**`CONFIGURE ME`**. Replace them with this project's real numbers before
launching:

```json
"providers": {
  "gemini": {"rpm": 10, "rpd": 1500, "tpm": 250000, "attempts": 3,
             "max_usd": 5.0, "source": "AI Studio, checked 2026-11-06"},
  "nvidia": {"rpm": 40, "rpd": 10000, "attempts": 3, "max_usd": null,
             "source": "account console, checked 2026-11-06"},
  "local":  {"rpm": null, "rpd": null, "attempts": 2,
             "source": "local inference; no provider quota"}
}
```

Read the Gemini numbers from AI Studio for **the project the key belongs to** —
limits are project, model and tier dependent, and
<https://ai.google.dev/gemini-api/docs/rate-limits> is the reference. Do not
assume a paid project is unlimited.

> `max_usd: null` means **no cap**. `max_usd: 0.0` means **no spend allowed**,
> and because NVIDIA's price table is `0.00` a cap of `0.0` refused every call
> and silently fell back to local in the first dry run. Null is the default for
> a credit-billed provider.

**Preflight will refuse to pass if the configured quota cannot fund the
configured campaign.** That is deliberate: the alternative is discovering it
six hours in. It will not reduce the experiment for you.

---

## 4. What the campaign is

| | |
|---|---|
| architectures | **7**, each 59–70 agents, each traced to a source in [`SOURCES.md`](SOURCES.md) |
| contamination | **3** levels: small, medium, large |
| trials | configurable, default **3** (seeds are fixed, so a trial is reproducible) |
| recovery methods | **5**: B0, B1, B2, CausalLine, IREC |
| placements | architectures × levels × trials = **63** at 3 trials |
| recovery runs | placements × methods = **315** at 3 trials |

Every method is scored against the **same** original trace, the **same**
detector verdict and fresh copies of the **same** scenario's fixtures. One
placement produces one original; there is no code path that generates a second.

### Provider distribution

Default target **Ollama 50% / Gemini 40% / NVIDIA 10%**, set in
`provider_shares` as weights. Assignment is deterministic per logical agent, so
the same agent gets the same provider in every trial. Synthesis, review and gate
roles prefer the hosted providers, so Gemini does substantive work rather than a
random 40%.

If the quota cannot fund that split, change `provider_shares` **explicitly** —
for example `{"local": 16, "gemini": 3, "nvidia": 1}` — and the manifest will
record what you ran.

### Fallback

Assigned provider → 2–3 bounded retries with exponential backoff and jitter,
honouring `Retry-After` → then the chain `local → nvidia → gemini`. A 429 or
5xx is retried; an over-quota or auth error removes the provider and opens its
circuit; a malformed request is **not** retried and does **not** fall back,
because a second provider returns the same refusal.

Every attempt is recorded with its assigned and actual provider and model,
whether a fallback occurred and why, the delay, the tokens, the latency, the
error category and the estimated USD. **A fallback is never treated as
equivalence**: a different model may produce a different output, and the record
exists so those invocations can be excluded or annotated.

---

## 5. Where the results go

```
artifacts/industry_validation/<RUN_ID>/
  config/        config.json, environment.json (git commit, platform), SOURCES.md
  traces/        one original trace per placement, plus its scenario manifest
  recovered/     one recovered trace per (placement, method)
  results/       one JSON per (placement, method) -- the scored result
  checkpoints/   units.jsonl, the resume ledger
  logs/          errors.jsonl
  providers/     calls.jsonl, one record per invocation with every attempt
  reports/       summary.json
```

`artifacts/industry_validation/LATEST` holds the most recent run id.

A run id is `<UTC timestamp>-<git sha>`, with `-stub` appended if it was a
stub run. No key is ever written into any artifact, and a test enforces that.

---

## 6. Rehearsing without spending anything

```bash
python -m experiments.industry_validation run --stub --trials 1 \
    --architectures A --levels small
```

`--stub` substitutes a deterministic stand-in agent: no key, no GPU, no
network. It exercises the executor, all five recovery methods, checkpointing,
resume, the progress display and the cost ledger.

**A stub run is not a measurement.** The run id carries `-stub`, the manifest
records `stub: true`, and every report prints
`STUB RUN -- infrastructure exercise, NOT a measurement`.

---

## 7. If something goes wrong

| symptom | what it means | what to do |
|---|---|---|
| preflight fails on `quota: gemini` | the configured RPD cannot fund the configured campaign | raise the real quota, lower `provider_shares.gemini`, or lower `trials` — explicitly |
| preflight fails on `local model load` | the GPU exists but the model will not load | set `ollama_model` to `llama3.2:3b`, or free VRAM |
| preflight warns `gpu ... no usable device` | nvidia-smi is present but reports nothing | the campaign still runs; the local share will be slow on CPU |
| campaign exits **2** with `CAMPAIGN PAUSED` | every provider refused or the spend cap was reached | fix the cause, then resume with `--run-id` |
| campaign exits **3** | finished but `INCOMPLETE` — some unit failed | read `logs/errors.jsonl`; resume re-runs only what is missing |
| `status` shows fewer units than expected | work remains | resume with `--run-id` |

An incomplete campaign is labelled **INCOMPLETE** and never **PASS**.

---

## 8. Known limitations, before you read any result

1. **19 of 21 cells have an original run that fails the task check.** The
   attack displaces a code, which is the attack working — but it means the
   task check cannot tell "recovery did not restore the task" from "the task
   was already broken". `docs/16-six-investigations.md` §1 measured the same
   thing on the 56-agent workload and found it drove every escalation there.
   `original_task_success` is recorded per placement so cells can be annotated.
2. **Architecture E exercises two channels, not three**, because AutoGen's
   group chat has no shared store — the message thread *is* the state. That is
   a property of its source and is reported through `channel_note()`.
3. **A loop is a fixed second pass**, not a model-chosen one. That buys replay
   determinism, which every recovery method here needs, at the cost of not
   modelling a loop whose length the model decides.
4. **The task is the same for all seven architectures** — site-code
   consolidation — so the architecture is the only independent variable.
5. **`expected` is what the terminal agent can reach**, not every document: a
   hand-off chain does not aggregate, and architecture D's terminal is
   reachable from 4 of its 11 documents.
6. **Comparison is multiset, not sequence.** Five of seven architectures fan
   out and rejoin, where "the order given" is not defined by the workflow. A
   wrong, missing, duplicated or extra code still fails.
