# Running CausalLine on another machine

Written for a machine that is not the one this project was developed on: a
college lab PC or laptop, Windows or Linux, no Docker, a local Ollama, and
API keys that may or may not exist.

**The one thing to know first:** the evaluation that produces every number in
`docs/07` and `docs/08` needs **no model, no key and no network**. It is pure
Python on a bare 3.11+ install. If all you need is to reproduce the research
pipeline, you are three commands from done and can stop at step 3.

Ollama is needed only for the *real-model* evaluation (`docs/09`, `docs/13`,
`docs/14`). API keys are needed only for the three-provider configuration of
`docs/14`, and never for anything else.

---

## 1. Python environment

Python **3.11 or newer**. Check with `python --version`.

```bash
git clone <this repository>
cd "Agent Recovery Algo"

python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
                                 # Windows cmd:        .venv\Scripts\activate.bat
```

## 2. Dependencies

The core has none. Two commands, depending on how much you want:

```bash
pip install pytest                      # enough to run the test suite
pip install -r requirements.txt         # adds numpy + matplotlib
```

`numpy` is needed only by the Lasso fallback in
`src/provenance/budget_attribution.py`; `matplotlib` only by
`python -m src.eval.economics`. Their absence breaks those two things and
nothing else — `startup_check.py` reports it as a WARN, not a FAIL.

`pip install -r requirements-lock.txt` pins the exact versions the published
numbers were produced with. Use it if you are reproducing, not extending.

## 3. Check the machine, then run the offline evaluation

```bash
python startup_check.py --local-only
```

Ten checks, each **PASS**, **WARN** or **FAIL**, and every FAIL comes with the
command that fixes it. It never prints an API key — keys are reported by name
and character count only.

A FAIL on Ollama is fine at this point; the offline evaluation does not use it:

```bash
python -m pytest tests/                           # the suite
python -m src.eval.experiment --detector oracle   # one matrix, all four methods
python -m src.eval.campaign                       # 96 cells x 30 reps (~9 min)
python -m src.eval.economics                      # cost model + figures
```

If `python -m pytest tests/` passes, this machine reproduces the research
pipeline. Everything below is the real-model layer on top of it.

## 4. Ollama

Install from <https://ollama.com/download>, then:

```bash
ollama serve                     # leave this running, or run it as a service
ollama pull llama3               # ~4.7 GB; this is the 8B model
```

Pull `llama3` (8B). **Do not substitute a 3B model to save disk.**
`llama3.2:3b` was tried and failed the 56-agent task on *capability* — it
transposes access codes across seven hops — which is a measurement of the model,
not of the method (`docs/real_mixed_model_60_agent_experiment.md`). If the
machine cannot host 8B, shrink the **workload** instead with `--num-agents`
(§6), not the model.

Then confirm, including one real inference call:

```bash
python startup_check.py --local-only --probe
```

Expect `PASS local inference` with a time. On CPU the first call takes
10–30 seconds; that is normal, not a hang. The published CPU figure is about
**4 tokens/second** with flat concurrency, which is why the runners default to
one request at a time (`docs/local_llm_frontier/01-benchmark-and-config.md`).

### If Ollama is somewhere else

Ollama's own variables are honoured, plus a few of ours. All optional:

```bash
export OLLAMA_HOST=gpu-box:11434       # bare host:port is accepted
export OLLAMA_MODEL=llama3:latest
export OLLAMA_NUM_CTX=4096
export OLLAMA_TIMEOUT_S=900
```

On Windows PowerShell: `$env:OLLAMA_HOST = "gpu-box:11434"`.

Or put them in `.env` — copy `.env.example`, which documents every one. `.env`
is gitignored.

## 5. Mode A — the established 56-agent evaluation

Always smoke-test first. Sixteen agents, one regime, every attack channel; a
wiring fault then costs two minutes instead of an hour.

```bash
python -m src.eval.college_runner --mode baseline56 --local-only --smoke
```

Then the campaign. Each `(seed, regime)` runs in its own process and an
existing result file is skipped, so an interrupted run is **continued**, not
repeated — just re-issue the same command.

```bash
# 3 seeds x 3 regimes = 9 runs, entirely local
python -m src.eval.college_runner --mode baseline56 --local-only \
    --seeds 3 --vary-placement

python -m src.eval.college_runner --status          # what is on disk
python -m src.eval.mixed_validation_report --dir data/results/college-baseline56
```

`--vary-placement` draws *which* records, memory entries and verifiers are
poisoned from the seed. It is the setting the primary result
(`docs/14-workload-varied-validation.md`) was produced under, and the one to
use. Without it the attack placement is frozen, which reproduces the
fixed-workload control (`docs/13`) and measures model non-determinism rather
than workload variation.

**Expect hours, not minutes**, for 56 agents on CPU. Use `--seeds 1
--regimes large` for a single run first, and `--timeout` to raise the per-run
ceiling (default 7200s).

### With API keys, for the three-provider configuration

Drop `--local-only` and 50 agents stay local while 2 go to Gemini and 4 to
NVIDIA — the routing `docs/14` used. Fill in `.env` first, then:

```bash
python startup_check.py                             # probes both providers
python -m src.eval.college_runner --mode baseline56 --seeds 3 --vary-placement
```

A provider that rate-limits is retried once and then answered locally, with the
agent recorded in `degraded` — so a run that hit an unknown limit is visible in
the results rather than silently different. Check that field before quoting a
mixed-provider number.

## 6. Mode B — a topology the machine can afford

The same pipeline, the same recovery code, the same three attack channels, at a
size you choose. `--num-agents` scales the published proportions
(acquisition / normalisers / hub / specialists / verifiers / reviewers /
synth-audit-executor):

```bash
python -m src.eval.college_runner --mode production_network \
    --num-agents 20 --local-only --seeds 2 --vary-placement
```

| `--num-agents` | actual shape |
|---|---|
| 9 | 1/1/1/1/1/1/3 |
| 20 | 4/3/1/4/3/2/3 |
| 30 | 6/5/1/6/5/4/3 |
| 40 | 8/7/1/8/7/6/3 |
| 56 | 12/10/1/12/10/8/3 — the published shape |

Or set stages directly: `--acquisition 8 --normalisers 6 --specialists 8
--verifiers 6 --reviewers 4`. Invalid shapes are refused **before** any token is
spent, with the reason: specialists must equal acquisition (they are
one-per-record), and no stage may exceed the one above it, or the end-to-end
check can never pass however well the models behave.

`--mode production_network` requires `--vary-placement`, because the frozen
placement names agents by index (`ver7`) and those indices only exist in the
56-agent shape. The runner says so and stops rather than failing per run.

### The shapes this covers

The 56-agent architecture *is* a hierarchical manager-worker network with
fan-out and fan-in: acquisition agents fan out, normalisers aggregate, a hub
fans in and broadcasts, specialists fan out again, verifiers and reviewers
aggregate upward. Shared memory and inter-agent messaging are properties of the
pipeline, so all three attack channels — poisoned tool output, poisoned shared
memory, planted inter-agent message — work at every size.

## 7. Resource limits

| Flag / variable | What it controls | Default |
|---|---|---|
| `--num-agents` | workload size — the main lever | 56 |
| `--seeds`, `--regimes` | how many runs | 3, all three regimes |
| `--timeout` | per-run ceiling, seconds | 7200 |
| `--out-dir`, `--workdir` | where results and traces go | under `data/` |
| `--gemini-cap`, `--nvidia-cap` | self-imposed request ceilings | 120, 800 |
| `OLLAMA_NUM_CTX` | context window | 4096 |
| `OLLAMA_TIMEOUT_S` | per-call ceiling | 900 |

Local inference runs **one request at a time** and that is deliberate:
throughput was measured identical at 1, 2 and 4 in flight on CPU while latency
scaled linearly, so concurrency buys nothing and costs predictability. Do not
raise it expecting a speedup.

Under 8 GB of RAM, `startup_check.py` warns and suggests
`--mode production_network --num-agents 12`. The 56-agent run was OOM-killed
twice on a 16 GB development machine when a browser was open, which is why
every runner is resumable.

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `FAIL ollama service ... refused` | the daemon is not running: `ollama serve` |
| `FAIL ollama model` | `ollama pull llama3` |
| `local inference` takes 30s | normal on CPU at ~4 tok/s; raise `OLLAMA_TIMEOUT_S` rather than lowering it |
| `MissingAPIKey` | you dropped `--local-only` without a key. Put it back |
| `this topology has N agents, not 56` | add `--vary-placement` |
| `planted message target verN is outside...` | same cause; the pre-flight caught it before spending anything |
| run killed partway | re-issue the identical command; finished runs are skipped |
| `ModuleNotFoundError: src` | run from the repository root, the directory with `pyproject.toml` |
| 7 skipped tests | the Lasso tests need numpy: `pip install -r requirements.txt` |
| a provider agent appears in `degraded` | it rate-limited and was answered locally; the run is still valid but is not a clean three-provider run |

## 9. What you get

```
data/results/college-baseline56/
├── seed20260917-small.json         one run: metrics, placement, topology,
├── seed20260917-medium.json        routing, ledgers, degraded agents
├── ...
└── driver-log.json                 status and wall-clock per run

data/runs/college-baseline56/       the traces themselves (gitignored, large)
```

Each result file carries `local_only` and `topology`, so a local-only or
off-size run is identifiable and never pooled with the published
three-provider 56-agent numbers. Report with:

```bash
python -m src.eval.mixed_validation_report --dir data/results/college-baseline56
```

Before quoting anything from a real-model run, read
`docs/13-mixed56-validation.md` §9 Limitation 1 and check `payload_landed`: a
run the model ignored has nothing to preserve unsafely, and its zero unsafe
count is arithmetic rather than evidence.
