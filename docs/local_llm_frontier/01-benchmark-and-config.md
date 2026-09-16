# Local LLaMA frontier — 1. Benchmark and configuration

Phases 1 and 2 of the local-LLaMA brief. What the machine can actually do, and
what was wired to it.

Everything here is regenerable:

```bash
python -m src.eval.local_bench --model llama3      --levels 1,2,4 --repeats 2
python -m src.eval.local_bench --model llama3.2:3b --levels 1,2,4 --repeats 1
python -m src.common.local_llama --smoke --model llama3.2:3b
```

---

## 1. The machine, measured rather than assumed

| | |
|---|---|
| GPU | NVIDIA GeForce RTX 3050 Laptop, **6144 MiB** |
| runtime | Ollama 0.17.7 |
| models pulled | `llama3:latest` (8B Q4, 4.9 GB), `llama3.2:3b` (3B, 2.5 GB) |

**The first finding is the one that shaped everything after it: neither model
runs on the GPU.** Ollama's own load record says so —

```
msg=load request="{... Parallel:1 ... GPULayers:[] ... }"
```

`GPULayers:[]` for both, and `ollama ps` reports `100% CPU` for the 8B *and* for
the 2.5 GB 3B, which fits 6 GiB with room to spare. So this is not a
capacity problem that a smaller model solves; the CUDA path is not being used at
all on this install. It is recorded as an environment property rather than
chased, and it is the reason every throughput number below is what it is.

`Parallel:1` in the same record is the second finding, and it independently
explains the sweep.

---

## 2. Phase 1 — the concurrency sweep

The brief forbids guessing concurrency. Both models were swept at 1, 2 and 4
concurrent requests, with repeats, on a prompt shaped like the ones the pipeline
actually sends (a task plus a rendered source, asking for a bounded answer —
benchmarking on "reply ok" would measure the scheduler and not the workload).

### llama3 (8B Q4), 2 repeats per level

| concurrency | failures | timeouts | throughput tok/s | p50 s | p95 s |
|---|---|---|---|---|---|
| 1 | 0 | 0 | 4.06 | 14.9 | 15.2 |
| 2 | 0 | 0 | 4.06 | 29.1 | 31.8 |
| 4 | 0 | 0 | 3.72 | 61.7 | 71.6 |

### llama3.2:3b, 1 repeat per level

| concurrency | failures | timeouts | throughput tok/s | p50 s | p95 s |
|---|---|---|---|---|---|
| 1 | 0 | 0 | 9.50 | 16.7 | 17.0 |
| 2 | 0 | 0 | 9.60 | 32.8 | 33.8 |
| 4 | 0 | 0 | 9.51 | 67.5 | 67.8 |

**Both models show the same shape: throughput flat, latency linear.** Doubling
the requests in flight doubles the time each one takes and completes no more
work per second. That is a fully serialised backend, and `Parallel:1` in the
load record is the mechanism.

### The verdict, and a rule that had to be corrected

| | llama3 (8B) | llama3.2:3b |
|---|---|---|
| **C_safe** | **1** | **1** |
| **C_saturated** | **1** | **1** |
| **C_failure** | none observed | none observed |

**`C_safe` was wrong the first time, and the way it was wrong is worth
recording.** The rule was "the highest concurrency with no failures and
throughput within 90% of the best", which answered **4** — nothing failed at 4,
and throughput there was within 10% of the peak. Running the campaign at 4
would have tripled every latency and produced no extra work per second.

A level that completes every call while running each one four times slower has
not failed, and it is not safe to run a campaign on either. The rule is now
**the smallest concurrency that reaches peak throughput**, because above
saturation extra concurrency is pure queueing. That answers 1, which is the
useful answer and the one the campaign uses.

No OOM or runtime failure was observed at any level on either model, which is
consistent with the work never reaching the GPU.

---

## 3. Phase 2 — the backend

`src/common/local_llama.py`. One class, `LocalLlamaClient`, behind the same
`generate(prompt, system, json_output, temperature) -> LLMResponse` contract
that `GeminiClient` and `NVIDIAClient` already answer. Everything downstream is
unchanged:

```
run_pipeline(client=NVIDIAClient(...))      existing frontier, untouched
run_pipeline(client=LocalLlamaClient(...))  this one
```

`src/common/nvidia.py` and `src/eval/real_campaign.py` are not imported,
modified or called by any of the new modules. The two frontiers share the
pipeline and share nothing else.

Three things the client gets right on purpose, each because the project has
already paid for getting one of them wrong:

- **`model`** — so `run_pipeline()` writes `llama3.2:3b` into the trace header
  instead of falling through to `load_settings()` and stamping every local trace
  `gemini-3.6-flash`. That is D-057, D-059 and D-075, three occurrences of one
  mistake; this backend is not the fourth.
- **`fingerprint()`** — wins over settings for the fields it owns (D-059), and
  carries endpoint, context size, seed and temperature so a run can be tied to
  the runtime that produced it.
- **honest token counts** — Ollama reports `prompt_eval_count` and `eval_count`
  and both are recorded. A backend reporting zero would make the local analysis
  look free, which is the most flattering possible lie and would corrupt every
  cost number the brief asks for.

Degenerate JSON is treated the way D-056 treats it on the hosted path: a
transient generation failure, retried inside the client, with the rejected
attempt's tokens still charged because they were still spent.

### Recorded configuration

| | |
|---|---|
| endpoint | `http://localhost:11434` |
| model | `llama3.2:3b` (campaign), `llama3` (benchmark only) |
| temperature | 0.0 |
| seed | 0 |
| `num_ctx` | 4096 |
| `num_predict` | 512, or 384 for JSON requests |
| timeout | 900 s |
| max attempts | 3 |
| concurrency | **1**, from Phase 1 |

**On determinism (Phase 6 of the brief):** temperature 0 and a fixed seed make
greedy decoding reproducible *for a fixed model and runtime version*. That is
not the same as byte-identical replay, and nothing in this frontier claims it.
The existing decision / code / JSON-shape / tool-args signatures are what
distinguish a real change from wording churn, exactly as on the hosted frontier.

---

## 4. What this costs, and why the campaign is the size it is

Measured on the running campaign rather than estimated: on **llama3 (8B)** a
short-workflow test took roughly **4.5 minutes per model call**, which puts one
test at about two hours and the six-test suite at about twelve. That is not a
campaign anyone can iterate on, and it is why `llama3.2:3b` was pulled — 2.4×
the throughput and shorter answers, at about 20 seconds per call.

**The brief asks for ≥30 repetitions per design point where practical. It is not
practical here**, and that is stated rather than quietly dropped: at the measured
throughput, 30 repetitions of six design points is on the order of a week of
wall clock on this machine. The campaign runs what it can afford, reports `n`
beside every number, and claims nothing that needs a larger `n`.
