---
name: pi-blackhole-fallback-chain
description: Verify and repair the pi-blackhole worker fallback chains (observer, reflector, dropper). Use when pi-blackhole reports "observer skipped — all candidates failed", "no model available for observer (all candidates exhausted)", when the observer/dropper never runs, or when a worker chain stops at the first cooldown-skipped provider instead of traversing to the next fallback.
---

# pi-blackhole fallback chain

The pi-blackhole extension runs three background workers — **observer**,
**reflector**, **dropper**. Each worker resolves a model through a *chain*:

```
stage model -> stage fallbacks -> (base config model)   [session fallback disabled]
```

Two independent things must hold for the chain to work:

1. **Configuration** — every stage declares a non-empty chain, and every member
   is a real, enabled model with a usable context window.
2. **Runtime** — a candidate that is skipped (cooldown, size) must **continue**
   to the next candidate. Older `pi-blackhole` builds *stop* traversal at the
   first cooldown skip, which surfaces as
   `no model available for observer (all candidates exhausted, sessionFallback disabled)`.

## Run the verifier

```bash
python3 ~/.pi/agent/skills/pi-blackhole-fallback-chain/scripts/verify_fallback_chain.py
```

Exit codes: `0` usable, `1` broken, `2` config/IO error.

Checks performed:

| Code | Level | Meaning |
|------|-------|---------|
| C1 | FAIL | stage chain empty or missing |
| C2 | FAIL | chain member not a known provider/model pair |
| C3 | FAIL | chain member disabled in `models.json` |
| C4 | FAIL | chain member has no context window (would be size-skipped) |
| W1 | WARN | no explicit `contextWindow` override on the member |
| W2 | WARN | member is currently in provider cooldown |
| W3 | INFO | member is a local (ollama) fallback — survives provider outages |
| R1 | FAIL | runtime patch missing — chain stops at the first cooldown skip |
| R2 | INFO | runtime patch present |

## Configuration shape

Chains live in `~/.pi/agent/pi-blackhole/pi-blackhole-config.json` under
**top-level** keys — *not* nested inside the stage model object:

```jsonc
{
  "observerModel":  { "provider": "openrouter", "model": "nvidia/nemotron-3.5-super-vl:free" },
  "observerFallbackModels": [
    { "provider": "openrouter", "model": "nvidia/nemotron-3.5-lightning:free" },
    { "provider": "zai",         "model": "glm-4.5-flash" },
    { "provider": "ollama-direct", "model": "qwen3:0.6b-8k", "contextWindow": 81920 }
  ],
  "reflectorModel": { ... }, "reflectorFallbackModels": [ ... ],
  "dropperModel":   { ... }, "dropperFallbackModels":   [ ... ],

  "workerAttemptTimeoutMs": 420000,
  "observeOnShutdown": true,
  "observeAfterTokens": 15000
}
```

Rules that matter in practice:

- **Always end the chain in a local model** (`ollama-direct/...`). Remote free
  providers rotate through 429s; a local tail keeps the observer alive.
- **Give the local tail an explicit `contextWindow`** larger than the ollama
  model's real `num_ctx`. Blackhole decides "fits" from this value, and ollama
  silently head-truncates oversized prompts instead of erroring, so over-declaring
  is safe while under-declaring causes a size-skip.
- **Set `workerAttemptTimeoutMs`** (e.g. `420000`). Unset means "no deadline" —
  a hung provider stalls the worker forever.
- Keep the *same* chain shape for all three stages; the resolver is shared.

## Runtime patch (the skip → continue fix)

Upstream `pi-blackhole` stops the candidate loop after the first cooldown skip.
The fix replaces that with `continue` and enriches the skip log with
`until` / `reason` / `cooldownStage`.

Durable copies live in the sibling repo `~/pi-blackhole-fallback-doctor`:

```bash
# re-apply after any `npm update pi-blackhole` (node_modules is wiped)
cp ~/pi-blackhole-fallback-doctor/patches/runtime.ts \
   ~/.pi/agent/npm/node_modules/pi-blackhole/src/om/runtime.ts
```

The installed bundle `dist/index.js` must also contain the fix (`grep -c
cooldownStage`). Then **restart every pi session** — running processes keep the
old code in memory.

## Diagnosing a live failure

1. `python3 ~/.pi/agent/skills/pi-blackhole-fallback-chain/scripts/verify_fallback_chain.py`
2. Inspect cooldowns: `cat ~/.pi/agent/pi-blackhole/pi-blackhole-cooldown.json`
   (keys are `provider/model`, lowercased).
3. Enable `debugLog: true` in the config, reproduce, then read
   `~/.pi/agent/pi-blackhole/debug.ndjson` — look for `model.cooldown_skip`,
   `observer.model_unavailable`, `observer.records`, `cursor.saved`.
4. Clear cooldowns by deleting the offending keys from the cooldown file.

**Note:** `debug.ndjson` only exists while `debugLog` is true — with it off the
file goes stale, and live errors then appear *only* inside the session JSONL
(`~/.pi/agent/sessions/<project>/<session>.jsonl`) as assistant entries with
`stopReason: length`.

## Related

- `~/.pi/agent/skills/pi-blackhole-fallback-doctor/` — deeper audit + the
  runtime patch source of truth (`audit_fallback_chain.py`).
- Repo: https://github.com/Das-rebel/pi-blackhole-fallback-doctor

---

## Local worker models (the "small local model" solution)

A blackhole worker that runs a **local** model is the only configuration immune
to provider 429s, expiring plans and evening throttles. But most small models
**cannot do the job**: the observer/reflector/dropper stages require a *native
tool call* (`record_observations` / `reflect` / `drop`), and Qwen2.5-class models
emit the tool JSON as **plain text** instead.

Verified on this machine (Apple M1, 8 GB) by replaying the real observer
contract (blackhole's `OBSERVER_SYSTEM` + `RecordObservationsSchema` + a
source-addressed chunk) against ollama's OpenAI-compatible endpoint:

| Model | Native tool call | Speed | Verdict |
|-------|------------------|-------|---------|
| `qwen3:0.6b-8k` | no | 79 s | echoes the chunk — unusable |
| `qwen2.5-coder:1.5b` | no (text-form JSON) | - | unusable |
| `qwen2.5:3b` | yes | 47 s | works, slow |
| `qwen2.5-fast:latest` | yes | 8 s | works |
| `josie:latest` | yes | 86 s | best quality, slowest |
| `llama3.2:3b` | yes | 6-18 s | **best choice** — native tool calls, no thinking channel |

Do **not** set `temperature: 0` on these models: greedy decoding
*deterministically* omits the tool-call wrapper (0/5 success). Use
`temperature: 0.2` and accept ~80 % per-call success — the
candidate-continuation patch below covers the rest.

### Working local worker recipe

Modelfile for the worker tag (`num_ctx 16384`, GPU offload, mild sampling):

```
FROM llama3.2:3b
PARAMETER num_ctx 16384
PARAMETER num_gpu -1
PARAMETER temperature 0.2
PARAMETER num_predict 1024
PARAMETER top_p 0.9
```

Create it with `ollama create bw-worker -f <modelfile>`.

Register it in `~/.pi/agent/models.json` under the `ollama-direct` provider
(baseUrl `http://127.0.0.1:11434/v1`):

```jsonc
{ "id": "bw-worker", "name": "Blackhole local worker", "reasoning": false,
  "contextWindow": 16384, "maxTokens": 4096,
  "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0 } }
```

Then point all three stages at it, with the remote free models as fallbacks:

```jsonc
"observerModel":  { "provider": "ollama-direct", "id": "bw-worker", "contextWindow": 16384 },
"observerFallbackModels": [
  { "provider": "openrouter", "id": "nvidia/nemotron-3.5-super-vl:free" },
  { "provider": "openrouter", "id": "nvidia/nemotron-3.5-lightning:free" },
  { "provider": "zai", "id": "glm-4.5-flash" }
]
```

**Shrink the prompt.** A local model cannot digest a 64K prompt. The observer
prompt is `static (~3.5K) + preamble (memory pool) + chunk`. Upstream issue
k0valik/pi-blackhole#115: an uncapped preamble grows with the memory pool and
makes every observer call fail.

```jsonc
"observerChunkMaxTokens": 8000,     // was 40000
"observerPreambleMaxTokens": 3000,  // was 0 = auto(30%) => ~24000 observed
"observationsPoolMaxTokens": 8000,
"reflectionsPoolMaxTokens": 4000,
"reflectorInputMaxTokens": 20000,
"dropperInputMaxTokens": 20000
```

### The `tool_not_called` patch (essential for local models)

A local model that runs but never calls its tool used to **silently end the
stage**: the observer returned `"continue"`, the cursor never advanced, and the
identical chunk failed identically on every future cycle. That is the
"blackhole solves nothing" symptom.

Patch (applied to `src/om/consolidation.ts` **and** `dist/index.js`):

```ts
if (reason?.kind === "tool_not_called") {
  runtime.skipFailedForCycle(stageModelForThinking);
  debugLog("observer.empty_retry", { coversUpToId, model: provider/id });
  if (!stageModelForThinking) break;
  continue;   // let the fallback chain take the next candidate
}
```

Durable copy + idempotent re-apply script:
`~/pi-blackhole-fallback-doctor/patches/apply-patches.sh` (re-run after any
`npm update pi-blackhole`, then **restart every pi session** — the extension
runs in-process).
