---
name: pi-blackhole-fallback-chain
description: >-
  Configure, verify and repair the pi-blackhole observer/reflector/dropper model
  chains so observational memory actually gets written. Use when blackhole reports
  "observer skipped - all candidates failed", "no observations - model did not call
  the observation tool", "all candidates exhausted", or when memory is never
  persisted. Also use when choosing worker models, sizing observer prompts, or
  deciding whether a local ollama model can serve as a blackhole worker.
---

# pi-blackhole fallback chain

The observer/reflector/dropper chain decides whether pi-blackhole writes anything to
memory. This skill covers the measured-good worker configuration, how to verify a
live chain, and the failure modes that silently produce zero memory.

## The one thing to understand first

**Memory is only written when the observer cursor reaches state `recorded`.** A worker
that runs but never calls its tool must be treated as a *candidate failure*, and the
chain must advance to the next candidate. Before the local patch, a `tool_not_called`
result returned `"continue"` without advancing the chain *and* without moving the
cursor — so the same chunk failed identically forever. That is the "blackhole solves
nothing" symptom.

A worker that calls the tool but extracts **nothing useful** is nearly as bad: the
observation is written to disk permanently and the dropper may never remove it.

## Decision metric: coverage with grounded source ids

Do **not** select a worker on tool-call rate. Every model tested can call the tool.
Score on:

- **coverage** — fraction of the chunk's durable facts the worker actually surfaced
- **grounded source ids** — do the `sourceEntryIds` exist in the chunk, or are they
  invented?
- **preamble leakage** — did it record the already-known memory instead of the new chunk?

A model with a 100% tool-call rate, 0.0 coverage and 0/15 grounded ids is worse than
one that fails loudly.

## Measured worker ranking (2026-11-04)

Real observer contract, real `record_observations` tool schema, disjoint
already-known vs new fact pools, production prompt size, `max_tokens=16384`,
2 reps each:

| Worker | Cost | Median latency | Coverage | Grounded ids | Leak | Verdict |
|---|---|---|---|---|---|---|
| `openrouter/nvidia/nemotron-3.5-super-vl:free` | free | 33.3 s | **0.923** | 24/24 | 0 | **primary** |
| `openrouter/liquid/lfm-2.5-2.6b:free` | free | **5.2 s** | 0.616 | 19/19 | 0 | **fallback 1 — the tiny model** |
| `zai-2/glm-5.3` (`thinking: low`) | paid | 56.7 s | 0.846 | 29/29 | 0 | fallback 2 |
| `openrouter/nvidia/nemotron-3.5-lightning:free` | free | 51 s | 0.00 | — | — | rejected |
| `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free` | free | 300 s timeout | — | — | — | rejected |
| `openrouter/google/gemini-2.5-flash:free` | — | HTTP 404 (no longer free) | — | — | — | rejected |
| `ollama` `qwen2.5-fast:3.1b` | free | 102 s | 0.286 | **0/15** | — | rejected — hallucinates ids |
| `ollama` `qwen2.5-coder:1.5b` | free | 45 s | 0.00 | 12/12 | wrong target | rejected — records the preamble |
| `ollama` `llama3.2:3b` | free | errors | — | — | — | rejected |

`liquid/lfm-2.5-2.6b:free` is the **extremely small model** (2.6B): free, 5 seconds,
100% grounded ids. It is the fastest usable worker and the right fallback.

Local ollama models are **excluded** because their observations would be persisted
with invented source ids. Re-admit them only after an observation-validation gate
exists.

## The working configuration

`~/.pi/agent/pi-blackhole/pi-blackhole-config.json`:

```json
{
  "observerModel":  { "provider": "openrouter", "id": "nvidia/nemotron-3.5-super-vl:free", "contextWindow": 32768 },
  "reflectorModel": { "provider": "openrouter", "id": "nvidia/nemotron-3.5-super-vl:free", "contextWindow": 32768 },
  "dropperModel":   { "provider": "openrouter", "id": "nvidia/nemotron-3.5-super-vl:free", "contextWindow": 32768 },
  "observerFallbackModels": [
    { "provider": "openrouter", "id": "liquid/lfm-2.5-2.6b:free", "contextWindow": 32768 },
    { "provider": "zai-2",        "id": "glm-5.3",                    "contextWindow": 72000 }
  ],
  "reflectorFallbackModels": [ /* same two */ ],
  "dropperFallbackModels":   [ /* same two */ ],
  "observerChunkMaxTokens": 4000,
  "observerPreambleMaxTokens": 1500,
  "observationsPoolMaxTokens": 8000,
  "reflectionsPoolMaxTokens": 4000,
  "reflectorInputMaxTokens": 8000,
  "dropperInputMaxTokens": 8000,
  "workerAttemptTimeoutMs": 420000
}
```

`~/.pi/agent/models.json` needs `liquid/lfm-2.5-2.6b:free` registered under the
`openrouter` provider's `modelOverrides` (models.json resolves models from **both**
`provider.models` and `provider.modelOverrides`).

`zai-2/glm-5.3` needs `reasoning: true` in models.json — GLM-5.3 **cannot disable
thinking** and rejects `thinking: off` with error `1210`; pi sends `thinking: low`
because `settings.json` has `defaultThinkingLevel: "low"`.

## Verify

```bash
python3 ~/.pi/agent/skills/pi-blackhole-fallback-chain/scripts/verify_fallback_chain.py
```

Exit `0` and `VERDICT: USABLE` means the chains resolve. Then confirm the pipeline
actually completes, not just that it resolves — see `REFERENCE.md` for the live
end-to-end procedure and the log fields to look for.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| "observer skipped — all candidates failed" | cooldown/size skip terminated traversal | apply the runtime skip→continue patch |
| "no observations — model did not call the observation tool" | worker's tool-call failure silently ended the stage | apply the `tool_not_called` → next-candidate patch |
| observations contain the *old* memory | worker recorded the preamble, not the chunk | choose a worker with coverage > 0.5 |
| `sourceEntryIds` that match nothing | worker hallucinated ids | choose a worker with 100% grounded ids |
| worker truncated its output (`finish=length`) | output cap too low for the model | raise the output allowance; `max_tokens=16384` was needed |

## Traps

- **`num_ctx` must be ≥ 8192 for ollama.** The observer prompt floor is ~3900 tokens
  (a 14.3k-char blackhole system prompt + tool schema). At `num_ctx 4096` ollama
  silently truncates and the model regurgitates the tool schema as fake observations.
- **A long-running ollama server can enter a dead Metal state**
  (`failed to initialize the Metal library` → HTTP 500 for every model). `num_gpu 0`
  does not avoid it. Restart the server; the same tag then answers in ~2.5 s.
- **Send `keep_alive: 0`** on every ollama request when benchmarking, or each visited
  model stays resident (~2 GB each) and the 8 GB box swaps itself to death.
- **`models.json` model entries use the key `id`**, not `model`.
