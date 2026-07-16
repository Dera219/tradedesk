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
│   ├── tools.py     # the ONLY place broker calls happen; role checks live here
│   └── state.py
├── schemas/         # Pydantic intent taxonomy + order models
├── rag/             # chunking (heading-based + naive) + retrieval
├── brokerage/       # BrokerageClient interface → mock | alpaca
└── auth/            # role decorators
corpus/              # self-authored brokerage policy + education docs (1 of ~13 written)
scripts/demo.py      # the DSN walkthrough
```

Directories are created when there is something to put in them. `app/mcp/` and `app/main.py`
don't exist yet because Part 5 and the FastAPI layer aren't built — an empty folder is a promise,
not progress.

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
| 1 — core | RAG over ~20 self-authored docs; heading-based chunking with title prefixing; low-confidence retrieval returns an honest "not in my docs" |
| 2 — core | LangGraph: `classify_intent` → router → handlers; trades pass the confirmation gate against a mock broker |
| 3 — ext | Swap mock → Alpaca behind `BrokerageClient`; env-var auth, error mapping, retries |
| 4 — ext | Compliance persona: read-only, cross-account, cannot trade |
| 5 — ext | MCP server exposing read-only tools; `place_order` deliberately excluded |

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

## Timeline

| Days | Work |
|---|---|
| 1–2 | Skeleton walkthrough, intent router |
| 3–4 | RAG pipeline |
| 5–7 | **Full acting agent on mock broker — minimum demoable product** |
| 8–9 | Alpaca integration |
| 10 | Compliance persona |
| — | MCP (stretch) |

Polish stops at day 7 if time runs short. Because mock and Alpaca share one interface, the demo
runs fully offline — worth rehearsing that path at least once, since venue wifi is a real risk.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # add your Alpaca PAPER keys
uvicorn app.main:app --reload
```

**Never commit `.env`.** It is gitignored. Use only paper-trading keys from
`https://paper-api.alpaca.markets` — live keys have no business in this project.

## Status

**Parts 0–2 work.** `python scripts/demo.py` runs the full conversation through the compiled
LangGraph, fully offline — mock broker, lexical retriever, keyword classifier. No API key, no
network. 160 tests, ruff clean, mypy strict clean.

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

### Still to build

LLM classifier, Chroma retriever, Alpaca broker (Part 3), MCP surface (Part 5), `app/main.py`
FastAPI wiring, and ~12 more corpus docs.

## License

MIT — see [LICENSE](LICENSE).
