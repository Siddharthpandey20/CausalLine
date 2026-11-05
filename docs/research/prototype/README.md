# IREC prototype: isolated research harness

Not production code. Nothing in `src/` imports it. It imports `src/` and reaches the
production replay engine through `run_pipeline` with a client wrapper, exactly as
`src/recovery/replay.py` does.

| file | what it is |
|---|---|
| `cutoff_replay.py` | `CutoffClient` (IREC: splice on byte-identical prompt, otherwise re-execute) plus scoring against scripted ground truth. `run_cp` runs IREC, B0 and B2 on one original trace; `run_cl` runs production CausalLine (hybrid / lazy) on the same cell and pricing |
| `mixed56_cp.py` | Rebuilds each mixed56 run's 145-event trace with a deterministic stub (same placement, same flagged ids), then simulates IREC driven by the run's recorded `events_with_token` and scores it against `truth_events` |
| `py39shim.py` | Import shim so the 3.11 codebase loads under Python 3.9 by prepending `from __future__ import annotations` at load time. Unneeded on 3.11+. Edit `REPO` if the checkout lives elsewhere |
| `scripted_matrix_results.json` | 24 cells: IREC vs B0 / B2 / CausalLine hybrid / lazy |
| `mixed56_workload_varied_sim.json`, `mixed56_validation_sim.json` | per-run IREC simulation |

Reproduce (from the repo root; on 3.11+ drop the shim import):

```bash
PYTHONDONTWRITEBYTECODE=1 python -c "
import sys; sys.path.insert(0,'docs/research/prototype'); import py39shim
from pathlib import Path; from cutoff_replay import run_cp, run_cl
print(run_cp('C', True, Path('/tmp/irec'), mode='exact', workflow='long', cost_model='proportional'))"

PYTHONDONTWRITEBYTECODE=1 python -c "
import sys; sys.path.insert(0,'docs/research/prototype'); import py39shim
from pathlib import Path; import mixed56_cp
mixed56_cp.main(Path('data/results/mixed56-workload-varied'), Path('/tmp/irec-m56'))"
```

Free and deterministic. No API key, no network.
