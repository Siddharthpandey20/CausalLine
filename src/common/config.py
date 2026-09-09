"""
Settings, read from .env.

No dependency on python-dotenv: the parser below is fifteen lines and the
ground rules say ask before adding a dependency (D-013).

Precedence: real environment variables win over .env, so a shell export can
override a committed default without editing files. .env is gitignored; see
.env.example for the keys.
"""

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_PATH = ".env"

# THE model name. Change it here and nowhere else: every other module reads
# Settings.model, and the value lands in each trace header via fingerprint().
# Overridable per machine with GEMINI_MODEL in .env.
# Flash tier, per docs/01-scope.md (Gemini) and the budget note in D-004.
# 26-08-2026: was gemini-2.5-flash, which now 404s for new API keys.
DEFAULT_MODEL = "gemini-3.6-flash"


# Accepted by gemini-3.6-flash. "minimal" produced 0 thought tokens on every
# prompt we tried, trivial and realistic alike; it is this model's replacement
# for the thinkingBudget=0 that 2.5 Flash accepted. See D-015.
THINKING_LEVELS: frozenset[str] = frozenset({"minimal", "low", "high"})


class MissingAPIKey(RuntimeError):
    """Raised with instructions rather than a bare KeyError, because this is
    the first thing that goes wrong on a new machine."""


@dataclass(frozen=True)
class Settings:
    """Everything the LLM client needs. Frozen: a run must not change model
    or temperature halfway through, or the trace stops being reproducible."""

    api_key: str
    model: str = DEFAULT_MODEL
    temperature: float = 0.0
    # Flash-tier models think by default. Thinking tokens are billed and are a
    # second source of run-to-run variation, so the testbed asks for as little
    # as the model allows and records the setting. gemini-3.6-flash rejects the
    # thinkingBudget=0 that 2.5 Flash accepted; "minimal" is the way to get
    # zero thought tokens on this model. See D-015.
    thinking_level: str = "minimal"
    max_output_tokens: int = 2048
    # Generous on purpose. Under a 20-requests-per-day quota (D-017) a
    # retry costs a request and waiting costs nothing, so the timeout is
    # set well past any plausible generation time. See D-020.
    timeout_s: float = 120.0
    max_attempts: int = 5
    # Free-tier Gemini Flash allows 20 generate_content requests per minute.
    # We pace below it rather than discovering the ceiling with a 429, because
    # a 429 retry spends another request against the same quota. Raise this
    # the day someone puts a card on the account.
    requests_per_minute: int = 15

    def min_call_interval_s(self) -> float:
        """Seconds to leave between calls. 0 disables pacing."""
        if self.requests_per_minute <= 0:
            return 0.0
        return 60.0 / self.requests_per_minute

    def fingerprint(self) -> dict[str, object]:
        """Goes in the trace header. docs/04 run hygiene: log model, version,
        temperature, date. Never includes the api key."""
        return {
            "model": self.model,
            "temperature": self.temperature,
            "thinking_level": self.thinking_level,
            "max_output_tokens": self.max_output_tokens,
            "requests_per_minute": self.requests_per_minute,
        }


def read_env_file(path: str | Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Parse KEY=VALUE lines. Ignores blanks, # comments, and a leading
    'export '. Strips one layer of matching quotes. Missing file -> {}."""
    path = Path(path)
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def normalise_model(name: str) -> str:
    """Drop a leading "models/".

    Gemini's own 404 message names the replacement as "models/gemini-3.6-flash",
    and the endpoint URL already ends in /models -- pasting it verbatim gives
    you a second 404 that looks identical to the first.
    """
    return name.strip().removeprefix("models/")


def load_settings(path: str | Path = DEFAULT_ENV_PATH, **overrides: object) -> Settings:
    """Build Settings from .env plus the real environment.

    Keyword overrides win over both, for tests and for the counterfactual
    runner, which needs the same model at a different temperature.
    """
    values = read_env_file(path)
    values.update({k: v for k, v in os.environ.items() if k.startswith("GEMINI_")})

    api_key = str(overrides.pop("api_key", "") or values.get("GEMINI_API_KEY", ""))
    if not api_key:
        raise MissingAPIKey(
            "GEMINI_API_KEY not found. Copy .env.example to .env and put your "
            "key in it, or export GEMINI_API_KEY. The key is never committed: "
            ".env is gitignored."
        )

    # NOTE: the defaults below are stated a second time, next to the Settings
    # fields. Keep the two in step -- changing only the dataclass default has
    # no effect on a Settings built through this function, which is all of them.
    def get(name: str, cast, default):
        if name in overrides:
            return cast(overrides.pop(name))
        raw = values.get("GEMINI_" + name.upper())
        return cast(raw) if raw not in (None, "") else default

    settings = Settings(
        api_key=api_key,
        model=normalise_model(get("model", str, DEFAULT_MODEL)),
        temperature=get("temperature", float, 0.0),
        thinking_level=get("thinking_level", str, "minimal"),
        max_output_tokens=get("max_output_tokens", int, 2048),
        timeout_s=get("timeout_s", float, 120.0),
        max_attempts=get("max_attempts", int, 5),
        requests_per_minute=get("requests_per_minute", int, 15),
    )
    if overrides:
        raise TypeError(f"unknown settings: {sorted(overrides)}")
    if settings.thinking_level not in THINKING_LEVELS:
        # Catch it here rather than as an opaque 400 from the API. The server
        # is the authority; this list is what we have verified.
        raise ValueError(
            f"GEMINI_THINKING_LEVEL {settings.thinking_level!r} not one of "
            f"{sorted(THINKING_LEVELS)}"
        )
    return settings
