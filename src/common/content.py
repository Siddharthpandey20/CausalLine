"""
The content store D-010 deferred.

`Event.inputs_ref` and `Event.output_ref` were opaque strings pointing at
nothing, because nothing in weeks 1-2 read event content: the graphs are built
from metadata and counterfactual replay reads `Source.content`, which is
stored inline (D-009). Selective replay is the thing that changes that, and it
needs three separate pieces of event content that the trace did not keep:

    the prompt that produced an event      to re-issue it with one source
                                          redacted, and to substitute a
                                          recomputed upstream output into it
    the output an event produced           to splice a non-invalidated event
                                          back in without re-invoking the model
    tool and memory state                  to detect a live object still
                                          pointing at something invalidated

Refs stay opaque to everything outside this module, which is what D-009 asked
for. They are content-addressed -- `ref = "c" + sha256(text)[:16]` -- and that
is load-bearing rather than tidy:

  * identical text stored twice is one record, so the store does not grow with
    the number of times a source is re-sent per question (D-016 makes that
    number large)
  * "did this spliced event's output change" is a 16-character comparison
    rather than a string diff, which is the assertion Phase 5 of the recovery
    work turns on. Two refs are equal if and only if the bytes are equal.

Storage split, extending D-018. A run is a directory of three files, not two:

    data/runs/run1.jsonl              events, sources, influence, checks, usage
    data/runs/run1.checkpoints.jsonl  agent state and memory snapshots
    data/runs/run1.content.jsonl      prompts, outputs, tool state

The third file is separate for D-018's reason exactly: it is the part that
scales with model verbosity rather than with event count, so folding it into
the trace would make every tool that reads a trace pay to parse text it does
not want, and would make the storage-overhead measurement (open issue #8)
impossible to split.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

CONTENT_SUFFIX = ".content.jsonl"

# What a stored blob is. Not a controlled vocabulary in the models.py sense --
# this is our own record type and nothing outside recovery switches on it --
# but it is worth naming so the store can be read by a human.
CONTENT_KINDS: frozenset[str] = frozenset(
    {
        "prompt",       # the full text sent to the model, sources included
        "source_block", # just the rendered source list inside that prompt, so
                        # a redaction never has to infer where it ends
        "system",       # the system instruction that accompanied it
        "output",       # what the model or a tool produced
        "tool_args",    # arguments a tool was called with
        "tool_state",   # a tool's observable state after the call
        "memory",       # a memory key/value pair, as JSON
    }
)


def content_path_for(trace_path: str | Path) -> Path:
    """data/runs/run1.jsonl -> data/runs/run1.content.jsonl"""
    path = Path(trace_path)
    return path.with_suffix("").with_name(path.stem + CONTENT_SUFFIX)


def ref_for(text: str) -> str:
    """The ref a given piece of text will always have.

    Callable without a store, which is what lets a replay check "is this the
    same output as before" without opening the sidecar at all.
    """
    return "c" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class ContentRecord:
    ref: str
    kind: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "text": self.text,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContentRecord":
        return cls(
            ref=data["ref"],
            kind=data["kind"],
            text=data["text"],
            meta=dict(data.get("meta") or {}),
        )


class MissingContent(KeyError):
    """A ref that resolves to nothing.

    Raised rather than returning None on purpose. A replay that silently
    treated absent content as an empty prompt would re-invoke the model with
    no context and log the answer as though it were a recomputation of the
    original event.
    """


class ContentStore:
    """Append-only content-addressed store for one run.

    Writing:

        with ContentStore(content_path_for(trace)) as store:
            ref = store.put("...", kind="prompt")

    Reading:

        store = ContentStore.load(content_path_for(trace))
        store.get(ref)
    """

    def __init__(self, path: str | Path, append: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.records: dict[str, ContentRecord] = {}
        self._fh = self.path.open("a" if append else "w", encoding="utf-8")

    # --- writing ------------------------------------------------------------

    def put(
        self, text: str, kind: str = "output", meta: dict[str, Any] | None = None
    ) -> str:
        """Store text, return its ref. Storing the same text twice is free and
        returns the same ref."""
        if not isinstance(text, str):
            raise TypeError(f"content must be a string, got {type(text).__name__}")
        if kind not in CONTENT_KINDS:
            raise ValueError(f"content kind {kind!r} not one of {sorted(CONTENT_KINDS)}")
        ref = ref_for(text)
        if ref in self.records:
            # First writer's kind and meta win. The bytes are what the ref
            # promises; the labels are commentary.
            return ref
        record = ContentRecord(ref=ref, kind=kind, text=text, meta=dict(meta or {}))
        self.records[ref] = record
        self._fh.write(json.dumps({"record": "content", **record.to_dict()}) + "\n")
        self._fh.flush()
        return ref

    def put_json(
        self, value: Any, kind: str = "output", meta: dict[str, Any] | None = None
    ) -> str:
        """Store a JSON-serialisable value. `sort_keys` so that equal values
        get equal refs regardless of construction order."""
        return self.put(json.dumps(value, sort_keys=True), kind=kind, meta=meta)

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "ContentStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- reading -------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "ContentStore":
        """Read-only view of a store on disk. Does not truncate the file."""
        store = cls.__new__(cls)
        store.path = Path(path)
        store.records = {}
        store._fh = _ClosedHandle()  # type: ignore[assignment]
        if store.path.exists():
            for line_no, line in enumerate(
                store.path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_no}: bad JSON ({exc})") from exc
                record = ContentRecord.from_dict(data)
                store.records[record.ref] = record
        return store

    def get(self, ref: str) -> str:
        record = self.records.get(ref)
        if record is None:
            raise MissingContent(
                f"{ref} is not in {self.path}. A trace whose refs do not "
                "resolve cannot be replayed; check the content sidecar was "
                "written beside the trace."
            )
        return record.text

    def get_json(self, ref: str) -> Any:
        return json.loads(self.get(ref))

    def record(self, ref: str) -> ContentRecord:
        record = self.records.get(ref)
        if record is None:
            raise MissingContent(f"{ref} is not in {self.path}")
        return record

    def has(self, ref: str) -> bool:
        return ref in self.records

    def refs_of_kind(self, kind: str) -> list[str]:
        return [r for r, rec in self.records.items() if rec.kind == kind]

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[ContentRecord]:
        return iter(self.records.values())

    def total_bytes(self) -> int:
        """Bytes of stored text, excluding refs and labels. The number the
        storage-overhead split wants, not the file size."""
        return sum(len(r.text.encode("utf-8")) for r in self.records.values())


class _ClosedHandle:
    """Stand-in file handle for a read-only store. Writing raises rather than
    silently appending to a file someone else is reading."""

    closed = True

    def write(self, _: str) -> None:
        raise ValueError("this ContentStore was opened read-only")

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass
