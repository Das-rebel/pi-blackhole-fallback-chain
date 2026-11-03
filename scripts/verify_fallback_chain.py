#!/usr/bin/env python3
"""
verify_fallback_chain.py — end-to-end verifier for the pi-blackhole worker
fallback chains (observer / reflector / dropper).

Checks, in order of severity:

  FAIL  C1  worker chain is empty or missing
  FAIL  C2  chain member is not a known provider/model pair
  FAIL  C3  chain member model is disabled in models.json
  FAIL  C4  chain member has no usable context window (would be size-skipped)
  WARN  W1  chain member has no explicit contextWindow override
  WARN  W2  chain member is currently in a provider cooldown
  FAIL  R1  runtime patch (skip -> continue) is missing from the installed
             pi-blackhole runtime  -> the chain will stop at the first
             cooldown skip instead of traversing to the next candidate
  INFO  terminal fallback is a local model (good: survives provider outages)

Exit codes:  0 = chain usable   1 = chain broken   2 = usage/IO error
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

AGENT = Path(os.environ.get("PI_AGENT_DIR", Path.home() / ".pi/agent"))
CONFIG = AGENT / "pi-blackhole" / "pi-blackhole-config.json"
COOLDOWN = AGENT / "pi-blackhole" / "pi-blackhole-cooldown.json"
MODELS = AGENT / "models.json"
RUNTIME_CANDIDATES = [
    AGENT / "npm/node_modules/pi-blackhole/src/om/runtime.ts",
    AGENT / "npm/node_modules/pi-blackhole/dist/index.js",
]

STAGES = ("observer", "reflector", "dropper")
STAGE_MODEL_KEY = {"observer": "observerModel", "reflector": "reflectorModel", "dropper": "dropperModel"}
STAGE_FALLBACK_KEY = {
    "observer": "observerFallbackModels",
    "reflector": "reflectorFallbackModels",
    "dropper": "dropperFallbackModels",
}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, code: str, msg: str) -> None:
        self.rows.append((level, code, msg))

    def count(self, level: str) -> int:
        return sum(1 for lvl, _, _ in self.rows if lvl == level)


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def norm(entry: dict) -> tuple[str, str]:
    """Return (provider, model-id) for a blackhole model entry."""
    provider = entry.get("provider") or entry.get("id") and entry.get("provider") or ""
    model = entry.get("model") or entry.get("id") or ""
    if not provider:
        # entries may embed "provider/model"
        raw = str(entry.get("id") or "")
        if "/" in raw:
            provider, model = raw.split("/", 1)
    return str(provider).strip().lower(), str(model).strip().lower()


def model_index(models: dict) -> dict[tuple[str, str], dict]:
    """Index every model pi can resolve.

    A provider declares models in two places:
      * ``models``       — a list of full model definitions
      * ``modelOverrides`` — a dict keyed by model id, used for models that are
        not in the list (e.g. extra OpenRouter free models)
    Both must be indexed, otherwise chain members that only exist as an
    override are wrongly reported as unknown.
    """
    idx: dict[tuple[str, str], dict] = {}
    for pname, pdef in (models.get("providers") or {}).items():
        if not isinstance(pdef, dict):
            continue
        pl = pname.lower()
        for m in pdef.get("models") or []:
            if isinstance(m, dict) and m.get("id"):
                idx[(pl, str(m["id"]).lower())] = m
        overrides = pdef.get("modelOverrides")
        if isinstance(overrides, dict):
            for mid, val in overrides.items():
                entry = dict(val) if isinstance(val, dict) else {}
                entry.setdefault("id", mid)
                idx.setdefault((pl, str(mid).lower()), entry)
    return idx


def check_runtime(rep: Report) -> None:
    """The chain only traverses if cooldown-skipped candidates 'continue'."""
    found = False
    for path in RUNTIME_CANDIDATES:
        if not path.exists():
            continue
        found = True
        text = path.read_text(encoding="utf-8", errors="ignore")
        patched = ("cooldownStage" in text) and (
            "continue" in text[text.find("cooldown_skip") - 1500 : text.find("cooldown_skip") + 1500]
            if "cooldown_skip" in text
            else False
        )
        if "cooldownStage" in text and patched:
            rep.add("INFO", "R2", f"runtime patch present in {path.name}")
        else:
            rep.add(
                "FAIL",
                "R1",
                f"runtime patch missing in {path} — a cooldown-skipped candidate will "
                "terminate traversal instead of continuing to the next fallback",
            )
        break
    if not found:
        rep.add("WARN", "R3", "pi-blackhole runtime not found — skipped runtime patch check")


def main() -> int:
    rep = Report()
    for path, label in ((CONFIG, "config"), (MODELS, "models.json")):
        if not path.exists():
            print(f"ERROR: missing {label}: {path}", file=sys.stderr)
            return 2

    try:
        config = load_json(CONFIG)
        models = load_json(MODELS)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot parse config: {exc}", file=sys.stderr)
        return 2

    cooldown = {}
    if COOLDOWN.exists():
        try:
            cooldown = load_json(COOLDOWN)
        except Exception:  # noqa: BLE001
            cooldown = {}
    cd = {}
    for k, v in (cooldown or {}).items():
        if isinstance(v, dict):
            cd[(k.split("/")[0].lower(), k.split("/")[-1].lower())] = v.get("until", "")

    idx = model_index(models)

    for stage in STAGES:
        chain: list[dict] = []
        primary = config.get(STAGE_MODEL_KEY[stage])
        if isinstance(primary, dict):
            chain.append(primary)
        elif isinstance(primary, str):
            chain.append({"provider": "", "model": primary})
        for fb in config.get(STAGE_FALLBACK_KEY[stage]) or []:
            if isinstance(fb, dict):
                chain.append(fb)

        if not chain:
            rep.add("FAIL", "C1", f"{stage}: fallback chain is empty")
            continue

        for i, entry in enumerate(chain):
            provider, model = norm(entry)
            label = f"{stage}[{i}] {provider}/{model}"
            if not provider or not model:
                rep.add("WARN", "C2", f"{label}: incomplete provider/model entry")
                continue
            known = idx.get((provider, model))
            if known is None:
                rep.add("FAIL", "C2", f"{label}: not resolvable from models.json providers")
                continue
            if known.get("disabled"):
                rep.add("FAIL", "C3", f"{label}: model is disabled in models.json")
            cw = entry.get("contextWindow") or known.get("contextWindow")
            if not cw:
                rep.add("FAIL", "C4", f"{label}: no context window — will be size-skipped")
            elif not entry.get("contextWindow"):
                rep.add("WARN", "W1", f"{label}: no explicit contextWindow override")
            if (provider, model) in cd:
                rep.add("WARN", "W2", f"{label}: currently in cooldown until {cd[(provider, model)]}")
            if provider.startswith("ollama"):
                rep.add("INFO", "W3", f"{label}: local terminal fallback")

    check_runtime(rep)

    fails = rep.count("FAIL")
    warns = rep.count("WARN")
    for level, code, msg in rep.rows:
        print(f"[{level:4}] {code:3} {msg}")
    verdict = "BROKEN" if fails else ("USABLE with warnings" if warns else "USABLE")
    print(f"\nVERDICT: {verdict}  (FAIL={fails} WARN={warns})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
