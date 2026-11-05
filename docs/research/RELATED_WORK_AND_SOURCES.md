# Related work and sources: full inventory

Companion to `FINAL_COST_OPTIMIZATION_RESEARCH.md`. Grouped by discipline.

**Verification status**, given for every entry:

- **V** — the existence, authors and venue were confirmed by search or fetch during this
  research (05-11-2026), and the mechanism stated was read from the source or its
  abstract.
- **V-abs** — confirmed, but only the abstract or a secondary summary was readable. The
  mechanism should be re-checked against the full text before it is quoted in the paper.
- **K** — cited from background knowledge, not re-fetched in this session. Verify before
  citing.

Where only secondary write-ups were available, that is said.

---

## A. Intrusion recovery and selective re-execution: the closest classical prior art

| source | mechanism | what it demonstrates | limitations | relation to CausalLine | status |
|---|---|---|---|---|---|
| Kim, Wang, Zeldovich, Kaashoek. **Intrusion Recovery Using Selective Re-execution (RETRO)**. OSDI 2010, pp. 89–104. https://people.csail.mit.edu/nickolai/papers/kim-retro.pdf · https://www.usenix.org/conference/osdi10/intrusion-recovery-using-selective-re-execution · slides https://static.usenix.org/events/osdi10/tech/slides/kim.pdf | Action history graph recorded at runtime. Repair rolls back the attack action and re-executes dependents. **Predicates** skip an action whose inputs are unchanged after repair; **shepherded re-execution** halts once a re-executed process's state matches the original | Recovers real and synthetic Linux attacks while preserving legitimate actions | Syscall granularity; relies largely on deterministic re-execution | **The direct ancestor of IREC**: repair at entry, re-execute, cut off on equality. CausalLine's version must place equality at a projection because LLM calls are not deterministic | V-abs (PDF streams not machine-readable here; mechanism from slides and abstract) |
| Kim. **Automatic intrusion recovery with system-wide history**. MIT PhD thesis, 2014. https://dspace.mit.edu/handle/1721.1/91107 | Extended treatment of RETRO / Warp / Aire | — | — | Background | V-abs |
| Chandra, Kim, Shah, Narula, Zeldovich. **Intrusion Recovery for Database-backed Web Applications (WARP)**. SOSP 2011, pp. 101–114, doi:10.1145/2043556.2043567. https://dspace.mit.edu/handle/1721.1/73667 | Roll back affected DB state, replay later legitimate requests; retroactive patching; browser-level replay | MediaWiki: 24–27% throughput cost, 2–3.2 GB/day storage | Normal-run logging overhead is substantial | Shows the normal-run cost that selective recovery usually pays. IREC adds **no** logging beyond CausalLine's existing trace store | V-abs |
| Chandra, Kim, Zeldovich. **Asynchronous Intrusion Recovery for Interconnected Web Services (AIRE)**. SOSP 2013. https://dspace.mit.edu/handle/1721.1/91473 | Each service repairs locally and sends repair messages to the others; asynchronous, decentralised | 4 realistic attacks; 19–30% CPU, 6–9 KB/request | Services must cooperate | The multi-party analogue of multi-agent recovery; changed messages propagate like IREC's changed projections | V-abs |
| Goel, Po, Farhadi, Li, de Lara. **The Taser Intrusion Recovery System**. SOSP 2005. https://www.eecg.utoronto.ca/~ashvin/publications/sosp-2005.pdf | Taint processes from a detected intrusion, revert their FS changes, selectively replay legitimate operations | Effective on a range of intrusions and admin errors | Taint is dependency (exposure)-based, so it over-taints | Historical example of exposure-based recovery, which is exactly what CausalLine refines | V-abs |
| Brown, Patterson. **Undo for Operators: Building an Undoable E-mail Store**. USENIX ATC 2003. https://static.usenix.org/events/usenix03/tech/brown.html | Rewind, Repair, Replay over logged user requests | — | — | The "3R" framing of post-detection recovery | V-abs |
| Candea, Kawamoto, Fujiki, Friedman, Fox. **Microreboot: A Technique for Cheap Recovery**. OSDI 2004. https://arxiv.org/abs/cs/0406005 | Reboot fine-grained components instead of the whole system | Recovers most failures an order of magnitude faster | Assumes crash-only components | The analogue of agent-level restart (B1 / escalation scope) | V-abs |

## B. Incremental computation and build systems: early cutoff

| source | mechanism | demonstrates | limitations | relation | status |
|---|---|---|---|---|---|
| Mokhov, Mitchell, Peyton Jones. **Build Systems à la Carte**. ICFP 2018 (PACMPL 2, Art. 79); JFP vol. 30 (2020), *Theory and Practice*. https://www.microsoft.com/en-us/research/uploads/prod/2018/03/build-systems-a-la-carte.pdf · https://www.microsoft.com/en-us/research/uploads/prod/2020/04/build-systems-jfp.pdf | Formal taxonomy; **verifying traces** (hash the inputs, skip when they match) and **early cutoff** (an unchanged rebuilt output stops propagation) | Classifies Make, Shake, Bazel and others; minimality notions | Deterministic tasks | Formal model for IREC's splice and cutoff rule | V |
| **Salsa**: the red-green algorithm and backdating. https://git.joshthomas.dev/mirrors/salsa/raw/branch/gh-pages/reference/algorithm.html · source `function/backdate.rs` https://docs.rs/crate/salsa/0.27.0/source/src/function/backdate.rs · rust-analyzer, *Durable Incrementality* (2023) https://rust-analyzer.github.io/blog/2023/07/24/durable-incrementality.html | Recompute a query; if the value equals the old one, keep the old revision ("backdate"), so dependents stay green | Production use in rust-analyzer / rustc | Backdating of provisional (cyclic) values is flagged in source as a safety stop-gap | Industrial early cutoff. The stop-gap note parallels why IREC's equality must be exact | V |
| rustc dev guide, **Incremental compilation**. https://rust.googlesource.com/rust-lang/rustc-dev-guide/+show/refs/heads/main/src/queries/incremental-compilation.md | Red/green marking of the query graph | — | — | As above | V |
| Hammer, Khoo, Hicks, Foster. **Adapton: Composable, Demand-Driven Incremental Computation**. PLDI 2014, pp. 156–166. https://people.mpi-sws.org/~hammer/adapton-pldi2014.pdf | Demanded computation graph; demand-driven change propagation | Speedups over prior incremental systems | Deterministic programs | Demand-driven variant: re-execute only what a final observer needs | V |
| Acar. **Self-Adjusting Computation** (PhD thesis, CMU, 2005); Acar, Blelloch, Harper, *Adaptive Functional Programming*, POPL 2002 | Dynamic dependence graphs plus change propagation | Asymptotic update bounds via trace stability | Deterministic | Change propagation is IREC's propagation rule | K |
| McSherry, Murray, Isaacs, Isard. **Differential Dataflow**. CIDR 2013. https://www.microsoft.com/en-us/research/publication/differential-dataflow/ | Differences propagate; a zero difference stops; nested iteration (Naiad) | — | Structured data | A zero delta stops propagation: early cutoff on structured outputs | V |
| Budiu, Chajed, McSherry, Ryzhyk, Tannen. **DBSP: Automatic Incremental View Maintenance for Rich Query Languages**. PVLDB 16(7):1601–1614, 2023. https://www.vldb.org/pvldb/vol16/p1601-budiu.pdf | Algebra of streams; incrementalises SQL and Datalog | Implementation (Feldera) | Relational | Motivation for typed inter-agent messages: a delta is well-defined only on structure | V |

## C. Rollback recovery and durable execution

| source | mechanism | demonstrates | limitations | relation | status |
|---|---|---|---|---|---|
| Elnozahy, Alvisi, Wang, Johnson. **A Survey of Rollback-Recovery Protocols in Message-Passing Systems**. ACM CSUR 34(3):375–408, 2002, doi:10.1145/568522.568525. https://cs.cornell.edu/courses/cs717/2001fa/papers/alvisi-survey.pdf | Piecewise-deterministic (PWD) assumption; pessimistic / optimistic / causal logging; orphans; domino effect | — | Reproduces the *same* history | PWD ≈ IREC's input-completeness assumption A1; orphan = consumer of a changed message | V |
| Strom, Yemini. **Optimistic Recovery in Distributed Systems**. ACM TOCS 3(3), 1985 | Optimistic logging, dependency tracking | — | — | Origin of PWD | K (cited via the survey) |
| **Temporal** documentation: workflows, event history, determinism. https://docs.temporal.io/workflows | Re-run workflow code against the event history; activity results recorded and reused; non-determinism errors | Production durable execution | Requires deterministic workflow code | Recorded tool responses mean replay re-issues side-effecting calls only when their arguments change (IREC's tool-boundary cutoff) | V |
| Zaharia et al. **Resilient Distributed Datasets**. NSDI 2012, pp. 15–28. https://www.usenix.org/conference/nsdi12/technical-sessions/presentation/zaharia | Lineage-based recomputation of lost partitions | Spark | Coarse lineage = exposure | Lineage tells you what *could* be affected, not what was | V |
| Carbone et al. **State Management in Apache Flink**. PVLDB 10(12), 2017 | Asynchronous barrier snapshots | — | — | Checkpointing background | K |
| DBOS: Skiadopoulos et al., **DBOS: A DBMS-Oriented Operating System**, PVLDB 15(1), 2021 | Durable workflows stored in a DBMS | — | — | Durable-execution background | K |

## D. Information flow, program analysis, debugging

| source | mechanism | demonstrates | limitations | relation | status |
|---|---|---|---|---|---|
| Goguen, Meseguer. **Security Policies and Security Models**. IEEE S&P 1982 | Noninterference | — | — | Formal non-influence | K |
| Clarkson, Schneider. **Hyperproperties**. JCS 18(6), 2010; TR https://ecommons.cornell.edu/bitstream/1813/11660/4/hyperproperties-tr.pdf | Properties of sets of traces; noninterference is **2-safety**: refutation needs ≥ 2 traces | — | — | Basis of Theorem 1 (no single-run clearance) | V-abs (2-safety statement confirmed via secondary sources) |
| Devriese, Piessens. **Noninterference through Secure Multi-Execution**. IEEE S&P 2010, pp. 109–124. https://lirias.kuleuven.be/retrieve/107152 | Run the program once per security level, with high inputs replaced for the low run; **sound by construction** | SpiderMonkey implementation with benchmarks | Cost ≈ k executions | The recovered run *is* the "low" execution, which is the safety argument of Theorem 2 | V-abs |
| Terauchi, Aiken. **Secure Information Flow as a Safety Problem**. SAS 2005 | Self-composition | — | — | 2-trace reasoning | K |
| Schwartz, Avgerinos, Brumley. **All You Ever Wanted to Know About Dynamic Taint Analysis and Forward Symbolic Execution**. IEEE S&P 2010, pp. 317–331. https://edmcman.github.io/bib/schwartz_2010_dynamic-abstract.html | Formal taint semantics; over- and under-tainting | — | Data-dependence (exposure) based | Why taint ≠ influence | V-abs |
| Korel, Laski. **Dynamic Program Slicing**. IPL 1988; Agrawal, Horgan, PLDI 1990 | Dynamic data and control dependence slices | — | Dependence ≠ semantic influence | Rejected as the decision procedure (R8) | K |
| Zeller, Hildebrandt. **Simplifying and Isolating Failure-Inducing Input**. IEEE TSE 28(2), 2002 | Delta debugging: many executions to isolate causes | — | Many runs | Interventional cause-finding needs runs; consistent with Theorem 1 | K (search did not return it) |
| Johnson, Brun, Meliou. **Causal Testing**. ICSE 2020 | Minimal input perturbations that flip behaviour | — | — | Same | K |
| Wang et al. (ByteDance). **AgentArmor: Enforcing Program Analysis on Agent Runtime Trace to Defend Against Prompt Injection**. arXiv:2508.01249, 2025. https://arxiv.org/abs/2508.01249 | Agent traces as PDGs plus a type system | AgentDojo ASR ≈ 3% | Dependency-based | PDG over agent traces = exposure structure | V-abs |

## E. Causal inference

| source | mechanism | relation | status |
|---|---|---|---|
| Bareinboim, Correa, Ibeling, Icard. **On Pearl's Hierarchy and the Foundations of Causal Inference**. Ch. 27 in *Probabilistic and Causal Inference: The Works of Judea Pearl*, ACM 2022 (TR R-60). https://causalai.net/r60.pdf | Causal Hierarchy Theorem: interventional / counterfactual layers generically not identifiable from lower layers | Theorem 1's causal-inference reading | V-abs |
| Ibeling, Icard. **A Topological Perspective on Causal Inference**. arXiv:2107.08558 | Collapse is topologically meagre | Strengthens the above | V-abs |
| Halpern. **Actual Causality**. MIT Press, 2016; Halpern & Pearl, BJPS 2005 | Actual causation; but-for vs. preemption | Leave-one-out misses redundant causes (D-030); IREC's joint intervention on F sidesteps that for recovery | K |

## F. LLM attribution and model internals

| source | mechanism | demonstrates | limitations | relation | status |
|---|---|---|---|---|---|
| Cohen-Wang, Shah, Georgiev, Madry. **ContextCite: Attributing Model Generation to Context**. arXiv:2409.00729, 2024 | Random ablations + sparse linear surrogate | Context attribution as a formal task | Approximate; logprobs | Rejected as decision procedure (R4); NeurIPS venue **not confirmed** | V-abs |
| Liu, Kandpal, Raffel. **AttriBoT: A Bag of Tricks for Efficiently Approximating Leave-One-Out Context Attribution**. ICLR 2025. https://arxiv.org/abs/2411.15102 · code https://github.com/r-three/AttriBoT | Cached activations, hierarchical attribution, proxy models | >300× speedup vs naive LOO | Approximation; white-box | KV reuse could make each IREC re-execution cheaper on self-hosted models | V |
| Wang, Zou, Geng, Jia. **TracLLM: A Generic Framework for Attributing Long Context LLMs**. USENIX Security 2025. https://www.usenix.org/conference/usenixsecurity25/presentation/wang-yanting · code https://github.com/Wang-Yanting/TracLLM | Informed search over context segments; prompt-injection forensics | Post-attack traceback | Precision/recall < 1; hundreds of seconds per pair (per AttnTrace) | Forensics, not recovery; no FN bound | V |
| **AttnTrace: Attention-based Context Traceback for Long-Context LLMs**. arXiv:2508.03793, 2025 | Attention-based traceback | Faster than TracLLM | Attention is not causation | R3 | V-abs |
| Jain, Wallace. **Attention is not Explanation**. NAACL 2019 | Attention weights vs. feature importance | — | — | R3 | K |
| Meng, Bau, Andonian, Belinkov. **Locating and Editing Factual Associations in GPT (ROME / causal tracing)**. NeurIPS 2022 | Activation patching | — | White-box; per-intervention passes | R6 | K |
| Koh, Liang. **Understanding Black-box Predictions via Influence Functions**. ICML 2017; Pruthi et al. **TracIn**, NeurIPS 2020 | Training-data influence | — | Wrong object | R5 | K |
| Sundararajan, Taly, Yan. **Axiomatic Attribution (Integrated Gradients)**. ICML 2017 | Gradient attribution | — | Not causal for discrete context removal | R3 | K |
| Sclar, Choi, Tsvetkov, Suhr. **Quantifying Language Models' Sensitivity to Spurious Features in Prompt Design**. ICLR 2024. https://arxiv.org/abs/2310.11324 | FormatSpread | Up to 76 accuracy points from formatting changes | — | Why raw-output byte equality rarely survives a prompt edit, so the cutoff goes at the consumer's projection | V |
| Shi et al. **Large Language Models Can Be Easily Distracted by Irrelevant Context**. ICML 2023, PMLR 202:31210–31227. https://proceedings.mlr.press/v202/shi23a.html | GSM-IC | Irrelevant context changes answers | — | Exposure *can* become influence, so it must be tested, not assumed | V |

## G. Agent security, attack defence, failure attribution

| source | mechanism | relation | status |
|---|---|---|---|
| Zhu et al. **MELON: Provable Defense Against Indirect Prompt Injection Attacks in AI Agents**. ICML 2025. https://arxiv.org/abs/2502.05174 · code https://github.com/ucsb-mlsec/MELON | Masked re-execution (user prompt removed) + tool-call comparison to *detect* injection | Re-execution as a security primitive; detection, not recovery | V |
| Debenedetti et al. **Defeating Prompt Injections by Design (CaMeL)**. arXiv:2503.18813, 2025. https://arxiv.org/abs/2503.18813 · code https://github.com/google-research/camel-prompt-injection | Control/data-flow extraction, capabilities, dual LLM; 77% AgentDojo tasks with provable security (v2) | Prevention; R7 | V |
| Costa, Köpf, et al. **Securing AI Agents with Information-Flow Control (FIDES)**. arXiv:2505.23643, 2025. https://www.microsoft.com/en-us/research/publication/securing-ai-agents-with-information-flow-control/ · https://github.com/microsoft/fides | Confidentiality/integrity labels, deterministic policy enforcement | Prevention; labels propagate on read (exposure); R7 | V-abs |
| Zhong et al. **RTBAS: Defending LLM Agents Against Prompt Injection and Privacy Leakage**. arXiv:2502.08966, 2025. https://arxiv.org/abs/2502.08966 | IFC with LM-judge and attention-based dependency screeners | Heuristic dependency screening, no FN bound; R3 | V-abs |
| Debenedetti et al. **AgentDojo**. NeurIPS 2024 Datasets & Benchmarks | Prompt-injection benchmark | Benchmark used by MELON, CaMeL, FIDES, AgentArmor | K |
| Zhang et al. **Which Agent Causes Task Failures and When? (Who&When)**. arXiv:2505.00212, 2025 | Failure-attribution benchmark over 127 MAS | Attribution of failures, not recovery; ICML venue **not confirmed** | V-abs |
| Zhang et al. **AgenTracer: Who Is Inducing Failure in the LLM Agentic Systems?** ICLR 2026. https://arxiv.org/abs/2509.03312 | Counterfactual replay + fault injection to build training data; 8B tracer model | Counterfactual replay used offline for labels | V-abs |

## H. Agent recovery, 2026: the closest recent prior art

| source | mechanism | differences from IREC / CausalLine | status |
|---|---|---|---|
| Gao, Chen, Song, Zhang (UC San Diego). **Provenance All the Way Down: Selective Recovery for Multi-Agent LLM Workflows (agent-rewind)**. Technical report, 2026. https://github.com/Yitang233/agent-rewind (MIT license) | Record (path, Merkle hash) of every read; recovery reruns the workflow and reuses every step whose reads still verify; **early cutoff follows**. Greedy decoding; speculative decoding against the previous output (74/74 matches ≤ 512 tokens). W4 includes a TAMAS indirect-injection scenario. W2: 5.0 steps rerun vs 22.5 for restart | **Same execution mechanism.** Differences: the dirty set is the faulty *producer*, not a source-level intervention; it makes no exposure-vs-influence claim; no exact-ground-truth unsafe/recall evaluation; no comparison with attribution-based recovery or topology closure; no lower bound. Tracking overhead on real models is unmeasured by its own admission | V (README read) |
| Uyar. **Selective Revocation and Replay: Post-Compromise Recovery of Explicit Persisted State in Memory-Augmented LLM Agents**. 2026, doi:10.5281/zenodo.18967061. https://github.com/aliuyar1234/selective-revocation-replay | Provenance-based revocation of descendants of suspicious roots; replay of dirty writer events | Provenance (exposure)-based; single agent; 9 vs 17 extra LLM calls vs rollback | V (README read) |
| Qi, Wu, Liu, Chen, Liu. **REVISE: Validity-Guided Recovery for Online Revisions in Agent Workflows**. arXiv:2609.00643, 2026 | Intersect a revision delta with data/control dependencies; recompute the affected region | Dependency-based; revisions, not attacks; 40.6–56.0% fewer model calls than restart | V-abs |
| Xu, Fan, Wang, Li, Liu. **Policy-Backed Selective Regeneration under Tainted Inter-Agent Communication (ESC-CR)**. arXiv:2609.26072, 2026 | Executable semantic commitments at a release boundary; on violation, taint the message and regenerate in a clean room | Policy-based detection + regeneration; not influence-based preservation | V-abs |
| **AgentRewind: Recoverable Execution for Long-Horizon LLM Agents**. arXiv:2608.14380, 2026 | Checkpoint / rewind with "rewind memory" | Rollback, not selective influence-based recovery | V-abs (search snippet) |
| **RecoverAgent: Intent-Preserving Recovery from Indirect Prompt Injection in Tool-using LLM Agents**. Findings of EMNLP 2026. https://aclanthology.org/2026.findings-emnlp.830/ | Continue after an unsafe tool response | Forward recovery, not selective replay | V-abs (search snippet) |

## I. Inference determinism, serving, and cost

| source | mechanism | relation | status |
|---|---|---|---|
| He et al. (Thinking Machines Lab). **Defeating Nondeterminism in LLM Inference**. Blog, 10-09-2025. https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference · code `thinking-machines-lab/batch_invariant_ops` | Batch-invariant RMSNorm, matmul and attention; temperature 0 becomes reproducible (secondary reports: 1,000/1,000 identical completions; ~34–60% throughput cost) | Enabler C8: raises IREC's cutoff yield on self-hosted models | V-abs (numbers from secondary write-ups) |
| Ollama: seed + temperature 0 for reproducible output (third-party guides; official `docs/api.md` not retrieved). https://makandracards.com/makandra/626358-ollama-generate-reproducible-results | Fixed seed + T=0 | The local backend's determinism lever | V-abs (secondary only) |
| Yao, Jiang et al. **CacheBlend: Fast LLM Serving for RAG with Cached Knowledge Fusion**. EuroSys 2025, pp. 94–109. https://arxiv.org/abs/2405.16444 | Reuse non-prefix KV caches, recompute a token subset | Cheaper re-execution when a middle chunk (the removed source) changes; self-hosted only | V |
| Kwon et al. **vLLM / PagedAttention**. SOSP 2023; Zheng et al. **SGLang / RadixAttention**. 2024 | Prefix caching | Same; applies equally to B0/B2, so no change in ratios | K |
| Provider prompt-caching pricing (Anthropic cache read ≈ 0.1× base input; OpenAI and Gemini vary by model). Secondary summaries only, e.g. https://www.developersdigest.tech/blog/prompt-caching-across-ai-providers-2026 | Billing discount on repeated prefixes | Orthogonal billing lever; check official pricing pages before quoting | V-abs (secondary only, inconsistent) |
| Leviathan, Kalman, Matias. **Fast Inference from Transformers via Speculative Decoding**. ICML 2023 | Draft-and-verify decoding | Basis of C7: verify the old output as a draft against the new prompt | K |

## J. Other disciplines (sequential testing and diagnosis)

| source | relation | status |
|---|---|---|
| Wald. **Sequential Tests of Statistical Hypotheses**. Ann. Math. Stat. 1945 | Background to the repo's falsified SPRT line (not revived) | K |
| Pattipati, Alexandridis. **Application of Heuristic Search and Information Theory to Sequential Fault Diagnosis**. IEEE SMC 1990 | Test sequencing. IREC needs no test ordering, because each re-execution is both test and repair | K |
| Dorfman. **The Detection of Defective Members of Large Populations**. Ann. Math. Stat. 1943; Du & Hwang, *Combinatorial Group Testing* | Group testing. Recovery needs only the joint intervention on F, so per-source group testing is unnecessary (R10) | K |

## K. Repository-internal evidence used

| document / file | what was taken from it |
|---|---|
| `docs/14-workload-varied-validation.md`, `data/results/mixed56-workload-varied/*.json` | primary-campaign tokens, placements, `truth_events`, `events_with_token`, escalation notes |
| `docs/13-mixed56-validation.md`, `data/results/mixed56-validation/*.json` | fixed-workload control, same analysis |
| `docs/15-cost-direction-reopened.md` | lazy vs hybrid replication; `targeted_only` pair false negatives |
| `docs/gate1/final_cost_direction.md` | self-report = 80% of analysis; memoization, group testing and splicing exhausted; closure invariant |
| `docs/local_llm_frontier/03-token-economics.md` | A/N + f win condition; 84% of tokens are prompt |
| `docs/real_mixed_model_60_agent_experiment.md` §2–4 | mixed56 topology, providers, canary design |
| `src/recovery/replay.py`, `src/recovery/causalline.py`, `src/recovery/verify.py`, `src/provenance/estimator.py`, `src/eval/experiment.py`, `src/eval/scripted.py`, `src/tracing/mixed.py` | mechanisms reproduced, and the two accounting defects (`experiment.py:run_cell` tools; `causalline.py:406`) |
| `docs/research/prototype/` | this research's harness and outputs |
