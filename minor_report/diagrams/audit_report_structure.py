"""Audit the minor-project report: structure, word limits, references, numbers."""
import re
from collections import Counter
from pathlib import Path

TEX = Path("minor_report/main.tex")
BIB = Path("minor_report/references.bib")
t = TEX.read_text(encoding="utf-8")
bib = BIB.read_text(encoding="utf-8")


def words(block: str) -> int:
    b = re.sub(r"%.*", "", block)
    b = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?(\{[^}]*\})?", " ", b)
    b = re.sub(r"[{}$\\~^_]", " ", b)
    return len([w for w in b.split() if any(c.isalnum() for c in w)])


def between(start, end):
    i = t.index(start) + len(start)
    return t[i:t.index(end, i)]


print("=== WORD LIMITS ===")
checks = [
    ("Abstract", between(r"\begin{abstract}", r"\end{abstract}"), 250),
    ("Impact statement",
     between(r"\section*{Impact Statement}", "%====" + "=="), 150),
    ("Problem definition",
     between(r"\label{sec:prob}", r"\section{Proposed Framework}"), 250),
]
for name, text, limit in checks:
    n = words(text)
    print(f"  {name:<20} {n:>4} / {limit}   {'OK' if n <= limit else 'OVER'}")

print("\n=== REQUIRED SECTIONS ===")
required = [
    (r"\title{", "Title"), (r"\author{", "Authors"),
    (r"\begin{abstract}", "Abstract"),
    (r"\section*{Impact Statement}", "Impact Statement"),
    ("Index Terms", "Index Terms"),
    (r"\section{Introduction}", "I. Introduction"),
    (r"\section{Motivation}", "II. Motivation"),
    (r"\subsection{Contributions}", "II.A Contributions"),
    (r"\section{Literature Work}", "III. Literature Work"),
    (r"\label{tab:lit}", "Literature table"),
    (r"\subsection{Research gap}", "Research gap"),
    (r"\section{Problem Definition}", "IV. Problem Definition"),
    (r"\section{Proposed Framework}", "V. Proposed Framework"),
    (r"\section{Experimental Result and Discussion}", "VI. Experiments"),
    (r"\section{Conclusion and Future Directions}", "VII. Conclusion"),
    (r"\appendix", "Appendix"),
    (r"\section*{Acknowledgment}", "Acknowledgment"),
    (r"\begin{thebibliography}", "References"),
]
for marker, label in required:
    print(f"  [{'x' if marker in t else ' '}] {label}")

print("\n=== KEYWORDS ===")
kw = between("Index Terms}}---", r"\vspace")
terms = [k.strip().rstrip(".") for k in kw.replace("\n", " ").split(",")
         if k.strip()]
print(f"  count {len(terms)} (need 5-6) | alphabetical: "
      f"{terms == sorted(terms, key=str.lower)}")
for k in terms:
    print(f"    - {k}")

print("\n=== FIGURES / TABLES / ALGORITHMS ===")
figs = re.findall(r"includegraphics\[[^\]]*\]\{([^}]*)\}", t)
print(f"  figures included: {len(figs)}")
for f in figs:
    print(f"    [{'x' if (Path('minor_report') / f).exists() else ' '}] {f}")
labels = set(re.findall(r"\\label\{([^}]*)\}", t))
refs = set()
for g in re.findall(r"\\(?:eq)?ref\{([^}]*)\}", t):
    refs.update(x.strip() for x in g.split(","))
print(f"  tables: {len(re.findall(r'begin.table', t))}")
print(f"  algorithms: {t.count('begin{algo}')}")
print(f"  refs to missing labels: {sorted(refs - labels) or 'none'}")
unref = {x for x in labels if x.split(':')[0] in ('fig', 'tab', 'alg', 'eq')}
print(f"  labels never referenced: {sorted(unref - refs) or 'none'}")

print("\n=== REFERENCES ===")
keys = set(re.findall(r"\\bibitem\{([^}]+)\}", t))
bibkeys = set(re.findall(r"@\w+\{([^,]+),", bib))
cited = set()
for g in re.findall(r"\\cite\{([^}]*)\}", t):
    cited.update(x.strip() for x in g.split(","))
print(f"  bibitem entries: {len(keys)} | cited: {len(cited)}")
print(f"  references.bib entries: {len(bibkeys)} | in sync: {bibkeys == keys}")
print(f"  cited but undefined: {sorted(cited - keys) or 'none'}")
print(f"  defined but uncited: {sorted(keys - cited) or 'none'}")
print(f"  DOI fields present: {bib.count('doi =') + t.count('doi =')} "
      f"(0 expected, none were verifiable)")

print("\n=== LATEX STRUCTURE ===")
o = Counter(re.findall(r"\\begin\{([A-Za-z]+\*?)\}", t))
c = Counter(re.findall(r"\\end\{([A-Za-z]+\*?)\}", t))
bad = {k: (o[k], c[k]) for k in set(o) | set(c) if o[k] != c[k]}
print(f"  unbalanced environments: {bad or 'none'}")
print(f"  braces: {t.count('{')} vs {t.count('}')} -> "
      f"{'balanced' if t.count('{') == t.count('}') else 'MISMATCH'}")
d = len(re.findall(r"(?<!\\)\$", t))
print(f"  unescaped $: {d} -> {'even' if d % 2 == 0 else 'ODD'}")
bare = [i for i, ln in enumerate(t.splitlines(), 1)
        if re.search(r"(?<!\\)%", ln) and not ln.lstrip().startswith("%")]
print(f"  bare % outside comments: {bare or 'none'}")
pkgs = re.findall(r"\\usepackage(?:\[[^\]]*\])?\{([^}]*)\}", t)
print(f"  packages required: {pkgs}")
print(f"  document class: "
      f"{re.search(r'documentclass(\[[^]]*\])?\{([^}]*)\}', t).group(2)}")
print(f"  body words: {words(t)}")
