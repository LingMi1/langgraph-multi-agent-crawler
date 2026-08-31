# Security Policy

LangGraph Multi-Agent Crawler is a research project — a multi-agent web harvester. It is not a hardened product, but it follows **security by default, explicit exceptions** for the parts that face untrusted input and the network.

[简体中文版](SECURITY.zh-CN.md)

## Supported / Defaults

| Concern | Default | Override |
|---------|---------|----------|
| TLS certificate verification on outbound crawl requests | **On** (`CRAWLER_TLS_VERIFY=true`) | Set `false` only for intranet sites with self-signed certs |
| robots.txt compliance (crawls only `User-agent: *` wildcard rules) | **On** (`CRAWLER_RESPECT_ROBOTS=true`) | Set `false` only when you explicitly own the target |
| API key auth (`X-API-Key`) | Off when `CRAWLER_API_KEY` is unset (local-dev friendly) | Set `CRAWLER_API_KEY` to enforce |
| LLM prompt injection defense | 3 layers, see below | Not recommended to disable |

## How untrusted input is handled

The harvester ingests **untrusted HTML** from the open web and feeds snippets to an LLM. Defense is layered, fail-closed where it matters:

1. **Separation & declaration** — untrusted content is wrapped by `wrap_untrusted()` with an explicit label and length cap before ever reaching a prompt, so the model can distinguish "data" from "instructions".
2. **Output schema validation** — all LLM outputs (`EvaluationResult`, `ExtractionRules`, …) are validated against strict Pydantic schemas before use; anything that fails is discarded, not partially trusted.
3. **Conflict demotion** — when an LLM verdict directly contradicts the deterministic extractor, `guard_llm_verdict()` demotes the LLM opinion instead of silently trusting it.

Any detected injection attempt is logged (see `log_injection_warning`).

## API layer

- Auth: `secrets.compare_digest` (constant-time) comparison of `X-API-Key` against `CRAWLER_API_KEY`; unset key → open (local dev), set key → all endpoints 401 on mismatch.
- Rate limiting: sliding 60s windows — 6 submissions and 30 SSE connections per client; identity prefers `X-Forwarded-For` (first hop) when behind a reverse proxy, else the API key.
- Task table capped (`deque(maxlen=20)`) to bound in-memory growth; per-task log ring buffer capped at 4000 lines.

## Outbound crawl safety

- Every request verifies the peer certificate unless explicitly exempted.
- Before fetching a URL, the target origin's `robots.txt` is consulted (wildcard `User-agent: *` only). Missing robots / 404 / network failure → **allow** (never let compliance checks stall the crawl). Disallowed paths return `blocked_by_robots`.
- Robots parsers are cached **per origin** (not per URL), so per-path rules like `Disallow: /private` stay correct.

## Reporting a vulnerability

This is a research project, not a production service. If you still find something exploitable:

- **Do not** open a public issue with exploit details.
- Send a short report to the repository owner (issues tab → private note) with: affected endpoint/module, a minimal repro, and impact.

## Explicit non-goals

- Multi-tenant isolation, SSRF hardening against arbitrary intranet targets, secret rotation, and formal threat modelling are **out of scope** for this project and noted here so no one assumes otherwise.
