"""
The four testbed tools: web, database, memory, code.

Web and database read from fixtures in `src/tracing/fixtures/`, not from the
live internet (D-014). Two reasons, both about the paper rather than about
convenience: ground truth is known by construction only if we author what the
tools return, and a run that hits the real web cannot be replayed, which
makes counterfactual replay meaningless.

Memory is a per-run JSON file. Code execution is a subprocess.
"""

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"
CODE_TIMEOUT_S = 15


@dataclass
class WebPage:
    url: str
    title: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "title": self.title, "content": self.content}


@dataclass
class Tools:
    """The tool surface one run sees.

    `web_pages` is passed in rather than read at call time so that src/eval/
    can hand in a poisoned corpus without touching this module.
    """

    web_pages: list[WebPage] = field(default_factory=list)
    db: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, str] = field(default_factory=dict)
    memory_path: Path | None = None

    @classmethod
    def from_fixtures(cls, memory_path: str | Path | None = None) -> "Tools":
        corpus = json.loads((FIXTURES / "web_corpus.json").read_text(encoding="utf-8"))
        db = json.loads((FIXTURES / "db.json").read_text(encoding="utf-8"))
        memory = json.loads((FIXTURES / "memory.json").read_text(encoding="utf-8"))
        return cls(
            web_pages=[WebPage(**p) for p in corpus["pages"]],
            db=db,
            memory=dict(memory),
            memory_path=Path(memory_path) if memory_path else None,
        )

    # --- web ---------------------------------------------------------------

    def web_search(self, query: str, limit: int = 4) -> list[WebPage]:
        """Keyword overlap against the fixture corpus. Deterministic: the same
        query returns the same pages in the same order, every run."""
        terms = {t.lower() for t in query.split() if len(t) > 2}

        def score(page: WebPage) -> tuple[int, str]:
            text = f"{page.title} {page.content}".lower()
            return (-sum(t in text for t in terms), page.url)

        ranked = sorted(self.web_pages, key=score)
        return ranked[:limit]

    # --- database ------------------------------------------------------------

    def db_lookup(self, key: str) -> Any:
        """Exact-key lookup. Returns None for a miss, which the agent sees as
        an empty result rather than an exception."""
        return self.db.get(key)

    # --- memory ---------------------------------------------------------------

    def memory_read(self, key: str) -> str | None:
        return self.memory.get(key)

    def memory_write(self, key: str, value: str) -> None:
        """Writes are kept in memory and mirrored to disk when memory_path is
        set. Recovery has to be able to roll one of these back
        (docs/02-architecture.md), so a write is always paired with a
        memory_write event carrying the key."""
        self.memory[key] = value
        if self.memory_path:
            self.memory_path.parent.mkdir(parents=True, exist_ok=True)
            self.memory_path.write_text(
                json.dumps(self.memory, indent=2), encoding="utf-8"
            )

    def memory_rollback(self, key: str, previous: str | None) -> None:
        """Undo one write. Called by src/recovery/ when the event that made
        the write turns out to be contaminated."""
        if previous is None:
            self.memory.pop(key, None)
        else:
            self.memory[key] = previous
        if self.memory_path:
            self.memory_path.write_text(
                json.dumps(self.memory, indent=2), encoding="utf-8"
            )

    # --- code -----------------------------------------------------------------

    def run_python(self, code: str, stdin: str = "") -> dict[str, Any]:
        """Run generated code in a subprocess and report what happened.

        NOT a sandbox. Tool sandboxing is explicitly out of scope
        (docs/01-scope.md), and this runs model-written code on the machine
        that invokes it. It is fenced only by a timeout and a scratch cwd.
        Do not point this at a corpus you have not read.
        """
        with tempfile.TemporaryDirectory(prefix="agent-recovery-") as workdir:
            script = Path(workdir) / "generated.py"
            script.write_text(code, encoding="utf-8")
            try:
                proc = subprocess.run(
                    [sys.executable, str(script)],
                    input=stdin,
                    capture_output=True,
                    text=True,
                    timeout=CODE_TIMEOUT_S,
                    cwd=workdir,
                )
            except subprocess.TimeoutExpired:
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": f"timed out after {CODE_TIMEOUT_S}s",
                    "returncode": None,
                }
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
            "returncode": proc.returncode,
        }
