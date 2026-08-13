""".env.example must document reality.

This file drifted once: it documented ALPACA_API_KEY / ALPACA_SECRET_KEY while the code read
APCA_API_KEY_ID / APCA_API_SECRET_KEY, carried five keys nothing read, and omitted the real
switches — so "copy to .env and fill in" produced a file the app ignored. These tests pin the
example to the variables the code actually reads, in both directions: no documented key may be
dead, and no key the code reads may go undocumented.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
ENV_EXAMPLE = REPO / ".env.example"

#: Read via the SDK or a library rather than an os.getenv in this repo, so the source scan
#: below can't see them — kept documented on purpose.
READ_OUTSIDE_THIS_REPO = {"ANTHROPIC_API_KEY"}


def documented_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=.*", line.strip())
        if match:
            keys.add(match.group(1))
    return keys


def keys_the_code_reads() -> set[str]:
    # _require_env is alpaca.py's fail-at-startup wrapper around os.getenv.
    pattern = re.compile(
        r"""(?:os\.getenv|os\.environ\.get|_require_env)\(\s*["']([A-Z][A-Z0-9_]*)["']"""
    )
    keys: set[str] = set()
    for source_dir in ("app", "scripts", "evals"):
        for path in (REPO / source_dir).rglob("*.py"):
            keys.update(pattern.findall(path.read_text(encoding="utf-8")))
    return keys


def test_every_documented_key_is_actually_read() -> None:
    dead = documented_keys() - keys_the_code_reads() - READ_OUTSIDE_THIS_REPO
    assert not dead, f".env.example documents keys nothing reads: {sorted(dead)}"


def test_every_key_the_code_reads_is_documented() -> None:
    undocumented = keys_the_code_reads() - documented_keys()
    assert not undocumented, f".env.example is missing keys the code reads: {sorted(undocumented)}"


def test_the_old_wrong_names_stay_gone() -> None:
    """The specific historical bug: Alpaca's env vars are APCA_*, not ALPACA_*. A doc that
    resurrects the wrong names sends users back to a silently ignored .env."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "ALPACA_API_KEY" not in text
    assert "ALPACA_SECRET_KEY" not in text
    assert "ALPACA_BASE_URL" not in text
    assert "APCA_API_KEY_ID" in text
    assert "APCA_API_SECRET_KEY" in text
