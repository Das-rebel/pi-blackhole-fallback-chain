# pi-blackhole-fallback-chain

A [pi](https://github.com/badlogic/pi-mono) skill that verifies and repairs the
**pi-blackhole** worker fallback chains (observer / reflector / dropper).

## Why

pi-blackhole resolves a model for each background worker through a chain:

```
stage model -> stage fallbacks -> base config model
```

Two things must hold:

1. the chain is configured with live, enabled models that have a usable context window, and
2. a candidate skipped by cooldown or size must **continue** to the next candidate.

Upstream builds violate (2): traversal stops at the first cooldown skip and the
observer dies with

```
no model available for observer (all candidates exhausted, sessionFallback disabled)
```

## Install

```bash
npm install -g pi-blackhole-fallback-chain
# or copy into ~/.pi/agent/skills/pi-blackhole-fallback-chain
```

## Use

```bash
python3 scripts/verify_fallback_chain.py
```

```
[FAIL] R1  runtime patch missing — a cooldown-skipped candidate will terminate traversal
[WARN] W1  observer[2] zai/glm-4.5-flash: no explicit contextWindow override
[INFO] W3  observer[3] ollama-direct/qwen3:0.6b-8k: local terminal fallback

VERDICT: BROKEN  (FAIL=1 WARN=1)
```

Exit code `0` = usable, `1` = broken, `2` = config/IO error.

## What it checks

- every stage chain is non-empty (C1)
- every member is a known provider/model pair (C2)
- no member is disabled (C3)
- every member has a context window (C4)
- explicit `contextWindow` overrides (W1), active cooldowns (W2), local tails (W3)
- the runtime skip → continue patch is present (R1/R2)

See `SKILL.md` for the configuration shape and repair steps, `REFERENCE.md` for
resolver internals, the debug event vocabulary, the cooldown format, and a
working end-to-end test procedure.

## Related

- `pi-blackhole-fallback-doctor` — deeper audit and the durable runtime patch
  (https://github.com/Das-rebel/pi-blackhole-fallback-doctor)

## License

MIT
