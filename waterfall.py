"""Provided by the finance team. Use this; don't reimplement it.

Why this exists
---------------
Venture investors usually hold *preferred* stock. Preferred stock has a
liquidation preference: in a sale, each preferred series is entitled to get
its money back (issue price x shares) before common gets anything. It can
instead choose to convert to common and take its pro-rata share, whichever
is worth more ("1x non-participating").

So the value of the fund's shares is NOT simply ownership % x company value.
When company value is high, everyone converts and it roughly is. When value is
low, preferences decide who gets paid, and the answer can be very different.

`allocate` splits a company's equity value across its share classes.
"""
from __future__ import annotations

from decimal import Decimal


def allocate(equity_value: Decimal, common_shares: int, series: list[dict]) -> dict[str, Decimal]:
    """Split `equity_value` across share classes.

    Args:
        equity_value: total value of the company's equity.
        common_shares: common shares outstanding, including the option pool.
        series: the preferred series outstanding at the valuation date. Each
            dict needs: name (str), shares (int), issue_price (Decimal or str),
            seniority (int; higher is paid first; equal is pari passu).

    Returns:
        {"Common": value, "<series name>": value, ...}. Values sum to equity_value.
    """
    equity_value = Decimal(equity_value)
    ss = [dict(s, issue_price=Decimal(str(s["issue_price"]))) for s in series]
    converted: set[str] = set()
    while True:
        payout = _allocate_fixed(equity_value, common_shares, ss, converted)
        for s in sorted(ss, key=lambda s: s["issue_price"]):
            if s["name"] in converted:
                continue
            trial = _allocate_fixed(equity_value, common_shares, ss, converted | {s["name"]})
            if trial[s["name"]] > payout[s["name"]]:
                converted.add(s["name"])
                break
        else:
            return payout


def _allocate_fixed(equity: Decimal, common: int, series: list[dict], converted: set[str]) -> dict[str, Decimal]:
    remaining = equity
    payout: dict[str, Decimal] = {}
    holders = [s for s in series if s["name"] not in converted]
    for tier in sorted({s["seniority"] for s in holders}, reverse=True):
        tier_series = [s for s in holders if s["seniority"] == tier]
        owed = sum(s["issue_price"] * s["shares"] for s in tier_series)
        paid = min(remaining, owed)
        for s in tier_series:
            payout[s["name"]] = paid * s["issue_price"] * s["shares"] / owed if owed else Decimal(0)
        remaining -= paid
    conv_shares = common + sum(s["shares"] for s in series if s["name"] in converted)
    per_share = remaining / conv_shares if conv_shares else Decimal(0)
    payout["Common"] = per_share * common
    for s in series:
        if s["name"] in converted:
            payout[s["name"]] = per_share * s["shares"]
    return payout
