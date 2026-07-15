# RAG corpus

Self-authored brokerage policy and education documents. Target ~20 files.

## Why the structure matters

These documents are not just content — their **heading structure is the chunking strategy**.
`app/rag/ingest.py` splits on headings and prefixes each chunk with its document title and
heading path, so a retrieved chunk carries the context needed to interpret it.

This is the fix for the failure in the demo plan. Naive fixed-size chunking split
[`pattern-day-trader-rule.md`](pattern-day-trader-rule.md) mid-rule: one chunk ended with "at
least $25,000 in account equity" and the next began with "if a flagged account drops below." A
question about the equity minimum retrieved a fragment, and the model confidently completed the
missing half from its own priors — producing an answer that sounded authoritative and was wrong.

Confident wrong answers are worse than "I don't know," because a user has no way to detect them.
That's the whole argument for the threshold in `RETRIEVAL_MIN_SCORE`.

## Authoring rules

1. **One concept per `##` heading.** A heading's content must stand alone when read cold — that
   is exactly how retrieval will present it.
2. **Never split a rule across headings.** If a rule has a condition and a consequence, they
   belong under one heading. This is the specific bug above.
3. **State the whole condition.** "Four or more day trades in five business days **and** more
   than 6% of total activity" — a chunk that drops the second clause is wrong, not merely terse.
4. **Close every doc with the not-financial-advice line.** It ends up in retrieved context, which
   is where it does the most good.

## Planned documents

- [x] Pattern day trader rule
- [ ] Order types (market, limit, stop, stop-limit)
- [ ] Time in force (day, GTC)
- [ ] Settlement and T+1
- [ ] Buying power and margin basics
- [ ] Fees and commissions
- [ ] Market hours and extended-hours trading
- [ ] Short selling basics
- [ ] Bid, ask, and spread
- [ ] Dividends and ex-dividend dates
- [ ] Account types
- [ ] Risk disclosures
- [ ] FAQ: common beginner questions

## Scope boundary

These docs explain **how trading works**. None of them recommend *what* to trade. If a document
would help answer "should I buy X?", it does not belong here — that question routes to
`out_of_scope` by design.
