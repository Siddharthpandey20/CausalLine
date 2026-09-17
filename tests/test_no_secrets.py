"""
No credential reaches anywhere it must not.

A test rather than a checklist item, because a checklist is run once and a test
is run on every commit. It reads the real `.env` when one exists and asserts
that none of its values appears in source, tests, docs, committed
configuration, generated artefacts, run traces, results, or git history.

On a machine with no `.env` the value-scan has nothing to look for and is
skipped; the shape-scan and the gitignore assertions still run, and those are
the ones that catch a key pasted into a file by hand.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The real prefix NVIDIA issues. A repository-wide grep for it is one of the
# cheapest credential checks available, and it only stays useful while nothing
# in the repository is shaped like one -- which is why the client tests use
# "TEST-NOT-A-REAL-KEY-..." fixtures rather than realistic ones.
KEY_SHAPES = (
    re.compile(r"nvapi-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIzaSy[A-Za-z0-9_\-]{30,}"),   # Google API key form
)

# Files that legitimately discuss the prefix without containing a key.
SHAPE_EXEMPT = {
    "tests/test_no_secrets.py",
    "tests/test_nvidia_client.py",
    "tests/test_real_llm_eval.py",
    "docs/09-real-llm-evaluation.md",
}


def secrets() -> list[str]:
    """Values from a real .env, if there is one. Never printed."""
    from src.common.config import read_env_file

    values = read_env_file(ROOT / ".env")
    return [
        v
        for k, v in values.items()
        if ("KEY" in k.upper() or "TOKEN" in k.upper() or "SECRET" in k.upper())
        and len(v) >= 12
        and not v.startswith("your-")
    ]


def scanned_files() -> list[Path]:
    globs = (
        "src/**/*.py",
        "tests/**/*.py",
        "docs/*.md",
        "*.md",
        "*.toml",
        "*.txt",
        "*.cfg",
        ".env.example",
        ".github/**/*",
        "data/generated/**/*",
        "data/results/**/*",
        "data/samples/**/*",
    )
    out: list[Path] = []
    for pattern in globs:
        out.extend(p for p in ROOT.glob(pattern) if p.is_file())
    return out


def test_no_configured_secret_appears_in_any_repository_file():
    values = secrets()
    if not values:
        pytest.skip("no .env on this machine; nothing to look for")
    for path in scanned_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for value in values:
            # The assertion message must not carry the value it is about.
            assert value not in text, (
                f"a configured secret appears in {path.relative_to(ROOT)}"
            )


def test_nothing_in_the_repository_is_shaped_like_an_api_key():
    """Catches a key pasted in by hand, on a machine with no .env to compare
    against."""
    offenders: list[str] = []
    for path in scanned_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in SHAPE_EXEMPT:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for shape in KEY_SHAPES:
            if shape.search(text):
                offenders.append(rel)
    assert not offenders, f"key-shaped strings in {sorted(set(offenders))}"


def test_env_is_gitignored_and_untracked():
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env"], cwd=ROOT, capture_output=True
    )
    assert ignored.returncode == 0, ".env is not covered by .gitignore"

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=ROOT,
        capture_output=True,
    )
    assert tracked.returncode != 0, ".env is tracked by git"


def test_generated_artifact_directories_are_gitignored():
    """A generated suite carries no credential -- another test asserts that --
    but it is run output, and run output does not belong in the repository."""
    for path in ("data/generated/x.jsonl", "data/runs/real/x.jsonl"):
        result = subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=ROOT, capture_output=True
        )
        assert result.returncode == 0, f"{path} is not gitignored"


def test_no_secret_is_in_git_history():
    values = secrets()
    if not values:
        pytest.skip("no .env on this machine; nothing to look for")
    log = subprocess.run(
        ["git", "log", "-p", "--all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        errors="ignore",
    ).stdout
    for value in values:
        assert value not in log, "a configured secret appears in git history"


def test_the_nvidia_settings_fingerprint_cannot_carry_a_key():
    """The fingerprint goes into every trace header. It is built field by
    field rather than from asdict(), so there is no code path from it to
    `api_keys` -- this asserts that stays true."""
    import json

    from src.common.nvidia import NVIDIASettings

    keys = ("TEST-NOT-A-REAL-KEY-0000000001", "TEST-NOT-A-REAL-KEY-0000000002")
    printed = json.dumps(NVIDIASettings(api_keys=keys).fingerprint())
    for key in keys:
        assert key not in printed
    assert "api_keys_configured" in printed


def test_no_settings_repr_can_carry_a_key():
    """THE HOLE THIS CLOSES ACTUALLY LEAKED, on 17-09-2026.

    `test_the_nvidia_settings_fingerprint_cannot_carry_a_key` above guards the
    path into a trace header, and that path was fine. The one nobody guarded
    was the default dataclass `repr`: `print(settings)`, a REPL echo, an
    f-string in a log line, or an exception whose args include the settings
    object printed the entire key pool verbatim -- into a terminal transcript,
    while checking the rate limiter.

    The class docstring said it held "everything the client needs, minus
    anything it must not print", which was true of every field except the one
    that mattered. Both settings classes now mark the credential
    `field(repr=False)`.
    """
    from src.common.config import Settings
    from src.common.nvidia import NVIDIASettings

    keys = ("TEST-NOT-A-REAL-KEY-0000000001", "TEST-NOT-A-REAL-KEY-0000000002")
    shown = repr(NVIDIASettings(api_keys=keys))
    for key in keys:
        assert key not in shown
    # Still useful for debugging: the non-secret fields are all there.
    assert "model=" in shown

    single = "TEST-NOT-A-REAL-KEY-0000000003"
    shown = repr(Settings(api_key=single))
    assert single not in shown
    assert "model=" in shown


def test_no_settings_repr_carries_the_real_configured_key():
    """The same assertion against whatever is actually in `.env`."""
    values = secrets()
    if not values:
        pytest.skip("no .env on this machine")
    from src.common.config import load_settings
    from src.common.nvidia import load_nvidia_settings

    shown = repr(load_settings()) + repr(load_nvidia_settings())
    for value in values:
        assert value not in shown
