"""
The local-LLaMA backend: a third client behind the same `generate()` contract.

    python -m src.common.local_llama --smoke

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
----------------------------------------------
It is the smallest integration that lets the existing pipeline run on a model
served by Ollama on this machine. It is **not** a second pipeline, a second set
of metrics, or a second definition of contamination. Everything downstream --
tracing, provenance, attribution, contamination, planning, replay, verification,
and every metric -- is the code the NVIDIA frontier already uses, called with a
different client.

    run_pipeline(client=NVIDIAClient(...))     existing frontier, untouched
    run_pipeline(client=LocalLlamaClient(...)) this one

`src/common/nvidia.py` is not imported, not modified and not called from here.
The two frontiers share the pipeline and share nothing else.

THE CONTRACT, AND THE THREE THINGS IT HAS TO GET RIGHT
-------------------------------------------------------
`generate(prompt, system, json_output, temperature) -> LLMResponse`, plus:

* **`model`**, so `run_pipeline()` writes the model that actually answered into
  the trace header instead of falling through to `load_settings()` and stamping
  every local trace `gemini-3.6-flash`. That is D-057, D-059 and D-075, three
  occurrences of one mistake, and this client is not going to be the fourth.
* **`fingerprint()`**, which wins over settings for the fields it owns (D-059).
* **honest token counts.** Ollama reports `prompt_eval_count` and `eval_count`;
  both are recorded, so the cost metrics mean the same thing here as on the
  hosted frontier. A backend that reported zero would make the local analysis
  look free, which is the most flattering possible lie.

WHAT IS MEASURED ABOUT THIS BACKEND BEFORE IT IS USED
------------------------------------------------------
`python -m src.eval.local_bench` — see `docs/local_llm_frontier/`. On the
machine this was written for the answer was uncomfortable and is recorded rather
than worked around: `llama3` (8B, Q4, 4.9 GB) does not fit a 6 GB laptop GPU
alongside a 4096-token context, so Ollama places it **100% on CPU** at about
4 tokens/second, and concurrency is flat — throughput identical at 1, 2 and 4
in flight while latency scales linearly. So the campaign runs at concurrency 1,
and the number of runs it can afford is a property of that, stated wherever a
number from it is quoted.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from src.common.llm import LLMError, LLMResponse

DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_MODEL = "llama3"
# Long, because a CPU-bound 8B model answering 400 tokens at ~4 tok/s takes
# minutes. A ceiling below the real answer time turns a slow backend into a
# failed one, which would be a measurement of this constant rather than of the
# model.
DEFAULT_TIMEOUT_S = 900.0
DEFAULT_MAX_ATTEMPTS = 3
# The pipeline's prompts are long and its answers are not. Capping output keeps
# a wall-clock budget predictable; `json_max_tokens` is smaller for the same
# reason NVIDIA's is (docs/09 section 7.3) -- every JSON answer asked for here
# is small, and a degenerate one should cost seconds rather than minutes.
DEFAULT_MAX_TOKENS = 512
DEFAULT_JSON_MAX_TOKENS = 384


class LocalLlamaUnavailable(LLMError):
    """The endpoint is not there. Never retried: waiting does not start Ollama."""


@dataclass
class LocalLlamaSettings:
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    temperature: float = 0.0
    max_output_tokens: int = DEFAULT_MAX_TOKENS
    json_max_tokens: int = DEFAULT_JSON_MAX_TOKENS
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    # Recorded in the trace header so a run can be tied to the exact runtime
    # that produced it. `seed` is passed to Ollama, which makes greedy decoding
    # reproducible *for a fixed model and runtime* -- not across versions, and
    # the report says so rather than claiming byte-identical replay (Phase 6).
    seed: int | None = 0
    num_ctx: int = 4096

    def fingerprint(self) -> dict[str, Any]:
        return {
            "provider": "ollama-local",
            "model": self.model,
            "model_handle": "local-llama",
            "endpoint": self.endpoint,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "num_ctx": self.num_ctx,
            "seed": self.seed,
        }


@dataclass
class LocalStats:
    """What one client spent, in the shape `real_llm._attach_api_stats` reads.

    `rate_limited` and `key_rotations` are always zero here and are present on
    purpose: the shared harness reads them off whatever client it was handed,
    and a local backend that simply lacked the attributes crashed the run
    *after* the pipeline had finished -- twelve minutes of real inference
    thrown away by an `AttributeError` at the reporting step.

    Carrying two structural zeros is the right fix rather than making the
    harness defensive, because the harness is shared with the NVIDIA frontier
    and the brief forbids changing it. There is no key pool here and no rate
    limit to hit, so zero is not a placeholder -- it is the measurement.
    """

    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0
    transient_errors: int = 0
    malformed_json: int = 0
    total_latency_s: float = 0.0
    # Always 0: a local endpoint has no quota and no keys to rotate.
    rate_limited: int = 0
    key_rotations: int = 0
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"{self.calls} call(s), {self.prompt_tokens + self.output_tokens} "
            f"token(s), {self.retries} retry(ies), "
            f"{self.transient_errors} transient error(s), "
            f"{self.malformed_json} malformed JSON, "
            f"{self.total_latency_s:.0f}s total"
        )


@dataclass
class LocalLlamaClient:
    """Drop-in for `GeminiClient` / `NVIDIAClient`, served by Ollama.

    Sequential by construction: `local_bench` measured this backend as fully
    serialised, so there is no key pool, no rate limiter and no concurrency
    here. Adding them would be machinery for a problem this machine does not
    have.
    """

    settings: LocalLlamaSettings = field(default_factory=LocalLlamaSettings)
    stats: LocalStats = field(default_factory=LocalStats)
    # Read by `real_llm._attach_api_stats`. Always 0.0: nothing throttles a
    # local endpoint, it just takes as long as it takes.
    throttled_s: float = 0.0

    @property
    def model(self) -> str:
        """D-075: the identity the trace header asks for."""
        return self.settings.model

    @property
    def total_tokens(self) -> int:
        """Every token this client has spent.

        `run_pipeline` reads this off the client at the end of a run to record
        what the workflow cost. Its absence is not a small omission: the
        pipeline had already completed twelve real inference calls when it
        raised `AttributeError` here, so the whole run was discarded at the
        last line and reported as VOID with no cause. Two tests were lost that
        way before the reason was printed.
        """
        return self.stats.prompt_tokens + self.stats.output_tokens

    def fingerprint(self) -> dict[str, Any]:
        """D-059: a fingerprint on the client wins over one on its settings."""
        return self.settings.fingerprint()

    # --- the contract ------------------------------------------------------

    def generate(
        self,
        prompt: str,
        system: str | None = None,
        json_output: bool = False,
        temperature: float | None = None,
    ) -> LLMResponse:
        options: dict[str, Any] = {
            "temperature": (
                self.settings.temperature if temperature is None else temperature
            ),
            "num_predict": (
                self.settings.json_max_tokens if json_output
                else self.settings.max_output_tokens
            ),
            "num_ctx": self.settings.num_ctx,
        }
        if self.settings.seed is not None:
            options["seed"] = self.settings.seed

        payload: dict[str, Any] = {
            "model": self.settings.model,
            "prompt": prompt,
            "stream": False,
            "options": options,
        }
        if system:
            payload["system"] = system
        if json_output:
            payload["format"] = "json"

        started = time.time()
        attempts = 0
        last: Exception | None = None
        while attempts < self.settings.max_attempts:
            attempts += 1
            try:
                body = self._post(payload)
            except LocalLlamaUnavailable:
                raise
            except LLMError as exc:
                # A transient backend error. Retried on the same endpoint --
                # there is nowhere else to go, and the failure mode here is a
                # busy machine rather than a bad request.
                last = exc
                self.stats.transient_errors += 1
                if attempts < self.settings.max_attempts:
                    self.stats.retries += 1
                    time.sleep(min(5.0 * attempts, 15.0))
                    continue
                raise

            text = (body.get("response") or "").strip()
            if not text:
                last = LLMError("empty answer from the local model")
                self.stats.transient_errors += 1
                if attempts < self.settings.max_attempts:
                    self.stats.retries += 1
                    continue
                raise last

            if json_output and not _parses_as_json(text):
                # Same treatment as D-056 gives a degenerate hosted answer: a
                # transient generation failure, retried inside the client, and
                # the rejected attempt's tokens are still charged because they
                # were still spent.
                self.stats.malformed_json += 1
                self.stats.prompt_tokens += int(body.get("prompt_eval_count") or 0)
                self.stats.output_tokens += int(body.get("eval_count") or 0)
                last = LLMError(f"expected JSON, got: {text[:200]}")
                if attempts < self.settings.max_attempts:
                    self.stats.retries += 1
                    continue
                raise last

            prompt_tokens = int(body.get("prompt_eval_count") or 0)
            output_tokens = int(body.get("eval_count") or 0)
            latency = time.time() - started
            self.stats.calls += 1
            self.stats.prompt_tokens += prompt_tokens
            self.stats.output_tokens += output_tokens
            self.stats.total_latency_s += latency
            return LLMResponse(
                text=text,
                model=self.settings.model,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                thoughts_tokens=0,
                total_tokens=prompt_tokens + output_tokens,
                attempts=attempts,
                latency_s=latency,
                slept_s=0.0,
                finish_reason=("length" if body.get("done_reason") == "length"
                               else "stop"),
            )
        raise last or LLMError("local model produced nothing")

    # --- transport ---------------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.settings.endpoint}/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.timeout_s
            ) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            if exc.code == 404:
                raise LocalLlamaUnavailable(
                    f"model {self.settings.model!r} is not pulled: {detail}. "
                    f"Run `ollama pull {self.settings.model}`."
                ) from exc
            raise LLMError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            if "refused" in reason.lower() or "actively refused" in reason.lower():
                raise LocalLlamaUnavailable(
                    f"nothing is listening on {self.settings.endpoint}. "
                    "Start Ollama, then re-run."
                ) from exc
            raise LLMError(f"network error: {reason}") from exc
        except TimeoutError as exc:
            raise LLMError(
                f"no answer in {self.settings.timeout_s:.0f}s"
            ) from exc


def _parses_as_json(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        json.loads(stripped)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def available(settings: LocalLlamaSettings | None = None) -> tuple[bool, str]:
    """Is the endpoint there and is the model pulled? Cheap, and offline-safe."""
    settings = settings or LocalLlamaSettings()
    try:
        with urllib.request.urlopen(
            f"{settings.endpoint}/api/tags", timeout=10
        ) as response:
            names = {
                m.get("name", "") for m in json.loads(response.read()).get("models", [])
            }
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if any(n == settings.model or n.startswith(settings.model + ":") for n in names):
        return True, f"{settings.model} available"
    return False, f"{settings.model} not pulled; have {sorted(names)}"



def placement(settings: LocalLlamaSettings | None = None) -> tuple[bool, str]:
    """Is the model actually on the GPU? (on_gpu, human-readable detail)

    WHY THIS IS A PREFLIGHT AND NOT A FOOTNOTE
    -------------------------------------------
    The first local campaign ran entirely on CPU and nobody noticed until the
    throughput was explained after the fact. Ollama had *not* failed and had not
    said anything at request time -- its startup log carried one line,
    `failure during GPU discovery ... failed to finish discovery before
    timeout`, and it then served every request from `library=cpu` at roughly a
    fifth of the speed the machine can do.

    That is the expensive kind of silent fallback: nothing errors, the numbers
    are real, and the campaign simply takes five times longer than it needed
    to. So placement is now checked *before* a campaign spends anything, and
    the caller is told loudly enough to act on it.

    The check asks the running server (`/api/ps`) rather than `nvidia-smi`,
    because the question is "where is this model loaded", not "does a GPU
    exist" -- the machine this was written on answers yes to the second and no
    to the first.

    Returns `(False, ...)` when the model is not loaded at all, because an
    unloaded model cannot be confirmed to be on the GPU. Load it with one
    cheap call first; `warn_if_cpu()` does exactly that.
    """
    settings = settings or LocalLlamaSettings()
    try:
        with urllib.request.urlopen(
            f"{settings.endpoint}/api/ps", timeout=10
        ) as response:
            running = json.loads(response.read()).get("models", [])
    except Exception as exc:  # noqa: BLE001
        return False, f"could not ask the server where the model is: {exc}"

    for entry in running:
        name = entry.get("name") or entry.get("model") or ""
        if not (name == settings.model or name.startswith(settings.model + ":")):
            continue
        total = int(entry.get("size") or 0)
        on_gpu = int(entry.get("size_vram") or 0)
        if total <= 0:
            return False, f"{name}: loaded, but the server reported no size"
        share = on_gpu / total
        detail = (
            f"{name}: {share:.0%} on GPU "
            f"({on_gpu / 1e9:.1f} GB of {total / 1e9:.1f} GB in VRAM)"
        )
        return share > 0.5, detail
    return False, f"{settings.model} is not currently loaded"


def warn_if_cpu(settings: LocalLlamaSettings | None = None) -> bool:
    """Load the model, check placement, and say so. Returns True if on GPU.

    Called by `local_bench` and `local_campaign` before either spends anything.
    It does not refuse to run -- a CPU result is still a real result and the
    project has no business discarding measurements -- but an unlabelled CPU
    campaign is the thing to prevent, so the label is printed where it cannot
    be missed and is carried into the results file.
    """
    settings = settings or LocalLlamaSettings()
    try:
        LocalLlamaClient(settings=settings).generate("hi")
    except Exception as exc:  # noqa: BLE001
        print(f"  placement: could not load the model to check: {exc}")
        return False
    on_gpu, detail = placement(settings)
    if on_gpu:
        print(f"  placement: OK -- {detail}")
        return True
    print(f"  placement: *** RUNNING ON CPU *** -- {detail}")
    print("  Ollama falls back to CPU silently when GPU discovery times out at")
    print("  startup; the server log line is `failure during GPU discovery`.")
    print("  Restart Ollama on an unloaded machine and re-check before")
    print("  committing to a long campaign -- it was ~5x slower on this box.")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="local LLaMA backend")
    parser.add_argument("--smoke", action="store_true", help="one live call")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    settings = LocalLlamaSettings(model=args.model)
    ok, detail = available(settings)
    print(f"endpoint {settings.endpoint}: {detail}")
    print(f"fingerprint: {json.dumps(settings.fingerprint(), indent=2)}")
    if not ok:
        return 1
    if not args.smoke:
        return 0

    client = LocalLlamaClient(settings=settings)
    response = client.generate("Reply with exactly: ok")
    print(f"\ntext    {response.text[:80]!r}")
    print(f"tokens  prompt={response.prompt_tokens} output={response.output_tokens}")
    print(f"call    attempts={response.attempts} latency={response.latency_s:.1f}s")
    print(f"stats   {client.stats.line()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
