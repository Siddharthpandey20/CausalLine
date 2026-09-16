# CausalLine: Combined Remediation Plan — Phase-Gated
## COMPLETED 16-09-2026. Every box below carries its proof: a command, a test name, a number, or a written scope decision.

---

## How to read this file

Every checkbox is ticked and every tick is followed by an indented **proof**
line naming something anyone can re-run or open. Where a box's investigation
found the problem was not what it was described as, that is written down as the
outcome rather than smoothed over — the brief's §0 says that is a valid,
complete result and it happened three times (Phase 0, Phase 6, and `docs/03`
#17).

The work landed in two passes on two branches, merged at `16d5446`:

- **`fd266dd`** — Phases 0–7 and most of 8, by the phase-gated line.
- **`feat/relay-confound`** — the independent root-cause investigation the brief
  calls Investigation A. Its `removability` equivalent was the weaker of the
  two and was dropped at the merge; its three gaps (`relay_diagnosis`, and the
  two identity fields in D-075) were kept.
- **this pass** — Phase 8's one open box, `docs/03` #18, and the fresh re-runs.

Two commands reproduce the state of everything below:

```bash
python -m pytest                       # 398 passed, 114 subtests
python -m src.eval.relay_diagnosis     # both failure shapes, exit 0
```

Full narrative: `docs/10-remediation.md`. Decisions: `docs/05` D-062..D-076.

---

## 0. Ground rules for this entire document

- Two audits are being merged here: **your own coding agent's relay-confound investigation** (real, verified, with actual fixes already made — cited as "Investigation A" below) and **your friend's 21-question architectural audit** (cited as "Investigation B" below). Where B repeats something A or an earlier audit already found, it isn't re-listed as new — only B's genuinely new findings appear here.
- **A phase is complete when every checkbox in it has a specific, reproducible proof attached** — a command, a test name, a number, a file. "I addressed this" is not a completed box.
- If a phase's investigation shows the problem doesn't actually exist the way it was described, that's a valid, complete outcome for that box — write down what you checked and what you found, the same standard as before.
- Do not skip to Phase 8 (the final re-experiment) until Phases 0–7 are each fully checked. The final experiment is only meaningful once everything upstream of it is trustworthy.

---

## Phase 0 — Reconcile a claim before anything else is built on it

**Source: Investigation B, Q18.** It reports that across 24 configurations, `invalidate` and `replay` were selected 73 and 22 times respectively, while `restart(agent)`, `isolate`, and `restart_all` were selected **zero** times, concluding the safe-frontier/checkpoint machinery has no practical effect. This needs to be checked against what earlier campaigns already showed — escalation has been observed reaching `restart_all` under the blind and pessimistic detectors in prior runs.

- [x] Determine exactly what "24 configurations" measured — the planner's own direct, first-pass selection, or the outcome after escalation is included. State which.

      PROOF  `python -m src.eval.action_census`, section (a) vs (b). The audit
      counted the **planner's first-pass selection**. Re-measured here as
      75 / 24 / 0 / 0 / 0 against its 73 / 22 / 0 / 0 / 0 — it reproduces; the
      two extra come from one configuration that now produces a plan under this
      pass's fixes.

- [x] Reconcile this against actual campaign data: report, with real numbers, how often each of the five actions is selected (a) as the planner's first choice and (b) after any escalation, across the full current campaign matrix.

      PROOF  same command, verified 16-09-2026:
      first choice          invalidate 75, replay 24, restart 0, isolate 0, restart_all 0
      every scope replayed  invalidate 75, replay 24, restart **4**, isolate 0, restart_all **4**
      Of 24 configurations: 10 produce no plan (blind flags nothing; heuristic
      misses four), 10 end `selective`, 4 climb the whole ladder and end
      `exhausted`. **A count of selected actions can never show escalation** —
      `invalidation_for_scope()` widens the event set and never consults the
      action vocabulary again, so the machinery runs and leaves no mark on the
      thing the audit counted. Both claims were right about different quantities.

- [x] Determine whether the safe frontier is genuinely inert, or whether it's simply that `invalidate`/`replay` are almost always cheaper so they nearly always win the greedy comparison — these are different findings and the report must say clearly which one is true. If the frontier still correctly bounds what `restart(agent)` would cost even when it doesn't win, that's a meaningful distinction to state, not a footnote.

      PROOF  D-063, and it is **neither**, cleanly. Not inert: of 56
      (configuration, agent) restart actions offered, the frontier made **14
      (25%) strictly cheaper** than restarting that agent from INIT, removing
      **3044 tokens** of recompute. Never wins, and **not on price**: in all
      11 configurations that produced a plan the best-scoring `restart(agent)`
      scored exactly 1.0 on cost/tainted-events-broken — the same as the
      winning `invalidate` — and lost the deterministic `(cost, label)`
      tiebreak. And in **0 of 11** did the frontier lower that best score,
      because the best-scoring restart is always a chain of zero-token events
      costing 1 each. The tiebreak was **not** adjusted to let `restart` win.

---

## Phase 1 — Close the comparator-blindness gap (e0013) and generalize the fix

**Source: Investigation A (root cause proven), independently confirmed by Investigation B Q3.** Redaction worked correctly for this pair; the output genuinely changed; no comparator facet represents "the answer quotes the removed source." This is the still-open half of the relay-confound finding, deliberately left unfixed and pinned rather than papered over.

- [x] Make the D-064 decision properly, not reactively: determine what the facet would need to look like to be principled rather than shaped exactly around this one failure, get a second opinion on whether adding it now violates the spirit of D-026 or is a legitimate generalization, and document the reasoning either way.

      PROOF  D-064, written as a decision with the objection stated first. The
      distinction that settles it: **D-026 forbids facets included or excluded
      on the basis of what they say about influence, and `carryover` is not a
      vocabulary — it shingles the removed source's own content, so there is no
      list to extend, no term to add and no threshold to move.** A hand-written
      "does the answer contain the payload" rule would have been the violation.
      It is also self-cancelling on redundancy: a span present in a source that
      stayed appears on both sides and the facet holds still.
      The second opinion is the merge itself: the independent relay-confound
      line reached the *opposite* first conclusion — it specified the same facet
      and declined to implement it, for the D-026 reason. Both arguments are now
      on the record in D-064 and the reason implementation won is that a facet
      with nothing to tune is not the thing D-026 pre-registered against.
      Cost declared, not buried: `carryover` and the real-LLM canary ground
      truth now share a mechanism, so any real-LLM pair number must be reported
      with the facet excluded. `python -m src.eval.relay_diagnosis` §5 prints
      both columns for exactly that reason.

- [x] Implement the nested-counterfactual removability check: before a clean verdict is trusted, confirm the source has no second, unremovable route into the prompt (the same class of bug that caused e0014). Record this check as its own evidence type, distinct from a plain counterfactual pass.

      PROOF  `src/provenance/removability.py`, D-066. Stronger than the proposed
      nested call **and free**: the question is about the prompt, not the model,
      and the prompt is on disk — so it redacts, shingles the source's content,
      and asks which spans still occur, naming where. Zero extra model calls,
      and a model that happened to answer the same way twice would have proved
      nothing. Recorded as its own evidence type:
      `removability=verified` / `residual:N` / `unchecked` in `CheckRecord.notes`,
      read back through `removability.verdict_of()` so there is one spelling.
      `tests/test_relay_confound.py::TestRemovability`.

- [x] Add a regression test that fails on e0013's exact scenario before this phase's fix and passes after.

      PROOF  `tests/test_relay_confound.py::TestCarryoverFacet`.
      `test_the_old_comparator_alone_would_have_called_this_clean` reaches the
      pre-D-064 comparator through `Calibration.excluded` — the pre-registered
      mechanism — and asserts `clean`;
      `test_the_carryover_facet_catches_it` asserts `tainted` with `carryover`
      as the facet that moved. Also end-to-end in
      `python -m src.eval.relay_diagnosis` §3, on a full pipeline run, in both
      the token and the no-token variant.

- [x] Confirm, via `test_step2_covers_step4.py` or an equivalent, that this fix does not repeat Investigation A's first failed attempt — short-circuiting before the call and silently destroying real positive edges.

      PROOF  `tests/test_relay_confound.py::TestCarryoverFacet::test_the_call_is_still_made`
      asserts the counterfactual call is issued and that the redacted prompt no
      longer carries the payload. `tests/test_step2_covers_step4.py` passes,
      including `test_paths_are_still_reported_even_though_step2_ignores_them`
      — which is the exact test that caught the short-circuit when Investigation
      A tried it, and which goes empty the moment positive edges are discarded.
      `src/provenance/estimator.py` is explicit: the call is made either way and
      a moved signature is kept either way; a failed removability check forbids
      only the *other* verdict.

- [x] Address docs/03 #16 directly: leave-one-out is only sound when every route from a source into the prompt is removable, and nothing currently enforces this project-wide. State whether the nested-counterfactual check above is a general enforcement of this or a narrow patch for this one case.

      PROOF  `docs/03` #16, answered. It is **general enforcement on every
      counterfactual path** — single-source, group-tested and merged-unit all
      route through the same check, and `docs/06` §2.5 states the residual
      limit plainly: detection is **textual**, so a relay the pipeline
      *paraphrased* rather than quoted is not found. Ours only ever quotes, so
      the enforcement is complete here and not in general. That is a narrower
      claim than "solved" and it is the one the measurement supports.

- [x] Address docs/03 #17: a false clean can be laundered into a structural clearance via carrier records, re-emerging with the system's highest trust label. `code_path_pairs()` already filters this; `ClearancePolicy` does not. Make it consistent.

      PROOF  D-067, `src/provenance/carriers.py`, and the finding is **worse
      than the box describes** — which is the Phase-0-style outcome §0 allows.
      The common case was not a laundered *clean* verdict but laundered
      **silence**: the upstream self-report pass records nothing for a negative,
      and the carrier wrote that nothing out as `clean / structural / 1.0`.
      A derived clearance now inherits the method and confidence of the thing it
      derives from; "not in the upstream context" is kept as a separate and
      genuinely structural answer; resolution happens at read time because
      `Trace.validate()` refuses two records for one pair.
      `tests/test_relay_confound.py::TestCarrierClearances`, four tests,
      including `test_the_policy_and_ground_truth_agree_on_what_a_carrier_is`
      — which is the consistency this box asked for.

- [x] **(Investigation B, Q20)** Make post-recovery verification independently re-check contamination rather than trusting "the flagged source was redacted, therefore this event is clean." Decide and implement what an independent re-check actually means here, and confirm it would have caught e0013/e0014-style cases before Phase 1's fix, not just after.

      PROOF  D-068, `surviving_payload()` in `src/recovery/verify.py`. An
      independent re-check here means **reading the recovered bytes** — the
      replayed event's output and the prompt actually issued — for the flagged
      source's material, rather than reasoning from the fact that
      `redact_flagged()` was called. It is independent of the estimator: it
      consults no check record, no influence edge and no comparator.
      It would have caught e0014 before Phase 1: that pair's defining property
      is that the payload is *still in the issued prompt*, which is exactly what
      this reads. `tests/test_recovery_hardening.py::TestIndependentRecheck`.
      Building it exposed `docs/03` #18 — **now also closed**, see Phase 3.

- [x] **(Investigation A, D-065)** Task success currently hinges on a single fragile formatting detail, and `verify()` is its sole consumer — meaning an unrelated bug can make the one method that honestly checks its own work look like it failed, while methods that don't check are never penalized. Make task-level success checking more robust to this specific class of unrelated failure, or explicitly document the fragility as a known testbed limitation if a robust fix isn't feasible now.

      PROOF  D-065 — **fixed, not documented away.** `task_outcome()` reads the
      dates out of stdout and compares them to `task/expected_iso`, instead of
      requiring stdout to equal the expected lines exactly. A banner line, a
      trailing blank, or a stray comment no longer fails the only method that
      checks its own work; a wrong *date* still does, which is the thing the
      check is for. `tests/test_recovery_hardening.py::TestTaskSuccessRobustness`.
      D-069 keeps the strictness that matters and says why: the task check is an
      end-to-end contamination detector, so it is deliberately not weakened past
      the formatting layer. The remaining fragility — the fixture needs `%B` and
      `%b` together — is recorded in D-065 as a testbed property.

---

## Phase 2 — Turn on the robustness machinery that every reported experiment has been running without

**Source: Investigation B, Q2.** `repeats=1` and `control_run=False` everywhere. The mechanisms built to guard against LLM variability and to detect exactly the class of confound found in Phase 1 have never actually been exercised in a reported result.

- [x] Re-run a representative subset of scenarios with `repeats>1` and `control_run=True`. Report whether the control run catches any additional relay-confound-style cases beyond e0013/e0014 — this is a real, open question, not a formality.

      PROOF  `python -m src.eval.robustness`, six CausalLine cells at oracle,
      verified 16-09-2026. **The honest answer is no.** No event's signature
      moved on an unchanged re-send, no verdict changed, no cell's answer moved.
      That is the expected result given the measured 0% floor — a control cannot
      fire on a client that holds still — and it is reported as a *measured*
      zero rather than assumed. Turning them on found one real bug on the way:
      `control_run` was comparing a signature carrying `carryover` against one
      that did not, so every event read as unstable, and a control that always
      fires is not a control
      (`tests/test_recovery_hardening.py::TestControlRunSymmetry`).

- [x] Calibrate the remaining four comparators (prose, code, json_shape, tool_args) with real noise-floor measurements — only the decision comparator has ever had one.

      PROOF  D-070, `python -m src.provenance.scripted_noise`,
      `data/noise/calibration-scripted.json`. Every stored pipeline prompt
      re-sent 20 times unchanged: **0% floor on all 18 (comparator, facet)
      pairs**, `carryover` included, so nothing is excluded.
      This box also turned up a correction it did not ask for: `experiment.py`
      was loading the **Gemini** calibration for scripted runs, so every number
      in `docs/07`/`docs/08` was scored with two decision facets excluded on the
      strength of a different model's measurement. Now loaded with
      `model=SCRIPTED_MODEL` so a future mix-up raises. Measured effect on the
      matrix: none.
      Two honesty notes kept: `tool_args` is absent because tool calls are not
      model calls and are attributed structurally; `code`'s `stdout`/`returncode`
      are absent because no runner is supplied in that context.
      One methodological note that nearly produced a false 0%: the first version
      built a fresh client per trial, resetting the counter the wording churn
      derives from, so every "re-send" came back byte-identical.

- [x] Report the token-cost impact of enabling these mechanisms honestly — this will likely raise `A`, and that's an expected, reportable trade-off, not something to minimize.

      PROOF  `data/results/robustness.json`, re-verified 16-09-2026:
      analysis tokens 4500 → 6900 (**+2400**), replay 800 → 800, work preserved
      78.9% → 78.9%, unsafe 0 → 0, escalations 0 → 0.
      **A/N rises from 1.25 to 1.92** — a 53% increase in analysis cost for no
      change in outcome on this testbed, which makes `docs/03` #7's ratio worse.
      **Default: both stay off**, and the reason is the measurement, not taste.
      They are now reachable, tested and reportable, which is what was missing.

---

## Phase 3 — Fix the structural fragilities that are real bugs, not open scope questions

**Source: Investigation B, Q8 and Q20.**

- [x] Replace `SplicingClient`'s position/call-order matching with an explicit identity check: assert the rerun's `(agent_id, kind)` sequence matches the original before splicing a stored output onto it. Add a test that would have caught a desynchronized replay silently splicing the wrong content.

      PROOF  `docs/03` #13. The pipeline announces `(agent_id, kind)` before each
      model call and `SplicingClient` aborts with `SpliceError` if it disagrees
      with the event the queue is about to hand back — turning the *silent*
      failure (ids still line up, wrong output spliced on) into the loud one.
      `announce` stays optional, so the replay engine does not become a hard
      dependency of one pipeline.
      `tests/test_recovery_hardening.py::TestSpliceIdentity`.

- [x] Make `redact_flagged()` fail loudly, not silently, when it cannot find a flagged source in a prompt it's supposed to redact from. This is the same mechanism class that caused e0014 — a redaction that doesn't fully happen must never be indistinguishable from one that did.

      PROOF  `redact_flagged(..., strict=True)` by default, raising
      `RedactionError`, with a second pass after the loop that re-reads the
      block and refuses if any flagged id is still rendered — so a *partial*
      redaction cannot look like a complete one either. A source that was never
      rendered is still skipped, because that is not a failure.
      `tests/test_recovery_hardening.py::TestRedactionFailsLoudly`.

- [x] **(added this pass)** `docs/03` #18 — a recovered trace stores a prompt that was never sent. Opened by D-068 and deferred; the deferral's reason was that it would blur the campaign diff, and that diff is now recorded.

      PROOF  D-076. The pipeline asks the client what it actually sent
      (`last_issued_prompt()`, optional, read by `getattr` exactly as `announce`
      is) and stores that. The half that was not in the write-up and would have
      bitten: correcting the prompt and leaving `source_block` alone stores two
      texts from different requests, and `splice_block()` refuses a block it
      cannot find — so every counterfactual on a recovered trace would have
      started raising. `_follow_redaction()` performs the same removal on the
      block and returns **None** rather than a guess when the result does not
      land inside the sent prompt.
      `tests/test_recovery_hardening.py::TestStoredPromptIsTheSentPrompt`,
      five tests. They fail on the pre-fix path: `e0013` and `e0014` both store
      the poisoned memory value the recovery reports as redacted.

---

## Phase 4 — Decide, implement, and document the upstream-attribution boundary

**Source: Investigation B, Q10–Q12, Q17.** The system currently treats every flagged source as the true origin and never asks whether that source was itself produced by something earlier and still-unidentified. `derived_from`, `ancestors()`, and `causal_past()` already exist in the codebase for other purposes but are never used for this.

- [x] Implement a basic backward walk: given a flagged source, check whether it has a `derived_from` link to an earlier event, and if so, recursively surface that earlier event's own upstream sources as additional investigation candidates.

      PROOF  `src/provenance/upstream.py`, D-073. Walks back over `derived_from`,
      using the trace's influence edges into the producing event where they exist
      and that event's exposures where they do not. `origin_event` is
      deliberately **not** walked: a web page is not derived from the tool
      response that fetched it.

- [x] Test this on a constructed scenario where the true origin is two hops upstream of what the detector actually flagged, and confirm the walk correctly surfaces it.

      PROOF  `tests/test_scope_boundaries.py::TestUpstreamAttribution::test_a_true_origin_two_hops_up_is_surfaced`
      — and it **did not need constructing**. On the real scripted A-influencing
      run the walk takes the Coder's script back through the Researcher's
      findings to the planted page, two hops, without reading
      `Source.malicious`.

- [x] If, after attempting this, it turns out to be genuinely out of scope for the remaining timeline, that's an acceptable outcome — but it must be a written, reasoned scope decision in the docs, not a silent gap a reviewer discovers.

      PROOF  D-073 draws the line explicitly: it **surfaces and stops**.
      Candidates go onto `RecoveryResult.notes`; nothing is flagged, nothing
      seeds the contamination walk, no recovery decision changes. The reason is
      not timeline — the relation walked is "was an input to", which is
      **exposure**, and promoting it to a seed would re-import the exact
      conflation this project exists to remove and grow the region back towards
      B2's. Pinned by
      `test_candidates_are_not_promoted_to_seeds`.

---

## Phase 5 — Decide and document the detector-interface and timing boundary

**Source: Investigation B, Q6, Q7, Q9, Q15.** Detector confidence and detection timing are computed but discarded — only a flat list of flagged source IDs crosses into recovery. The system is post-hoc/batch only; there is no online, mid-execution detection-and-recovery path, despite architecture documentation sometimes implying agent-level compromise detection that the actual `Verdict` object cannot represent.

- [x] Wire detector confidence into at least the investigation-ordering step (prioritize which flagged sources get checked first when budget is limited) — cheap, concrete, and closes part of this gap without a large redesign.

      PROOF  D-074. Confidence orders the sources **within** an event,
      ascending — least certain flag first, because clearing a doubtful flag
      removes a whole downstream region while confirming a certain one mostly
      re-derives taint. It deliberately does **not** reorder which event is
      taken next: that is the frontier expansion and it is forward for a reason.
      `tests/test_scope_boundaries.py::TestInvestigationOrder`, three tests.

- [x] Write an explicit, permanent scope statement: this system is post-hoc/batch recovery, not online/mid-execution recovery, and state why that's the right scope for now rather than leaving it as an implicit assumption.

      PROOF  `docs/02-architecture.md` and `docs/01-scope.md`, both edited to
      carry it permanently. The reason is about **evidence, not effort**:
      counterfactual replay needs an output that already exists, so there is
      nothing for the method to be applied to mid-execution.

- [x] Correct any documentation that currently implies agent-level detection when the actual interface only ever represents source-level flags.

      PROOF  `docs/02`'s opening diagram said the detector reports "agent X
      compromised". It never did — `Verdict` is a map from source id to
      confidence and cannot express an agent-level claim. Corrected, and the
      difference is not cosmetic: **an agent-level verdict *is* baseline B1**,
      which is the thing CausalLine is measured against.

---

## Phase 6 — Re-evaluate self-report now that the foundation under it is harder

**Source: Investigation B, Q1.** Self-report only ever triages which candidates get checked first; it never independently establishes influence. Now that Phases 1–3 have hardened the actual attribution mechanism, measure whether self-report is still earning its keep.

- [x] Measure how often self-report's prioritization actually changes total investigation cost or outcome, post-Phase-1-3 fixes.

      PROOF  `python -m src.eval.selfreport_value`, six CausalLine cells,
      verified 16-09-2026. `targeted_only` is `hybrid` minus the inline
      self-report and nothing else, which is the only comparison that isolates
      the triage — the existing `--ablation` modes also skip the targeted pass
      and so differ in two things at once.
      analysis tokens 4500 → 1600 (**−2900**), recovery 5300 → 2500 (−2800),
      work preserved 78.9% → 73.7%, unsafe 0 → 0, **pair-level false negatives
      0 → 1**, escalations 0 → 1.

- [x] Make and document one of: keep as a pure cost-ordering heuristic, remove entirely, or keep unchanged — backed by the measurement above, not intuition.

      PROOF  D-072: **keep — and the docs were wrong about what it does.**
      Self-report is not a cost reducer. It *costs* 2800 recovery tokens across
      these cells and it buys safety: its over-claimed positives are recorded as
      `tainted` without verification, so they taint pairs the targeted pass
      alone misses, which is why removing it produces a pair-level false
      negative and an extra escalation. It is a conservative bias bought with
      tokens, not a cost-ordering heuristic, and
      `src/provenance/selfreport.py` and `docs/03` #7 now say so.

---

## Phase 7 — Memory rollback scope decision

**Source: Investigation B, Q19.** Memory is currently reset wholesale to the initial fixture state on every recovery attempt, rather than selectively rolling back only the specific writes that were actually contaminated.

- [x] Determine whether selective per-write memory rollback is cheap to add given the existing `derived_from`/checkpoint infrastructure, or genuinely out of scope.

      PROOF  D-071, and the answer is **both, for two different questions**.
      For the *replay*, wholesale reset is not a shortcut: this testbed's replay
      re-executes the whole workflow from its starting state, so seeding it with
      writes the original run made would show an early `memory_read` a value the
      original never saw, and the replay would stop being a replay.
      For a *deployment*, the audit is right and it is cheap.

- [x] Either implement it with a test proving a clean write survives a recovery that only needed to roll back a different, contaminated write, or document explicitly why wholesale reset remains the accepted approach for now.

      PROOF  `selective_memory_rollback()` returns `{key → value to restore, or
      None to delete}` covering only keys an invalidated write touched,
      reverting each to the last *surviving* write, then the fixture value, then
      removal. A key written only by surviving events **does not appear at all**
      — absent means it stays. Carried on `RecoveryResult.memory_rollback`, with
      notes naming what was undone and what was kept.
      `tests/test_scope_boundaries.py::TestSelectiveMemoryRollback`, four tests,
      including the exact one this box asks for:
      `test_a_clean_write_survives_a_rollback_of_a_different_write`.
      Hand-built two-write trace, because the four-agent testbed writes memory
      exactly once and cannot express the case — stated rather than hidden.

---

## Phase 8 — Final re-experiment, only after every box above is checked

- [x] Run the full test suite. Report pass count.

      PROOF  `python -m pytest` → **398 passed, 114 subtests passed**, ~68s,
      16-09-2026, on the merged tree. Was 391 at the merge; +5 for
      `TestStoredPromptIsTheSentPrompt` (docs/03 #18) and +2 for
      `TestTheDiagnosticStillPasses`.

- [x] Run the full 30-repetition campaign fresh. Report the complete results table, including anything that changed from the last stored version, and explain every change.

      PROOF  `data/results/campaign-30x-final.txt`, 720 cells, 3600 pipeline
      runs. Full table and a per-delta explanation in `docs/10-remediation.md`
      §8.2 and §8.5.

- [x] Re-run the economics analysis fresh.

      PROOF  `python -m src.eval.economics`, 16-09-2026. N=600, A inline 800,
      A targeted 200, f=0.6767, inline selective wins 0/24, targeted 12/24,
      attack-rate threshold **NONE**. All identical to the stored report except
      storage, which came out **25125 B against the 26499 B §8.3 reported** —
      a discrepancy that does not reproduce and is written up rather than
      overwritten, in §8.5.

- [x] Re-run the real-LLM diagnostic (`relay_diagnosis` or its successor) to confirm e0013 is now resolved and e0014 remains resolved.

      PROOF  `python -m src.eval.relay_diagnosis`, exit **0**, and it is the
      *successor* this box allows rather than a hosted run — there is no
      `NVIDIA_API_KEY_*` here, and D-064 makes a token-scored real-LLM
      confirmation of `carryover` circular anyway. The successor measures around
      that circularity instead of declaring it and stopping:
      **e0014 RESOLVED by removability**, shown with `carryover` *excluded* so
      the credit goes to the mechanism that earns it — `removability=residual`,
      1 residual span in the token run and 9 in the prose run. Non-circular:
      a fact about a prompt on disk.
      **e0013 RESOLVED by the carryover facet**, with `removability=verified` at
      that event proving the e0014 fix cannot be what saved it. The pre-D-064
      comparator still returns `clean`; the current one returns `tainted` with
      `carryover` as the facet that moved.
      **The circularity is separated, not asserted:** run 1 matches via the
      *distinctive-token* path (the canary itself — circular, labelled so); run 2
      contains no token-shaped string anywhere and matches via **9 word
      shingles** — so `carryover` is a content-overlap facet, not a canary
      detector, and that is the run that carries the claim.
      Pair scoring is printed **both ways** per D-064's binding rule: unsafe 0
      with the facet, 1 without. Regression-tested in
      `tests/test_relay_confound.py::TestTheDiagnosticStillPasses`, so it cannot
      rot into documentation.

- [x] Produce one consolidated report: what changed, what the new headline numbers are, and — critically — an explicit statement of what is now believed to be true about the system's causal-attribution claims that could not honestly have been said before this phase-gated pass began.

      PROOF  `docs/10-remediation.md`, with §8.5 and §9 written this pass.

---

## What is still owed, and it is one thing

**A real-LLM campaign under this code, scored with `carryover` excluded.** Every
fix above is confirmed against a deterministic reproduction of the failure, not
against a real model. That is the right instrument for mechanisms that are
properties of a prompt and a comparator — but it is not the same claim, and
`docs/09` §9 already forbids the stronger one. It needs quota and an
`NVIDIA_API_KEY_*`; neither exists on this machine. **Until it is run, no
real-LLM number from this pass may be quoted**, and the diagnostic prints that
sentence every time it is run so it cannot be forgotten.
