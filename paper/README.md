# The paper

`causalline.tex` — *Exposure Is Not Influence: Selective Recovery After Prompt
Injection in Multi-Agent LLM Systems*.

## Building

No custom class file, no bibliography tool, no figures to generate. Any
standard TeX distribution will do:

```
cd paper
pdflatex causalline && pdflatex causalline
```

Twice, because of the cross-references. Packages used are all in a base TeX
Live or MiKTeX install: `fontenc`, `inputenc`, `geometry`, `amsmath`,
`amssymb`, `booktabs`, `graphicx`, `xcolor`, `array`, `hyperref`. It also
builds unchanged on Overleaf.

There is no `.bib`: the seven references are a `thebibliography` block, so
there is no BibTeX pass and nothing to go stale.

## Where every number comes from

No figure in the paper was typed by hand. Each is reproducible from the
repository:

| paper table | source |
|---|---|
| Tab. 1 (exposure vs influence) | `docs/13-mixed56-validation.md` §7 |
| Tab. 3 (preserved work) | `python -m src.eval.mixed_validation_report` |
| Tab. 4 (safety) | same, §4 |
| Tab. 5 (scripted matrix) | `data/results/campaign.json`, oracle cells |
| Tab. 6 (cost) | `python -m src.eval.mixed_validation_report`, §5 |
| §7.1 (Gate 1 falsification) | `docs/gate1/`, D-092 |
| §7.2 (the safety defect) | `docs/12-issue20-correction.md` |

Regenerating the validation tables:

```
python -m src.eval.mixed_validation_report --dir data/results/mixed56-validation
```

## What the paper deliberately does not claim

Worth knowing before editing it, because these are load-bearing omissions and
not oversights:

- **No significance test.** Five seeds per regime, and the seeds vary model
  sampling rather than the workload (Limitation 1). A *p*-value over that
  would be arithmetic, not evidence.
- **No claim that the method is cheaper.** It is not. §6 states the token cost
  against every baseline including full restart, and gives the condition under
  which it is nonetheless the right choice.
- **The figure 31.7%** — an earlier large-regime result — must never appear.
  It was five unsafe preservations wearing a performance gain, and §7.2 is the
  account of it.
