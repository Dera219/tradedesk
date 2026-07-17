# Account Types

## Cash account

In a cash account you trade only with money you have deposited and that has settled. You cannot
borrow, so you cannot be forced into a margin call, and your maximum loss on any position is what
you put in. The main constraint is settlement: you must wait for sale proceeds to settle (T+1)
before reusing them, or risk a good-faith violation.

A cash account is the simpler, lower-risk default, and it is what a paper-trading learner should
picture.

## Margin account

A margin account lets you borrow from the broker against your holdings, increasing buying power
and enabling short selling. It also exposes you to margin calls and forced liquidation. Margin
accounts are subject to the pattern day trader rule and its $25,000 minimum if you trade
frequently.

The extra capability comes with the extra risk that the broker can sell your positions to protect
its loan.

## Retirement accounts

Tax-advantaged retirement accounts (such as an IRA) have their own rules: contribution limits,
restrictions on margin and short selling, and penalties for early withdrawal. They are built for
long-term holding, not active trading.

## Which one this assistant simulates

This assistant simulates a straightforward cash-style brokerage account with paper money. There is
no real margin, no real borrowing, and no real settlement delay — every fill is instant and
simulated, so you can practice placing and reasoning about orders without financial risk.

---

*This document is educational material for a simulated brokerage. It is not financial advice.*
