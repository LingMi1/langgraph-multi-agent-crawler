# Architecture

LangGraph Multi-Agent Crawler is a **deterministic-first** pipeline: every node's default action runs without a model, and the LLM enters only at judgment points, under a semaphore. Each LLM failure degrades back to the deterministic path. The crawl is the product; the LLM is an enhancer.

## Supervisor graph

`graph/workflow.py` assembles a LangGraph `StateGraph` with conditional routing. The linear happy path:

```
START → Scout → Navigate → FetchExtract → Evaluate → Media → Storage → END
                              │  ▲          │  ▲
                              │  │(BFS loop)│  │(review verdict)
                              └──┘          │  │
                              └───────────► ConfigAdjust ◄┘
                                            CodeGen (LLM last resort)
                                            ReactTakeover (deep degrade)
```

## The 9 agents

All agents live in `graph/agents.py` and inherit one `BaseAgent` template method, which gives every node trace recording, exception isolation, and timing for free.

| Agent | Responsibility | LLM |
|---|---|---|
| ScoutAgent | analyze seed site → site profile + initial plan | no |
| NavigateAgent | extract navigation → fill BFS queue + section list | optional (classify) |
| FetchExtractAgent | fetch + rule-based cleaning + persist (deterministic first) | deep-degrade only |
| EvaluateAgent | quality gate + plan-completion check → adjudicate next step | yes (heuristic veto) |
| ConfigAdjustAgent | apply eval-driven config changes, re-crawl (≤3 times) | no |
| CodeGenAgent | LLM generates site-specific CSS-selector rules | yes |
| ReactTakeoverAgent | ReAct takeover when the deterministic chain fully fails | yes |
| MediaProcessorAgent | image filtering (decor/qr) + externalize + global dedup | no |
| StorageAgent | CSV persist + site-pattern memory write | no |

## The degradation chain

`route_after_evaluate` (`graph/workflow.py`) is the single gate:

1. **Evaluate passed** → media → storage. Done.
2. **Failed, adjustments < 3** → `config_adjust`: apply a UA / JS-render / header change, clear the queue, re-crawl.
3. **3 attempts exhausted** → `code_gen`: the LLM generates a site-specific CSS-selector rule set, validated before use.
4. **Still failing** → `react_takeover`: a ReAct loop with a whitelisted tool set decides `retry` or `giveup` — the system can finish a site even when every deterministic and generative path has failed.

Each rung is cheaper than the last to enter and more expensive to have needed; the chain guarantees the crawl terminates with content saved, never dropped.

## Reliability primitives

- **Run-level circuit breaker** (`agents/breaker.py`) — both LLM entry points share one breaker: 3 consecutive exhausted-retry failures fast-fail every later call in the run; a single success resets; reset at run start. The breaker blocks only the LLM, never the crawl.
- **Batched post-hoc rescue** — the BFS hot path runs at zero LLM (selector locate reads a SQLite-persisted cache). Under-par pages go to a rescue queue grouped by URL template, so one LLM call locates the selector for a whole section.
- **Multi-provider failover** (`agents/llm_pipeline.py`) — `chat_json`/`chat_stream` switch to backup base URLs once the primary exhausts retries.
- **Budget-triggered compaction** (`agents/react.py`) — over-budget conversation history is folded into one summary; LLM summarizer first, a rule summarizer as fallback.
- **Offline golden harness** (`tools/golden_check.py`) — 3 template sites, P/R/F1 with a conservative overlap accounting, `--json` output; regressions gate CI.

## Storage, memory & safety

- **CSV + files**: every run writes `output/<netloc>/crawl_results.csv` plus per-page HTML files under the site's section hierarchy (see `agents/storage.py`).
- **Dedup**: content fingerprints (MD5 of structured body + title) keep identical pages stored once; duplicate CSV rows carry a `<!-- duplicate … -->` placeholder and are excluded from exports.
- **Site memory** (`memory.py`): `site_patterns` persists site type / JS-render / template hints; a second crawl of the same site skips re-reconnaissance.
- **Safety** (`agents/safety.py`): untrusted HTML is wrapped with an explicit "data, not instructions" declaration; strict Pydantic output schemas; conflict veto demotes LLM verdicts that contradict deterministic extraction.
- **Compliance** (`agents/fetcher.py`): robots.txt check on by default (stdlib `robotparser`), TLS verification on by default, self-imposed frequency limiting.

## Detailed docs

- [Installation](installation.md)
- [API reference](api.md)
- [Development](development.md)
