# Order Types

## Market order

A market order buys or sells immediately at the best price currently available. You are telling
the broker "fill this now, whatever the price." It almost always executes, but the price is not
guaranteed — in a fast-moving or thinly traded stock, the fill can be worse than the price you
saw when you placed it.

Use a market order when getting filled matters more than the exact price.

## Limit order

A limit order sets the worst price you will accept. A buy limit fills only at your limit price or
lower; a sell limit fills only at your limit price or higher. You control the price, but the order
may never fill if the market does not reach your limit.

Use a limit order when the price matters more than certainty of getting filled.

## Stop order

A stop order is dormant until the price crosses a trigger you set, at which point it becomes a
market order. A sell stop below the current price is the common "stop-loss" — it turns into a
market sell once the stock falls to your trigger, capping further loss. Because it becomes a
market order, the fill price can be worse than the trigger in a fast drop.

## Stop-limit order

A stop-limit order combines the two: when the stop trigger is hit, it becomes a *limit* order
rather than a market order. This protects you from a terrible fill, but it introduces the risk
that the price gaps past your limit and the order never fills — leaving you still holding a
position you meant to exit.

The trade-off is exact: a stop order guarantees execution but not price; a stop-limit guarantees
price but not execution.

---

*This document is educational material for a simulated brokerage. It is not financial advice.*
