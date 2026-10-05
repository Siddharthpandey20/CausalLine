# Minor Project — Mid-Term Evaluation Report

LaTeX source for the mid-term evaluation report on **CausalLine**, an
influence-aware selective recovery system for multi-agent LLM pipelines.

This is a **college minor-project report**, not the conference submission. A
separate publication-oriented draft lives in `../paper/`; it is organised around
a different argument and should not be confused with this document.

## Building

No custom document class, no BibTeX pass, no `algorithm` package. One command,
twice (the second pass resolves cross-references):

```
pdflatex main
pdflatex main
```

Packages used — all ship with TeX Live scheme-basic, MiKTeX basic and Overleaf:

| Package | Why |
|---|---|
| `fontenc`, `inputenc` | encoding |
| `graphicx` | the six figures |
| `amsmath`, `amssymb` | the problem formulation |
| `booktabs`, `array` | the literature and results tables |

The IEEE-like appearance (two columns, Roman section numbers, centred
small-caps headings) is produced with LaTeX kernel commands in the preamble
rather than by a class file, so there is nothing to install.

> **Not compiled here.** No TeX distribution was available on the machine where
> this was written, so the document was validated structurally — balanced
> environments, matched braces, every `\ref` resolving to a `\label`, every
> `\cite` resolving to a `\bibitem`, and every `\includegraphics` path
> existing — but never run through LaTeX. Please build it once before
> submitting.

## Layout

```
minor_report/
├── main.tex                     the report
├── references.bib               the same 15 references, for later BibTeX use
├── README.md
├── figures/                     six vector PDFs, embedded in the report
│   └── _png/                    raster previews, not used by the report
└── diagrams/
    ├── make_figures.py          regenerates every figure
    ├── introspect_topology.py   derives the agent graph from a real trace
    ├── topology.json            that graph, as data
    └── results.json             campaign results, as data
```

`references.bib` is **not** read by `main.tex` — the bibliography is inline so
the document builds in one pass. The `.bib` is kept in sync (an audit checks
the two agree) for anyone moving the report onto a BibTeX-based template.

## Regenerating the figures

```
python diagrams/make_figures.py
```

Reads `diagrams/topology.json` and `diagrams/results.json` and writes
`figures/*.pdf`. To rebuild the topology data from a fresh execution:

```
python diagrams/introspect_topology.py diagrams/topology.json
```

That runs the 56-agent pipeline with a deterministic echo client — no API
calls, no cost — and recovers the agent graph from the resulting trace, so the
architecture figures show recorded edges rather than edges read off the source.

## Where every number comes from

No figure in the report is typed by hand. Each is reproducible from the parent
repository:

| Report item | Source |
|---|---|
| Table II (workflow: 56 agents, 145 events) | `diagrams/topology.json` |
| Fig. 2, Fig. 3 (agent graphs) | `diagrams/topology.json` |
| Table IV, Fig. 4–6 (results) | `python -m src.eval.mixed_validation_report --dir data/results/mixed56-workload-varied` |
| Table V (safety) | same |
| Table VI (cost) | same |
| Scripted matrix (96 cells × 30 reps) | `data/results/campaign.json` |
| Test count (493 passing) | `python -m unittest discover -s tests` |
| Line count (~36k) | `find src -name "*.py" \| xargs wc -l` |

The main campaign itself:

```
python -m src.eval.mixed_validation --seeds 5 --vary-placement
```

## Claims deliberately not made

Worth knowing before editing, because these are considered omissions:

- **No confusion matrix, no training/validation curves, no accuracy.** The
  system is not a supervised classifier; those plots would have to be invented.
- **No statistical significance test.** Five workloads per regime supports
  descriptive statistics and a win/tie/loss count, nothing more.
- **No claim that the method is cheaper.** It costs 1.8×–2.5× a full restart in
  tokens. Section VI-F says so and gives the condition under which it is still
  the right choice.
- **No claim to the exposure/influence distinction or to counterfactual
  attribution.** Both are prior work and are cited as such.
- **The figure 31.7%** must not appear anywhere: it was an earlier large-regime
  result produced by a provenance defect, and Section VI-H is the account of it.
