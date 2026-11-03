# pi-blackhole-fallback-chain — reference

## How blackhole resolves a worker model

For each stage (observer / reflector / dropper) the candidate list is built as:

1. the stage's primary model (`observerModel`, …)
2. the stage's fallback list (`observerFallbackModels`, …)
3. the base config model — *unless* `sessionFallback` is enabled, in which case
   the session's own model is also tried

`MAX_STAGE_ATTEMPTS = 10` bounds the number of attempts per stage.

### Skip reasons that must NOT end traversal

| Reason | Source | Correct behaviour |
|--------|--------|-------------------|
| `cooldown_skip` | provider recently failed; `until` timestamp | try the next candidate |
| `sizeSkipped` | candidate context window < estimated input | try the next candidate |
| `output allowance` | estimated output > candidate allowance | try the next candidate |

A candidate skipped for any of these reasons must `continue` the loop. Ending
the loop produces:

```
observer.model_unavailable
  reason: no model available for observer (all candidates exhausted, sessionFallback disabled)
```

and the observer is skipped entirely — no `observer.records`, no `cursor.saved`,
so nothing is ever consolidated.

## Size-skip arithmetic

Effective context window precedence:

1. `contextWindow` override on the blackhole model entry
2. the pi model registry value for that provider/id
3. a 128K fallback

Blackhole skips a candidate when the estimated input is larger than that
window. Because ollama silently head-truncates rather than erroring, declaring a
`contextWindow` far above the model's real `num_ctx` is safe and preferred.

## Debug event vocabulary

| Event | Meaning |
|-------|---------|
| `observer.start` | observer began on a chunk; logs `chunkTokens` / accumulated |
| `model.cooldown_skip` | candidate skipped; logs `until`, `reason`, `cooldownStage` |
| `observer.model_unavailable` | traversal exhausted — the failure mode |
| `observer.empty` | model replied but did not call the observation tool (`tool_not_called`) |
| `observer.records` | observation records produced (`count`) |
| `observer.appended` | records appended to the memory file |
| `cursor.saved` | observer/reflector/dropper cursor persisted (`state: recorded`) |
| `dropper.agent_start` / `dropper.result` | dropper ran; result e.g. `not_over_target` |

## Cooldown file

`~/.pi/agent/pi-blackhole/pi-blackhole-cooldown.json`

```jsonc
{
  "openrouter/nvidia/nemotron-3.5-super-vl:free": {
    "until": "2026-11-03T19:39:11.000Z",
    "reason": "429",
    "stage": "observer"   // informational only
  }
}
```

Keys are `provider/model`, lowercased. To force the chain straight to its local
tail, pre-seed cooldowns for every remote member — useful for end-to-end tests.

## End-to-end test procedure

Headless `pi -p` **cannot** exercise the observer: the child inherits
`PI_BLACKHOLE_ROOT_SESSIONS` and is classified as a subagent, and the
`agent_end` observer is aborted on disposal with no shutdown-observe path.

Working method:

1. `tmux new-session -d -s bhtest`
2. inside it, strip the PI environment so the session is `session:main`:
   ```bash
   env -u PI_BLACKHOLE_ROOT_SESSIONS -u PI_SESSION_ID -u PI_SESSION_FILE \
       -u PI_MODEL -u PI_PROVIDER -u PI_CODING_AGENT -u PI_REASONING_LEVEL pi
   ```
3. paste a large prompt with `tmux load-buffer` / `paste-buffer`
   (`send-keys -l` fails above ~80 KB)
4. submit the multiline editor with **Alt+Enter** (plain Enter inserts a newline)
5. watch `~/.pi/agent/pi-blackhole/debug.ndjson` for `observer.records` and
   `cursor.saved`
6. `tmux kill-session -t bhtest`

## Known provider pitfalls

- `groq` gpt-oss models exit 0 with **zero bytes** in pi text mode (output stays
  in the reasoning channel) — require the literal text `PONG`, not exit 0.
- `mistral` models fail with 422 on the `store` field (API schema drift).
- `zai-2` models are subject to **evening time-based throttling**: responses
  truncate to a 1-token signature with `stopReason: length`. It self-recovers
  after the window; no context-window change is warranted.
- The **local** ollama tail is the only member immune to provider outages.

## Files

- `~/.pi/agent/pi-blackhole/pi-blackhole-config.json` — chains, timeouts
- `~/.pi/agent/pi-blackhole/pi-blackhole-cooldown.json` — provider cooldowns
- `~/.pi/agent/pi-blackhole/debug.ndjson` — event log (only while `debugLog` is true)
- `~/.pi/agent/npm/node_modules/pi-blackhole/src/om/runtime.ts` — patched source
- `~/pi-blackhole-fallback-doctor/patches/runtime.ts` — durable patch copy
