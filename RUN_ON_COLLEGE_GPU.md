# Run the industry-validation campaign on the college GPU machine

Everything you need, in order. **No Python file needs editing.** One JSON file
needs three numbers checked.

Branch: **`exp/college-campaign`**

---

## Step 0 — get the code

```bash
git clone -b exp/college-campaign https://github.com/Siddharthpandey20/CausalLine.git
cd CausalLine
git log --oneline -1          # expect the campaign-config commit at the tip
```

If the clone gives you `main` instead, the branch did not reach the remote:

```bash
git fetch origin exp/college-campaign
git checkout exp/college-campaign
```

---

## Step 1 — Python and the virtual environment

Needs **Python 3.11 or newer** (3.13 is what this was built on).

```bash
python --version                      # must be >= 3.11

python -m venv .venv
.venv\Scripts\activate                # Windows
source .venv/bin/activate             # Linux
```

**The campaign itself needs no third-party packages** — the core is
dependency-free and the runner imports only the standard library. Install these
two only if you also want to run the test suite and the economics plots:

```bash
python -m pip install --upgrade pip
python -m pip install pytest          # to run the test suite
python -m pip install -r requirements.txt   # numpy + matplotlib, for plots only
```

---

## Step 2 — Ollama and the local model

Install Ollama from <https://ollama.com/download>, then:

```bash
ollama serve                          # leave this running in its own terminal
```

In a second terminal, pull the model. **Which one depends on your GPU's VRAM:**

| GPU VRAM | pull this | set `ollama_model` to | speed |
|---|---|---|---|
| **8 GB or more** | `ollama pull llama3:latest` | `llama3:latest` | the model this project's earlier results used — **prefer this** |
| **6–8 GB** | `ollama pull llama3:latest` | `llama3:latest` | works, tight; measured 2.10 s/call on a 6 GB RTX 3050 |
| **under 6 GB** | `ollama pull llama3.2:3b` | `llama3.2:3b` | ~2.5× faster, but a **different model**, so say so in any write-up |

Check your VRAM first:

```bash
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
```

---

## Step 3 — the `.env` file

```bash
cp .env.example .env
```

Then open `.env` and set what you have. **Nothing is required** — with no keys
at all the campaign runs entirely on local Ollama and records every fallback.

```bash
GEMINI_API_KEY=AIza...            # your Gemini key
NVIDIA_API_KEY_1=nvapi-...        # optional, used as the secondary fallback
```

Leave the rest commented out. `.env` is gitignored and no key is ever written
into a log, a manifest or a result file.

---

## Step 4 — the one file to check

Open **`configs/industry_validation.json`**. Three things to confirm:

```json
{
  "trials": 3,
  "ollama_model": "llama3:latest",
  "providers": {
    "gemini": { "rpm": 10, "rpd": 1500, "max_calls": 350, ... },
    ...
  }
}
```

1. **`ollama_model`** — must match what you pulled in Step 2.
2. **`providers.gemini.rpd`** and **`rpm`** — set these to your project's real
   limits. Read them in Google AI Studio for the project the key belongs to;
   limits are project, model and tier dependent. Reference:
   <https://ai.google.dev/gemini-api/docs/rate-limits>
3. **`providers.gemini.max_calls`** — the soft cap, currently **350**. When
   Gemini reaches it, the provider is refused and every later call goes to
   local Ollama, then NVIDIA. Raise or lower it freely.

Leave `architectures`, `levels`, `trials` and `methods` alone — those define
the experiment.

---

## Step 5 — are we ready? (the check that must pass)

```bash
python -m experiments.industry_validation preflight --config configs/industry_validation.json --probe
```

`--probe` actually loads the model and sends one real local request, which is
the point: a detected GPU does not prove the configured model fits.

**You want `0 fail`.** It looks like this:

```
  PASS python                 3.13.1
  PASS core imports           tracing, provenance, recovery, architectures, IREC all import
  PASS system RAM             15.7 GB, 16 logical cores
  PASS free disk              59.1 GB available
  PASS gpu                    NVIDIA GeForce RTX 3050 6GB Laptop GPU, driver 581.08
  PASS gpu VRAM               6.0 GB
  PASS cuda                   driver reports CUDA 13.0
  PASS architectures          7 runnable, 442 agents configured in total
  PASS ollama service         http://localhost:11434, 2 model(s)
  PASS ollama model           llama3:latest
  PASS local model load       llama3:latest answered in 2.1s
  PASS key: gemini            GEMINI_API_KEY set (39 chars)
  PASS quota: gemini          350 calls ... SOFT-CAPPED at 350, the rest falls back to local then nvidia
  PASS campaign spend cap     $5.00
  ...
  18 pass, 3 warn, 0 fail
  Ready to launch.
```

If anything **FAILS**, it prints the exact fix. The common ones:

| failure | fix |
|---|---|
| `ollama service ... unreachable` | `ollama serve` is not running |
| `ollama model ... not installed` | run the `ollama pull` it names |
| `local model load ... did not answer` | not enough VRAM — set `ollama_model` to `llama3.2:3b` |
| `gpu VRAM 4.0 GB, llama3 needs about 6.0` | same — use `llama3.2:3b` |
| `quota: gemini ... N day(s) of the configured RPD` | lower `max_calls`, or lower `provider_shares.gemini` |
| `free disk ... needs 20 GB` | free space; the full campaign writes about 1.5 GB |

Optional, if you want the suite as well (about 3 minutes):

```bash
python -m pytest tests/ -q            # expect: 733 passed, 1 skipped
```

---

## Step 6 — launch

```bash
python -m experiments.industry_validation run --config configs/industry_validation.json
```

That is the whole campaign: **7 architectures × 3 contamination levels ×
3 trials × 5 recovery methods = 63 placements, 315 recovery units.**

To keep it alive if your SSH session or terminal closes:

```bash
# Linux
nohup python -m experiments.industry_validation run \
      --config configs/industry_validation.json > campaign.log 2>&1 &

# Windows PowerShell
Start-Process -NoNewWindow -RedirectStandardOutput campaign.log `
  python "-m","experiments.industry_validation","run","--config","configs/industry_validation.json"
```

**Expected runtime**, scaled from a measured 2.10 s per local call:

| GPU class | approx. per-call | total |
|---|---|---|
| RTX 3050 6 GB (measured here) | 2.10 s | **~8–9 h** |
| RTX 3060 / 4060 | ~1.2 s | ~5 h |
| RTX 3090 / 4090 / A100 | ~0.5 s | ~2 h |

It is **14,088 model invocations**: 350 to Gemini (the cap), the rest local
with NVIDIA as secondary.

---

## Step 7 — watching progress

The terminal refreshes a block like this after every recovery method:

```
INDUSTRY VALIDATION
Architecture : C-software-delivery
Contamination: medium
Trial        : 23/63
Placement    : C-software-delivery--medium--20260918
Active agents: 62/62

Recovery:
  B0          Complete
  B1          Complete
  B2          Complete
  CausalLine  Running
  IREC        Pending

Placements   : 22/63
Recovery runs: 112/315

Provider usage (assigned -> actual):
  gemini     350 calls     68950 tok  $  0.0067  fail 0  refused 42  fallback in/out 0/42
  local     8104 calls   1596488 tok  $  0.0000  fail 0  refused  0  fallback in/out 42/0
  nvidia     720 calls    141840 tok  $  0.0000  fail 0  refused  0  fallback in/out 0/0
  attempts 9174, fallbacks 42, failed 0, deferred 0
  spend $0.0067 of $5.00 cap
Elapsed 3.41 h; estimated remaining 5.12 h
```

From another terminal, at any time:

```bash
python -m experiments.industry_validation status
```

Or watch the raw counts:

```bash
# Linux
watch -n 60 'ls artifacts/industry_validation/*/results | wc -l'
tail -f campaign.log

# Windows PowerShell
Get-Content campaign.log -Wait -Tail 40
```

---

## Step 8 — where everything is stored

```
artifacts/industry_validation/<RUN_ID>/
├── config/        config.json, environment.json (git commit, GPU, platform), SOURCES.md
├── traces/        one original trace per placement + its scenario manifest
├── recovered/     one recovered trace per (placement, method)  -- 315 of them
├── results/       one JSON per (placement, method) -- the scored result
├── checkpoints/   units.jsonl  <- the resume ledger
├── logs/          errors.jsonl (absent if nothing went wrong)
├── providers/     calls.jsonl  <- every invocation, every attempt
└── reports/       summary.json <- the consolidated result
```

`<RUN_ID>` is `<UTC timestamp>-<git sha>`, e.g.
`20261106T090000Z-5695ae33`. The newest id is also in
`artifacts/industry_validation/LATEST`.

Total size: **about 1.5 GB** for the full campaign.

To bring the results back:

```bash
git add artifacts/industry_validation/<RUN_ID>
git commit -m "Industry validation campaign results, <RUN_ID>"
git push origin exp/college-campaign
```

(Rehearsal runs are gitignored; a real run is not, so it commits normally.)

---

## Step 9 — if it stops

**Resume is the same command plus the run id.** Nothing completed is redone.

```bash
python -m experiments.industry_validation run \
    --config configs/industry_validation.json --run-id <RUN_ID>
```

| exit code | meaning | what to do |
|---|---|---|
| **0** | `CAMPAIGN COMPLETE`, all 315 units | you are done |
| **2** | `CAMPAIGN PAUSED` — every provider refused, or the spend cap was hit | fix the cause, then resume with `--run-id` |
| **3** | finished but `INCOMPLETE` — some unit failed | read `logs/errors.jsonl`, then resume; it re-runs only what is missing |

Safe to do at any time: close the terminal, reboot the machine, kill the
process. A unit counts as complete only if its ledger line says `ok` **and**
its result file exists, so nothing is ever assumed done. An interrupted
campaign was tested: killed at 3 of 5 units, it resumed to 5/5 without
rewriting a completed result or writing a duplicate ledger line.

**An incomplete campaign is labelled `INCOMPLETE`, never `PASS`.**

### Things that will *not* stop it

- **Gemini hitting the 350 cap** → refused, work continues on local, each
  fallback recorded. This is expected and will happen.
- **A 429 or a 503** → 2–3 bounded retries with backoff and jitter, honouring
  `Retry-After`, then the fallback chain.
- **Ollama restarting** → retried, then NVIDIA.
- **One placement faulting** → recorded in `logs/errors.jsonl`, campaign
  continues, final report says `INCOMPLETE`.

### The one thing that will

Every provider failing at once — most likely `ollama serve` having died with no
NVIDIA key configured. The campaign checkpoints and pauses with resume
instructions rather than spinning.

---

## Step 10 — reading the result

```bash
python -m experiments.industry_validation status
cat artifacts/industry_validation/<RUN_ID>/reports/summary.json
```

The summary gives, per method across all 63 placements: unsafe preservations,
recall, contamination escapes, task success, tokens and work preserved — each
with its denominator — plus per-provider calls, tokens, attempts, fallbacks and
USD.

### Read this before quoting any number

1. **Most original runs fail the task check**, because an influencing attack
   displaces a code. That is the attack working, but it means the task check
   cannot separate "recovery failed" from "it was already broken".
   `original_task_success` is recorded per placement.
   See `docs/16-six-investigations.md` §1.
2. **Architecture E exercises two attack channels, not three** — AutoGen's
   group chat has no shared store, so poisoned memory does not apply. A
   property of its source, not a gap.
3. **A fallback is not equivalence.** Where Gemini was capped, those agents ran
   on a different model. `fallback_occurred` is on every record so those
   invocations can be excluded or annotated.
4. **IREC degrades to near-full-restart on architecture D** (a deep hand-off
   chain): 57 of 59 model events re-executed. Expected, not a bug.
5. The architecture is the only independent variable — the task, the attack
   channels and the scoring are identical across all seven.

Full detail: `docs/industry_validation/SOURCES.md` (what each architecture is
grounded in) and `docs/industry_validation/COLLEGE_SETUP.md`.

---

## Quick reference

```bash
git clone -b exp/college-campaign https://github.com/Siddharthpandey20/CausalLine.git
cd CausalLine
python -m venv .venv && .venv\Scripts\activate
python -m pip install pytest
ollama serve                                    # separate terminal
ollama pull llama3:latest                       # or llama3.2:3b if VRAM < 6 GB
cp .env.example .env                            # add GEMINI_API_KEY
#   check configs/industry_validation.json: ollama_model, gemini rpd/rpm
python -m experiments.industry_validation preflight --config configs/industry_validation.json --probe
python -m experiments.industry_validation run      --config configs/industry_validation.json
python -m experiments.industry_validation status   # from another terminal
```
