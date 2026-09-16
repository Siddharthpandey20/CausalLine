# CausalLine: Fix the Located Bugs, Adopt the Good Proposal, Scrutinize the Risky One
## Phase-gated. Every phase needs a before/after number or test, same standard as every prior round.

---

## Phase 1 — Fix the two bugs the last report found and deliberately left open

Both were correctly *not* quietly patched — they're logged as D-077, D-078, and issue #19. Fix them now, but preserve the same discipline that got them logged instead of silently patched: every fix here must show its before/after delta explicitly, especially where the fix makes CausalLine's own numbers look better.

### 1a. The canary-token / circularity tension (issue #19a)

**The bug:** ground-truth canary tokens are always exactly 8 characters. The estimator's `distinctive_spans()` helper — which the carryover facet (D-064) depends on to notice "the answer quotes something suspicious from the removed source" — only promotes spans of 10+ characters. Every real canary token is structurally invisible to the carryover facet, by construction. This is what let a genuine unsafe preservation through in gen001.

**Why this isn't a one-line fix:** simply lowering the threshold to 8 would make the carryover facet sensitive to exactly the same pattern the ground-truth generator uses to build canary tokens — recreating the circularity that D-064 declared as a risk and that this round's testing showed does *not* currently exist, only because the threshold mismatch happens to keep them apart. Closing the coverage gap naively reopens the circularity gap. Do not do this the naive way.

- [ ] Do not simply change the number 10 to 8. Design a fix where the ground-truth marker mechanism and the estimator's own span-detection mechanism are structurally decoupled — different detection logic, not just different thresholds on the same logic — so lowering one can't accidentally make it mirror the other.
- [ ] Prove the fix closes the coverage gap: reconstruct gen001's exact scenario and show the carryover facet now correctly flags it.
- [ ] Prove the fix does not reopen the circularity: construct a case where the carryover facet's verdict and the true ground truth *disagree*, and confirm the facet still produces its own independent answer rather than trivially matching ground truth by shared construction. If you cannot construct a disagreeing case, that itself is evidence of remaining circularity — say so.

### 1b. The ISO_DATE task-check regex (issue #19b, same class as D-065)

**The bug:** the task-success pattern's trailing `\b` doesn't match a legitimate ISO datetime like `2024-03-12T00:00:00`, so a genuinely correct run gets scored as a failure, which then makes recovery look like it failed even though the actual work was fine — and only CausalLine is charged for it, since it's the only method that checks task success at all.

- [ ] Fix the regex.
- [ ] Re-run only the specific campaign cells this affects, report the before/after numbers for exactly those cells, and state plainly that this change was expected to move CausalLine's numbers upward — don't let that go unstated the way D-065 warned against.

---

## Phase 2 — Attempt to restore a second model, bounded effort

DeepSeek is currently failing preflight; MiniMax is confirmed retired (410) and not worth pursuing further.

- [ ] Spend a bounded, small amount of time (not open-ended) debugging why DeepSeek fails preflight specifically. If it's a fixable configuration issue, fix it. If it's a genuine external availability problem, document that plainly and move on — don't let this block anything else.

---

## Phase 3 — Build the lazy/on-demand self-report architecture (the good half of the teammate proposal)

This part of the proposal is sound: querying self-report on every event during every clean run, before anyone knows whether an attack even happened, is real, unnecessary cost. Deferring it to only fire for agents actually exposed to a flagged source, only after detection, doesn't weaken safety — the existing "unchecked defaults to tainted" policy already means nothing gets treated as clean before it's checked, whether that check happens eagerly or lazily. But verify that claim rather than assume it.

- [ ] Before switching anything, check whether any other part of the system currently assumes self-report records already exist inline during normal execution (calibration measurement, checkpoint garbage collection eligibility, anything else that reads check records mid-run). List what you checked, even the things that turned out fine.
- [ ] Implement the lazy trigger: no self-report calls during normal execution; only fire, targeted at the specific exposed agents, once a detector flags a source.
- [ ] Measure the actual token savings on clean (no-attack) runs, and confirm the previously-measured safety value of self-report (the ~2800 recovery-token figure from the last report) is unchanged on attacked runs — this is a real architecture change, so re-measure rather than assume the old number still applies.

---

## Phase 4 — Do not build the hybrid comparator cascade yet. Resolve two open tensions first.

This part of the proposal needs real scrutiny before any implementation time goes into it — not because the idea is bad, but because it collides with two things already established in this project, and neither collision is addressed in the proposal as written.

### 4a. It reintroduces exactly what D-026 already declined, without addressing why

D-026 explicitly ruled out an LLM-as-judge approach, on the grounds that a judge is itself an uncalibrated instrument whose own noise floor would need measuring before it could be trusted — the same standard every comparator facet was held to. The proposed "Tier 2 / Stage 3 micro-judge" is exactly that judge. Adopting it silently, without addressing the decision it contradicts, is not acceptable.

- [ ] Either write a clear, reasoned amendment to D-026 explaining specifically what's different now that makes a judge acceptable when it wasn't before, or don't build this tier.
- [ ] If the judge tier is adopted, it must get the same noise-floor calibration every other comparator received — run it on unchanged re-sends, measure how often it disagrees with itself, exclude or account for that instability the same way every other facet's calibration works. Do not exempt this one component from the standard applied to everything else.

### 4b. Prove, don't assume, that the new design would have caught gen001

The proposal's diff-based approach (compare full outputs directly, not just a length-thresholded "distinctive span") is structurally different from the current carryover facet's mechanism, and might genuinely have caught the gen001 leak where the current mechanism didn't. That's a real, plausible claim — but it's currently just a claim.

- [ ] Build a minimal prototype of the diff-based check only — not the full three-stage architecture — and run it against a reconstruction of gen001's exact scenario.
- [ ] Report whether it catches it. If it does, that's real evidence for the proposal's core mechanism, independent of whether the judge tier gets adopted. If it doesn't, say so plainly — that would mean the same class of blind spot can recur even under this design, which is important to know before investing further in it.
- [ ] Only after both 4a and 4b are resolved with actual evidence, decide whether to build the full cascade, a partial version, or none of it — and write down which, and why.

---

## Phase 5 — Final re-experiment

Only after Phases 1–4 are each checked with real evidence:

- [ ] Full test suite, pass count reported.
- [ ] Full campaign re-run, every changed cell explained individually — which phase caused it, and in which direction.
- [ ] A fresh real-LLM run if quota allows, specifically re-testing gen001's scenario to confirm Phase 1a's fix holds on a live model, not just the reconstruction.
- [ ] One consolidated report: what changed, what the current honest headline numbers are, and an updated claims list.