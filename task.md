# CausalLine: Prove the Break-Even Projection

## Why this is the priority over everything else right now

The local-LLaMA economics analysis found that lazy self-report alone projects `A/N + f` from 1.58 to 0.92 — crossing the break-even line for the first time in this project's history. That number is currently a projection, computed by subtracting self-report's measured cost from runs collected with it still on. It has not been proven the way everything else in this project has been proven: by actually running it and measuring directly.

## Task 1 — Wire the SPRT gate (the two-line fix already identified)

`sprt_investigate.config_for(analysis_tokens, restart_tokens)` derives real thresholds from the actual cost model and has no caller outside its own tests. Pass `sprt_config=config_for(analysis_tokens, restart_tokens)` at the call site in `refine_for_verdict`, replacing the current defaults (`f_star=0.3/0.7`, which are taste numbers, not measurements).

- [ ] Confirm via grep that `config_for` now has a real caller outside its test file.
- [ ] Also fix `planner.greedy_cover`'s cap: it currently compares accumulated replay cost only, never analysis cost, against `restart_all_cost()`. The dominant cost term (A, at 1.18×N) is invisible to the one guard that exists. Include analysis spend already committed in the comparison.
- [ ] Report, before and after, how many investigations now abort early on the existing local-LLaMA traces, and by how much this reduces measured A.

## Task 2 — Run a dedicated campaign with lazy self-report as the default, not projected

- [ ] Flip lazy self-report to default-on for this campaign specifically (D-082's finding that it costs 3.6 points of work-preserved on attacked runs still holds — report that cost again here, don't let it disappear because the campaign has a different goal this time).
- [ ] Run on the local GPU frontier, same design points as the existing campaign, as many repetitions as wall-clock allows — report the real `n`, same discipline as before.
- [ ] Compute `A/N + f` directly from this run's real numbers. Report whether it comes in at, above, or below the projected 0.92, and if it differs, say by how much and give a mechanism, not a shrug.
- [ ] Re-run the paired sign test against B0/B1/B2 under this configuration — confirm work-preservation and precision advantages hold under lazy mode, not just under the mode that produced the original numbers.

## Task 3 — docs/03 #15, for real this time

This has now blocked the task-recovery conclusion on three independent real-model campaigns. Investigate the actual generation/scoring mismatch directly — not another workaround, the root cause — and either fix it or produce a specific, evidenced explanation of why it can't be fixed without changing the testbed's task design.

## What "done" looks like

A real, measured `A/N + f` number from an actual campaign run under lazy self-report, standing next to the 0.92 projection — either confirming it or explaining precisely why it differs. That comparison is the deliverable. Everything else in this brief supports getting to that one number honestly.