# CausalLine: Combined Remediation Plan — Phase-Gated
## Every phase has a checklist. Every box needs evidence, not a claim. Do not start a phase until the previous one's every box is checked with proof.

---

## 0. Ground rules for this entire document

- Two audits are being merged here: **your own coding agent's relay-confound investigation** (real, verified, with actual fixes already made — cited as "Investigation A" below) and **your friend's 21-question architectural audit** (cited as "Investigation B" below). Where B repeats something A or an earlier audit already found, it isn't re-listed as new — only B's genuinely new findings appear here.
- **A phase is complete when every checkbox in it has a specific, reproducible proof attached** — a command, a test name, a number, a file. "I addressed this" is not a completed box.
- If a phase's investigation shows the problem doesn't actually exist the way it was described, that's a valid, complete outcome for that box — write down what you checked and what you found, the same standard as before.
- Do not skip to Phase 8 (the final re-experiment) until Phases 0–7 are each fully checked. The final experiment is only meaningful once everything upstream of it is trustworthy.

---

## Phase 0 — Reconcile a claim before anything else is built on it

**Source: Investigation B, Q18.** It reports that across 24 configurations, `invalidate` and `replay` were selected 73 and 22 times respectively, while `restart(agent)`, `isolate`, and `restart_all` were selected **zero** times, concluding the safe-frontier/checkpoint machinery has no practical effect. This needs to be checked against what earlier campaigns already showed — escalation has been observed reaching `restart_all` under the blind and pessimistic detectors in prior runs.

- [ ] Determine exactly what "24 configurations" measured — the planner's own direct, first-pass selection, or the outcome after escalation is included. State which.
- [ ] Reconcile this against actual campaign data: report, with real numbers, how often each of the five actions is selected (a) as the planner's first choice and (b) after any escalation, across the full current campaign matrix.
- [ ] Determine whether the safe frontier is genuinely inert, or whether it's simply that `invalidate`/`replay` are almost always cheaper so they nearly always win the greedy comparison — these are different findings and the report must say clearly which one is true. If the frontier still correctly bounds what `restart(agent)` would cost even when it doesn't win, that's a meaningful distinction to state, not a footnote.

---

## Phase 1 — Close the comparator-blindness gap (e0013) and generalize the fix

**Source: Investigation A (root cause proven), independently confirmed by Investigation B Q3.** Redaction worked correctly for this pair; the output genuinely changed; no comparator facet represents "the answer quotes the removed source." This is the still-open half of the relay-confound finding, deliberately left unfixed and pinned rather than papered over.

**Proposed solutions already on the table (Investigation A):**
- D-064 (specified, not yet implemented): a "quotes the removed source" facet. Investigation A deliberately did not implement this immediately after seeing the failure, citing D-026's pre-registration rule against adding detection capability reactively right after seeing what it would catch.
- A nested counterfactual: before trusting any clean verdict, verify the source has no *other*, unremoved route into the prompt — described as "one extra call per pair," not yet built.

**What this phase must do:**
- [ ] Make the D-064 decision properly, not reactively: determine what the facet would need to look like to be principled rather than shaped exactly around this one failure, get a second opinion on whether adding it now violates the spirit of D-026 or is a legitimate generalization, and document the reasoning either way.
- [ ] Implement the nested-counterfactual removability check: before a clean verdict is trusted, confirm the source has no second, unremovable route into the prompt (the same class of bug that caused e0014). Record this check as its own evidence type, distinct from a plain counterfactual pass.
- [ ] Add a regression test that fails on e0013's exact scenario before this phase's fix and passes after.
- [ ] Confirm, via `test_step2_covers_step4.py` or an equivalent, that this fix does not repeat Investigation A's first failed attempt — short-circuiting before the call and silently destroying real positive edges.
- [ ] Address docs/03 #16 directly: leave-one-out is only sound when every route from a source into the prompt is removable, and nothing currently enforces this project-wide. State whether the nested-counterfactual check above is a general enforcement of this or a narrow patch for this one case.
- [ ] Address docs/03 #17: a false clean can be laundered into a structural clearance via carrier records, re-emerging with the system's highest trust label. `code_path_pairs()` already filters this; `ClearancePolicy` does not. Make it consistent.
- [ ] **(Investigation B, Q20)** Make post-recovery verification independently re-check contamination rather than trusting "the flagged source was redacted, therefore this event is clean." Decide and implement what an independent re-check actually means here, and confirm it would have caught e0013/e0014-style cases before Phase 1's fix, not just after.
- [ ] **(Investigation A, D-065)** Task success currently hinges on a single fragile formatting detail, and `verify()` is its sole consumer — meaning an unrelated bug can make the one method that honestly checks its own work look like it failed, while methods that don't check are never penalized. Make task-level success checking more robust to this specific class of unrelated failure, or explicitly document the fragility as a known testbed limitation if a robust fix isn't feasible now.

---

## Phase 2 — Turn on the robustness machinery that every reported experiment has been running without

**Source: Investigation B, Q2.** `repeats=1` and `control_run=False` everywhere. The mechanisms built to guard against LLM variability and to detect exactly the class of confound found in Phase 1 have never actually been exercised in a reported result.

- [ ] Re-run a representative subset of scenarios with `repeats>1` and `control_run=True`. Report whether the control run catches any additional relay-confound-style cases beyond e0013/e0014 — this is a real, open question, not a formality.
- [ ] Calibrate the remaining four comparators (prose, code, json_shape, tool_args) with real noise-floor measurements — only the decision comparator has ever had one.
- [ ] Report the token-cost impact of enabling these mechanisms honestly — this will likely raise `A`, and that's an expected, reportable trade-off, not something to minimize.

---

## Phase 3 — Fix the structural fragilities that are real bugs, not open scope questions

**Source: Investigation B, Q8 and Q20.**

- [ ] Replace `SplicingClient`'s position/call-order matching with an explicit identity check: assert the rerun's `(agent_id, kind)` sequence matches the original before splicing a stored output onto it. Add a test that would have caught a desynchronized replay silently splicing the wrong content.
- [ ] Make `redact_flagged()` fail loudly, not silently, when it cannot find a flagged source in a prompt it's supposed to redact from. This is the same mechanism class that caused e0014 — a redaction that doesn't fully happen must never be indistinguishable from one that did.

---

## Phase 4 — Decide, implement, and document the upstream-attribution boundary

**Source: Investigation B, Q10–Q12, Q17.** The system currently treats every flagged source as the true origin and never asks whether that source was itself produced by something earlier and still-unidentified. `derived_from`, `ancestors()`, and `causal_past()` already exist in the codebase for other purposes but are never used for this.

- [ ] Implement a basic backward walk: given a flagged source, check whether it has a `derived_from` link to an earlier event, and if so, recursively surface that earlier event's own upstream sources as additional investigation candidates.
- [ ] Test this on a constructed scenario where the true origin is two hops upstream of what the detector actually flagged, and confirm the walk correctly surfaces it.
- [ ] If, after attempting this, it turns out to be genuinely out of scope for the remaining timeline, that's an acceptable outcome — but it must be a written, reasoned scope decision in the docs, not a silent gap a reviewer discovers.

---

## Phase 5 — Decide and document the detector-interface and timing boundary

**Source: Investigation B, Q6, Q7, Q9, Q15.** Detector confidence and detection timing are computed but discarded — only a flat list of flagged source IDs crosses into recovery. The system is post-hoc/batch only; there is no online, mid-execution detection-and-recovery path, despite architecture documentation sometimes implying agent-level compromise detection that the actual `Verdict` object cannot represent.

- [ ] Wire detector confidence into at least the investigation-ordering step (prioritize which flagged sources get checked first when budget is limited) — cheap, concrete, and closes part of this gap without a large redesign.
- [ ] Write an explicit, permanent scope statement: this system is post-hoc/batch recovery, not online/mid-execution recovery, and state why that's the right scope for now rather than leaving it as an implicit assumption.
- [ ] Correct any documentation that currently implies agent-level detection when the actual interface only ever represents source-level flags.

---

## Phase 6 — Re-evaluate self-report now that the foundation under it is harder

**Source: Investigation B, Q1.** Self-report only ever triages which candidates get checked first; it never independently establishes influence. Now that Phases 1–3 have hardened the actual attribution mechanism, measure whether self-report is still earning its keep.

- [ ] Measure how often self-report's prioritization actually changes total investigation cost or outcome, post-Phase-1-3 fixes.
- [ ] Make and document one of: keep as a pure cost-ordering heuristic, remove entirely, or keep unchanged — backed by the measurement above, not intuition.

---

## Phase 7 — Memory rollback scope decision

**Source: Investigation B, Q19.** Memory is currently reset wholesale to the initial fixture state on every recovery attempt, rather than selectively rolling back only the specific writes that were actually contaminated.

- [ ] Determine whether selective per-write memory rollback is cheap to add given the existing `derived_from`/checkpoint infrastructure, or genuinely out of scope.
- [ ] Either implement it with a test proving a clean write survives a recovery that only needed to roll back a different, contaminated write, or document explicitly why wholesale reset remains the accepted approach for now.

---

## Phase 8 — Final re-experiment, only after every box above is checked

Do not start this phase early. Once Phases 0–7 are all checked:

- [ ] Run the full test suite. Report pass count.
- [ ] Run the full 30-repetition campaign fresh. Report the complete results table, including anything that changed from the last stored version, and explain every change — don't just show new numbers next to old ones without an explanation for each delta.
- [ ] Re-run the economics analysis fresh.
- [ ] Re-run the real-LLM diagnostic (`relay_diagnosis` or its successor) to confirm e0013 is now resolved and e0014 remains resolved.
- [ ] Produce one consolidated report: what changed, what the new headline numbers are, and — critically — an explicit statement of what is now believed to be true about the system's causal-attribution claims that could not honestly have been said before this phase-gated pass began.

**The report for this phase is the actual deliverable of the whole document.** Everything from Phase 0 onward exists to make that final report trustworthy.