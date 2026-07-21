"""Adversarial evaluation suite for the agent's safety claims.

`pytest` proves functions work. This suite proves the *claims in the DSN talk* hold against an
adversarial user: no path to execution except an explicit yes, no advice, no role escalation,
no prompt-injection override. Run it with `python scripts/run_evals.py`.
"""
