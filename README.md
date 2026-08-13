# TradeDesk

A conversational paper-trading assistant. Users chat with it to learn trading concepts, check
quotes and their portfolio, and place **simulated** trades against Alpaca's paper-trading API.

**AI.Accelerate FY26 capstone — Path B (open).** Chidera Onyebu. Demo target: Discover ServiceNow.

> ### No real money. No financial advice.
>
> Every order routes to Alpaca **paper trading**. The agent handles no real funds and holds no
> real positions. It will not answer "should I buy NVDA?" — that intent (`out_of_scope`) redirects
> to education. Answering it would be unlicensed financial advice, and the redirect is the product
> working as designed, not a gap in it.

## Why this domain

Trading has naturally high-stakes actions, which makes the brief's agent-safety concepts
load-bearing rather than decorative. An agent that can move simulated money forces real design
decisions — intent gating, human confirmation, authorization enforced in code — and those
decisions are the story worth telling at DSN.

## Architecture

```
Chat UI → FastAPI → LangGraph StateGraph → handler node → reply
                         │
                    classify_intent (structured JSON, Pydantic-validated)
                         │
                    ┌────┴─────┬──────────┬───────────┬──────────────┐
                 educate    research   portfolio    trade      out_of_scope
                  (RAG)     (quotes)   (positions)  (GATED)     (redirect)
```

```
app/
├── agent.py         # hand-rolled orchestration, kept as the graph's differential-test oracle
├── graph/
│   ├── build.py     # the compiled LangGraph StateGraph
│   ├── handlers.py  # one async handler per intent
│   ├── classifier.py
│   ├── confirmation.py  # the gate's yes/no parsing
│   ├── llm_classifier.py  # Claude-backed classifier, fails closed to out_of_scope
│   ├── tools.py     # the ONLY place broker calls happen; role checks live here
│   └── state.py
├── schemas/         # Pydantic intent taxonomy + order models
├── rag/             # chunking (heading-based) + lexical and vector retrievers
├── brokerage/       # BrokerageClient interface → mock | alpaca (paper only)
├── auth/            # role decorators
├── main.py          # FastAPI: sessions in, replies out — no logic of its own
└── mcp_server.py    # Part 5: the gate as a propose/confirm token handshake
corpus/              # 13 self-authored brokerage policy + education docs
evals/               # 18 adversarial scenarios, asserted at the broker
scripts/demo.py      # the DSN walkthrough (offline)
scripts/run_evals.py # the adversarial scorecard
docs/SAFETY_AUDIT.md # 8 risks, each pinned to a named test
```

## The safety design

Six mechanisms, designed in from day one rather than bolted on:

**1. The confirmation gate.** The agent never executes a trade in the turn it parses one. It
echoes a structured order back and executes only after an explicit yes. One turn of separation
between "the model thinks you want this" and "money moves."

**2. The LLM never builds API calls.** It fills a Pydantic `OrderRequest`; Python builds the
request. Model output is data to be validated, never a command to be run.

**3. Server-side validation.** Symbol allowlist, quantity and buying-power checks, market-hours
check — re-checked at the broker boundary even though the schema already validated. Upstream
validation is a convenience; the boundary check is the guarantee.

**4. Authorization in code, not prompts.** Role checks are decorators in the tool layer that raise
403. A system prompt saying "you may not trade" is a request the model usually honors — and
"usually" is not a security property. See [`app/auth/roles.py`](app/auth/roles.py).

**5. Idempotent submission.** A `client_order_id` is generated when the order is *proposed* and
reused on every retry, so a timeout that hides a successful fill cannot double-buy.

**6. Structured refusal.** `out_of_scope` is a real intent, not a parse-failure bucket.

## Lab mapping

| Part | Implementation |
|---|---|
| 0 — core | Trace a message end-to-end; identify router splice points |
| 1 — core | RAG over 13 self-authored docs; heading-based chunking with title prefixing; low-confidence retrieval returns an honest "not in my docs" |
| 2 — core | LangGraph: `classify_intent` → router → handlers; trades pass the confirmation gate against a mock broker |
| 3 — ext | Swap mock → Alpaca behind `BrokerageClient`; env-var auth, error mapping, retries |
| 4 — ext | Compliance persona: read-only, cross-account, cannot trade |
| 5 — ext | MCP server; the gate survives the protocol as a `propose_order` → single-use token → `confirm_order` handshake |

## Demo plan

**End-to-end:** definition question (cited RAG answer) → live quote → trade request →
confirmation → fill → updated positions.

**A failure I fixed:** naive fixed-size chunking split the pattern-day-trader rule across chunk
boundaries, so retrieval returned half a rule and the model confidently completed the other half
wrong. Heading-based chunking with title prefixing fixed it. Before/after shown live — this is
the most valuable 60 seconds of the demo, because the brief explicitly says judges want the
reasoning more than a clean happy path.

**One design decision:** confirmation gate + code-level authorization, demonstrated by a jailbreak
prompt failing at the Python role check.

## Understanding the code

[`docs/CODE_WALKTHROUGH.md`](docs/CODE_WALKTHROUGH.md) walks the path a message takes through
the system — front door, graph, classifier, proposal, role checks, gate — plus the RAG failures
that shaped the design, and short answers to the questions the design invites.

## Governance

[`docs/SAFETY_AUDIT.md`](docs/SAFETY_AUDIT.md) works through eight risks — unconfirmed execution,
prompt injection, role escalation, double execution, financial advice, hallucinated education,
reaching a live endpoint, and fat-finger orders. Every "test performed" cell names a real eval
scenario or unit test, and every residual risk is stated rather than rounded to zero.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload      # then open http://localhost:8000
```

A single-page chat UI backed by FastAPI. No API key needed — the whole stack (mock broker,
lexical retriever, keyword classifier) runs offline, so the demo can't be broken by venue wifi.

Three opt-in swaps, all env-var-gated so the offline default stays the default:

```bash
TRADEDESK_LLM_CLASSIFIER=1         # Claude-backed intent classifier (needs ANTHROPIC_API_KEY)
TRADEDESK_BROKER=alpaca            # real Alpaca paper trading (needs APCA_API_KEY_ID + APCA_API_SECRET_KEY)
TRADEDESK_RETRIEVER=chroma         # embedding retrieval (downloads model weights on first start)
```

Switches can live in a `.env` file instead of the shell — `cp .env.example .env` and fill it in.
The entrypoints load it at startup (python-dotenv), and shell-exported variables always win over
the file. `.env.example` documents every variable the code reads, including
`TRADEDESK_MAX_SESSIONS`, the cap on stored chat sessions (default 500, least-recently-used
eviction — the session endpoint is unauthenticated, so the store is bounded by design).

The Alpaca client (`app/brokerage/alpaca.py`) hard-codes the paper-trading host — there is no
configuration that points it at the live API. Duplicate `client_order_id` submissions return the
original fill instead of re-buying, so a retry after a timeout can never double-execute.

Or run the scripted walkthrough without a browser:

```bash
python scripts/demo.py
```

**Never commit `.env`.** It is gitignored. Use only paper-trading keys from
`https://paper-api.alpaca.markets` — live keys have no business in this project.

## Status

**Parts 0–5 work, plus the eval suite and both retrievers.** `python scripts/demo.py` runs the
full conversation through the compiled LangGraph, fully offline — mock broker, lexical retriever,
keyword classifier. No API key, no network. 293 tests, ruff clean, mypy strict clean.

That offline property is deliberate. Venue wifi is a real risk, and every stand-in sits behind
the same interface as its real counterpart (`MockBroker`/Alpaca, `LexicalRetriever`/Chroma,
`KeywordClassifier`/LLM). The safety properties live in Python, so swapping any of them cannot
remove them.

### The gate is the graph's shape

The compiled wiring, printed from the real graph rather than drawn by hand:

```
__start__          -> classify_intent
__start__          -> confirmation      <-- the ONLY path to a fill
classify_intent    -> educate | research | portfolio | trade | cancel_modify | out_of_scope
trade              -> __end__
confirmation       -> __end__
```

Note what's absent: **there is no `trade -> confirmation` edge.** Proposing an order cannot flow
into filling it. `test_trade_has_no_edge_to_confirmation` fails if anyone ever adds one for
convenience.

The pending-order check is the conditional *entry* point rather than a node after classification.
If classification ran first, "yes" would be handed to the classifier and routed by its own logic,
leaving the order alive in state for a later turn to resurrect.

### Done

- Parts 0–2: RAG pipeline, acting agent, confirmation gate, compiled LangGraph
- **FastAPI app + chat UI** ([`app/main.py`](app/main.py)) — per-session state, the gate verified
  across separate HTTP requests, sessions isolated from each other
- **Full corpus** — 13 self-authored docs (PDT rule, order types, settlement, margin, fees,
  market hours, spread, short selling, dividends, account types, risk, FAQ, time-in-force)
- **LLM classifier** ([`app/graph/llm_classifier.py`](app/graph/llm_classifier.py)) — Claude
  structured output, fails closed to `out_of_scope`, opt-in via `TRADEDESK_LLM_CLASSIFIER=1`
- **Part 3: Alpaca paper broker** ([`app/brokerage/alpaca.py`](app/brokerage/alpaca.py)) —
  paper host hard-coded, Decimal-safe money, idempotent retries via `client_order_id`, error
  taxonomy mapped to the handlers' language; opt-in via `TRADEDESK_BROKER=alpaca`
- **Adversarial eval suite** ([`evals/`](evals/)) — 18 scripted attack conversations
  (prompt injection, fake authority, roleplay jailbreaks, stale confirmations, role
  escalation) asserted on *behavior*: the strongest check is "the broker was never called",
  recorded by a spy, not "the agent said no". `python scripts/run_evals.py`; runs in CI via
  pytest, and the same suite runs against the LLM classifier to prove the swap didn't weaken
  the gate
- **Part 5: MCP server** ([`app/mcp_server.py`](app/mcp_server.py)) — TradeDesk's tools for
  any MCP client (`python -m app.mcp_server`). The confirmation gate survives the protocol as
  a two-tool handshake: `propose_order` returns a single-use token with a 120s TTL;
  `confirm_order` executes only with that exact token, and any failed check burns the
  proposal. The one exception is a transport failure mid-confirm — no answer is not a failed
  check, so the proposal survives and the same token retries with the same proposal-time
  `client_order_id`, which cannot double-execute. `TRADEDESK_MCP_ROLE=compliance` starts it
  read-only; `TRADEDESK_BROKER=alpaca` routes to the paper account
- **Chroma vector retriever** ([`app/rag/vector.py`](app/rag/vector.py)) — embedding
  retrieval (ONNX MiniLM via Chroma) behind the same `Retriever` protocol; opt-in via
  `TRADEDESK_RETRIEVER=chroma`. The refusal threshold survives the swap: scores are cosine
  similarity in [0, 1], and out-of-domain queries land near 0 — verified with real embeddings
  ("day trading with a small account" → PDT doc at 0.65; a recipe question → 0.06)
- 293 tests (unit + API integration + evals), ruff + mypy strict clean

### Still to build

Nothing — every planned part (0–5) plus the eval suite and the Chroma retriever is built.
Remaining switches are environmental: `ANTHROPIC_API_KEY` for the LLM classifier,
`APCA_*` keys for live paper trading.

## License

MIT — see [LICENSE](LICENSE).
