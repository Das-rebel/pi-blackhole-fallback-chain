# REFERENCE — pi-blackhole worker chains

Measured evidence behind `SKILL.md`. All numbers are from the real
`record_observations` contract with the real blackhole system prompt and tool schema.

## The harness

`/tmp/bw_bench4.py` is the reference harness:

- loads the **real** observer system prompt from
  `pi-blackhole/src/om/agents/observer/prompts.ts` and the **real** tool schema from
  `.../observer/tool-schema.ts`
- builds a prompt with **disjoint** fact pools: the preamble contains facts the model
  already knows, the chunk contains new facts plus one deliberate contradiction
- scores: native tool call, observations per call, **grounded `sourceEntryIds`**,
  **coverage** (fraction of chunk facts surfaced), **preamble leakage**, relevance
  enum validity, latency, prompt/output tokens

Derived scripts: `bw_bench6.py` (local-only + text-JSON path + `keep_alive: 0`),
`bw_bench_or.py` (OpenRouter free tier), `bw_bench_glm.py` (zai-2),
`bw_final.py` (production-settings head-to-head).

## Three harness bugs that produced false conclusions

Each of these manufactured a "the model is bad" result that was actually a
measurement artifact.

1. **Shared fact pool (v3).** Preamble and chunk drew from the same fact list, so a
   correct model returned `observations: []` — the *right* answer. This is where
   `zai-2/glm-5.3` got wrongly convicted. Fixed by disjoint pools in v4.
2. **`num_ctx 4096` truncation.** The observer prompt is ~4450 tokens
   (14.3k-char system prompt + tool schema + chunk). At `num_ctx 4096` ollama
   silently truncated it and the model emitted the **tool schema back** as fake
   observations. `ptok=2050` at 4096 vs `ptok=4164` at 8192.
3. **`max_tokens=4096` cap.** `nemotron-3.5-super-vl:free` showed `finish=length`
   with zero observations on the larger prompt — but blackhole allows 32000 output.
   At `max_tokens=16384` the same model scored coverage 0.923 with 24/24 grounded ids.

Plus one **environment** bug: a long-running ollama server in a dead Metal state
made every local model fail with HTTP 500 while the models themselves were fine.

## Full measurement table

Production settings (`max_tokens=16384`, 12 preamble + 12 chunk facts, 2 reps):

| Model | Cost | Median | Coverage | Grounded | Leak | Tool calls |
|---|---|---|---|---|---|---|
| `openrouter/nvidia/nemotron-3.5-super-vl:free` | free | 33.3 s | 0.923 | 24/24 | 0 | 2/2 |
| `openrouter/liquid/lfm-2.5-2.6b:free` | free | 5.2 s | 0.616 | 19/19 | 0 | 2/2 |
| `zai-2/glm-5.3` (`thinking: low`) | paid | 56.7 s | 0.846 | 29/29 | 0 | 2/2 |

Smaller prompt (20 preamble + 6 chunk facts):

| Model | Median | Coverage | Grounded | Note |
|---|---|---|---|---|
| `openrouter/nvidia/nemotron-3.5-super-vl:free` | 14.8 s | 0.857 | 12/12 | 2/2 reproducible |
| `openrouter/liquid/lfm-2.5-2.6b:free` | 3.4 s | 0.857 | 7/7 | 2/3 calls HTTP 429 (free-tier rate limit) |
| `zai-2/glm-5.3` | 35 s | 0.857 | 19/19 | 3/3 |
| `ollama qwen2.5-fast:3.1b` (8192 ctx) | 101.6 s | 0.286 | **0/15** | invents source ids |
| `ollama qwen2.5-coder:1.5b` (8192 ctx) | 45 s | 0.00 | 12/12 | records the preamble; 1 of 2 reps returned nothing |

Other free candidates screened and rejected: `nvidia/nemotron-3.5-lightning:free`
(no tool call, `finish=length`), `nvidia/nemotron-3-ultra-550b-a55b:free` (300 s
timeout), `google/gemini-2.5-flash:free` (HTTP 404 — no longer free),
`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` (`finish=length`),
`google/gemma-4-31b-it:free` (HTTP 504), `recursive/flint-v1.0:free`
(`finish=length`), `xingchen-agi/xing4.0-29b-a4b:free` (no output),
`cohere/north-mini-code:free` (malformed tool args).

`liquid/lfm-2.5-2.6b:free` was also tested at 12+12 facts where it was rate-limited
2 of 3 times — the free tier's rate limit is the risk, not the model.

### Local LFM2.5 attempt

`LiquidAI/LFM2.5-2.6B-GGUF` (Q4_K_M, 1.6 GB) imports into ollama cleanly
(`FROM /tmp/LFM2.5-2.6B-Q4_K_M.gguf`), but the embedded chat template does **not**
produce native tool calls: 83 s, 1024 generated tokens, empty content,
`done_reason=length`. Making this work locally needs a custom tool-call template
for the LFM2 format. Until then the free *hosted* LFM2 is the answer.

## Live end-to-end verification

Headless `pi -p` **cannot** test observers: the child inherits
`PI_BLACKHOLE_ROOT_SESSIONS`, is classified as a subagent, and the `agent_end`
observer aborts on disposal after the first candidate. A live interactive session
is required.

```bash
# 1. a fresh pi in tmux, with every PI_* var stripped so it is session:main
tmux new-session -d -s bhfinal -x 250 -y 50
tmux send-keys -t bhfinal \
  'env -u PI_BLACKHOLE_ROOT_SESSIONS -u PI_SESSION_ID -u PI_SESSION_FILE \
       -u PI_MODEL -u PI_PROVIDER -u PI_CODING_AGENT -u PI_REASONING_LEVEL \
       pi --provider minimax --model MiniMax-M3' Enter

# 2. paste a >15k-token conversation (send-keys -l fails above ~80KB)
tmux load-buffer -b bh /tmp/bh_big.txt && tmux paste-buffer -b bh -t bhfinal
tmux send-keys -t bhfinal M-Enter        # Alt+Enter submits the multiline editor

# 3. read the blackhole debug log
python3 -c "
import json
for l in open('$HOME/.pi/agent/pi-blackhole/debug.ndjson'):
    d=json.loads(l); print(d.get('event'), {k:v for k,v in d.items() if k not in ('ts','event')})
"
```

A healthy run logs:

```
observer.start → observer.empty (a candidate failed) → observer.start →
observer.records {count:3} → observer.appended {count:3} →
reflector.start {newObsCount:3} → dropper.result {not_over_target} →
cursor.saved {observer: "recorded", reflector: "recorded"}
```

`cursor.saved` with `observer: "recorded"` is the proof that memory was written.

Set `debugLog: true` in `pi-blackhole-config.json` for this test and turn it back
off afterwards.

## Where memory actually lands

Not in the pi-blackhole directory — its `*-pending.json` files hold only cursor
state. The store is **SQLite `~/.pi/memory/memory.db`, table `semantic`**, with FTS5
mirror tables (`semantic_fts`, `lessons_fts`). Sample row from the verification run:

```
key:    project.observatory.deployment
content: Observatory deployment PROMOTED from staging -> production on Tuesday
         2026-11-03 (user stated "last Tuesday" on Fri 2026-11-06; date resolved
         accordingly)
confidence: 0.95   source: user
```

Note the two behaviours worth keeping: the worker **resolved a relative date** against
the conversation date, and it **flagged ambiguity** ("every second Tuesday" → 11-10 vs
11-17) instead of guessing.

## Ollama configuration that works

```
FROM llama3.2:3b
PARAMETER num_ctx 8192       # 4096 truncates the observer prompt
PARAMETER num_gpu -1
PARAMETER temperature 0.2    # temperature 0 yields ZERO native tool calls
PARAMETER num_predict 1024
PARAMETER top_p 0.9
```

## ollama / environment traps

- **Dead Metal state.** `failed to initialize the Metal library` /
  `failed to allocate context` / `MTL0 backend` → every model returns HTTP 500.
  `num_gpu 0` does **not** avoid it (macOS llama.cpp always initialises Metal).
  Fix: restart the ollama server. Verified: the same tag that 500'd then PONGed in 2.5 s.
- **`keep_alive: 0`** on every request when sweeping models — otherwise each visited
  model stays resident (~2 GB each) and an 8 GB box drives 9.5/10 GB of swap, which
  makes every latency measurement meaningless.
- **Swapped-out box:** a 764-token prompt taking 602 s is not a model property, it is
  swap thrash.
- **Local latency numbers on this box are noisy**: Brave (37 processes) and several
  pi sessions compete for memory and GPU.
