"""
Record and replay LLM calls.

At 20 free-tier requests a day (D-017), spending quota so that three people
can each look at a real trace is not affordable. Record one live run, commit
the cassette, and everyone develops against real model output for free.

    python -m src.tracing.pipeline data/runs/run1.jsonl --record data/cassettes/run1.jsonl
    python -m src.tracing.pipeline data/runs/run2.jsonl --replay data/cassettes/run1.jsonl

Replay is honest, not fake: the text and the token counts are the ones the
model actually returned. What did *not* happen is the request. So:

  * `latency_s` and `attempts` are replayed as recorded and mean nothing about
    this run
  * a replayed trace is marked `"cassette": "replay"` in its header, and no
    number from a replayed run may go in the paper as a fresh measurement

That marking is the whole reason this module writes to the trace header
rather than staying invisible. See D-019.

Keying: a call is identified by everything that could change the answer --
model, temperature, thinking level, system instruction, prompt, JSON mode.
Identical calls made more than once (counterfactual replay repeats each
comparison three times) are replayed in the order they were recorded, and
once the recording is exhausted the last response repeats, since temperature
is 0 and the model is being asked the same question again.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.common.config import Settings
from src.common.llm import GeminiClient, LLMResponse

MODES = ("record", "replay", "auto")


class CassetteMiss(RuntimeError):
    """Replay was asked for a call the cassette does not contain.

    Usually means the prompt changed since recording. The message names the
    prompt so the difference is findable, because a silent fallback to a live
    call is how a "free" development loop quietly spends the day's quota.
    """


def call_key(
    settings: Settings,
    prompt: str,
    system: str | None,
    json_output: bool,
    temperature: float | None,
) -> str:
    """Stable hash of everything that can change the response."""
    material = json.dumps(
        {
            "model": settings.model,
            "temperature": settings.temperature if temperature is None else temperature,
            "thinking_level": settings.thinking_level,
            "max_output_tokens": settings.max_output_tokens,
            "system": system or "",
            "prompt": prompt,
            "json_output": json_output,
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


@dataclass
class Cassette:
    """A file of recorded responses, indexed by call key."""

    path: Path
    entries: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _served: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Cassette":
        path = Path(path)
        cassette = cls(path=path)
        if not path.exists():
            return cassette
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            cassette.entries.setdefault(record["key"], []).append(record)
        return cassette

    def __len__(self) -> int:
        return sum(len(v) for v in self.entries.values())

    def append(self, key: str, prompt: str, system: str | None, response: LLMResponse) -> None:
        record = {
            "key": key,
            "prompt": prompt,
            "system": system,
            "model": response.model,
            "text": response.text,
            "prompt_tokens": response.prompt_tokens,
            "output_tokens": response.output_tokens,
            "thoughts_tokens": response.thoughts_tokens,
            "total_tokens": response.total_tokens,
            "finish_reason": response.finish_reason,
            "latency_s": response.latency_s,
            "attempts": response.attempts,
        }
        self.entries.setdefault(key, []).append(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def take(self, key: str) -> dict[str, Any] | None:
        """Next recorded response for this key, or None if there is none.

        Repeats of the same call are served in recorded order. When they run
        out the last one repeats rather than raising: at temperature 0 the
        same question asked again is the same question.
        """
        recorded = self.entries.get(key)
        if not recorded:
            return None
        index = self._served.get(key, 0)
        self._served[key] = index + 1
        return recorded[min(index, len(recorded) - 1)]


class CassetteClient:
    """Drop-in for GeminiClient, backed by a cassette.

    mode="record"  call the real client, save every response
    mode="replay"  never call the API; a miss raises CassetteMiss
    mode="auto"    replay what is recorded, record what is not
    """

    def __init__(
        self,
        cassette: Cassette,
        settings: Settings,
        inner: GeminiClient | None = None,
        mode: str = "replay",
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        if mode in ("record", "auto") and inner is None:
            raise ValueError(f"mode={mode!r} needs a live client to record with")
        self.cassette = cassette
        self.settings = settings
        self.inner = inner
        self.mode = mode
        self.calls = 0
        self.total_tokens = 0
        self.replayed = 0
        self.recorded = 0

    @property
    def throttled_s(self) -> float:
        return self.inner.throttled_s if self.inner else 0.0

    def generate(
        self,
        prompt: str,
        system: str | None = None,
        json_output: bool = False,
        temperature: float | None = None,
    ) -> LLMResponse:
        key = call_key(self.settings, prompt, system, json_output, temperature)

        if self.mode != "record":
            recorded = self.cassette.take(key)
            if recorded is not None:
                self.calls += 1
                self.replayed += 1
                self.total_tokens += int(recorded["total_tokens"])
                return _to_response(recorded)
            if self.mode == "replay":
                raise CassetteMiss(
                    f"no recording for this call in {self.cassette.path}.\n"
                    f"  key    {key}\n"
                    f"  system {(system or '')[:60]!r}\n"
                    f"  prompt {prompt[:120]!r}\n"
                    "The prompt has probably changed since the cassette was "
                    "recorded. Re-record, or run with --record."
                )

        assert self.inner is not None  # guarded in __init__
        response = self.inner.generate(
            prompt, system=system, json_output=json_output, temperature=temperature
        )
        self.cassette.append(key, prompt, system, response)
        self.calls += 1
        self.recorded += 1
        self.total_tokens += response.total_tokens
        return response

    def summary(self) -> dict[str, Any]:
        return {
            "cassette": str(self.cassette.path),
            "mode": self.mode,
            "replayed": self.replayed,
            "recorded": self.recorded,
        }


def _to_response(record: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        text=record["text"],
        model=record["model"],
        prompt_tokens=int(record["prompt_tokens"]),
        output_tokens=int(record["output_tokens"]),
        thoughts_tokens=int(record.get("thoughts_tokens", 0)),
        total_tokens=int(record["total_tokens"]),
        # Recorded, not measured now. Never report these from a replayed run.
        attempts=int(record.get("attempts", 1)),
        latency_s=float(record.get("latency_s", 0.0)),
        slept_s=0.0,
        finish_reason=record.get("finish_reason"),
    )


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "data/cassettes/run1.jsonl"
    cassette = Cassette.load(target)
    print(f"{target}: {len(cassette)} recorded calls, {len(cassette.entries)} distinct")
    for key, records in cassette.entries.items():
        first = records[0]
        preview = " ".join(first["prompt"].split())[:70]
        print(f"  {key}  x{len(records)}  {first['total_tokens']:>5} tok  {preview}...")
