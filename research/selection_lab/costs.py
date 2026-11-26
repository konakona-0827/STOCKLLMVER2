"""Separate measured CLI usage from hypothetical API-token budgets."""
from __future__ import annotations

from pathlib import Path

from .data import read_json, write_json


def api_estimate(input_tokens, output_tokens, cached_tokens=0, model="gpt-6-sol"):
    rates = {"gpt-6-sol": (2, .2, 10), "gpt-6-luna": (.1, .01, .5)}
    if model not in rates or min(input_tokens, output_tokens, cached_tokens) < 0 or cached_tokens > input_tokens:
        raise ValueError("INVALID_TOKEN_BUDGET_OR_MODEL")
    incoming, cached, outgoing = rates[model]
    return ((input_tokens - cached_tokens) * incoming + cached_tokens * cached + output_tokens * outgoing) / 1_000_000


def cost_report(pilot, output):
    folder = Path(pilot)
    summary = read_json(folder / "summary.json")
    cached = sum(read_json(p).get("usage", {}).get("input_tokens_details", {}).get("cached_tokens", 0)
                 for p in folder.glob("*.json"))
    total_in, total_out = summary["total_input_tokens"], summary["total_output_tokens"]
    result = dict(as_of="2026-11-11", currency="USD unless provider price is explicitly TWD",
                  measured_cli=dict(calls=summary["attempted_calls"], input_tokens=total_in,
                                    output_tokens=total_out, cached_input_tokens=cached,
                                    elapsed_seconds=summary["elapsed_seconds"], api_charge_usd=0,
                                    subscription_or_credits_usage=True, quota_percentage_unknown=True,
                                    extra_credit_charge_not_observed=True),
                  api_equivalent_not_a_bill={},
                  monthly_scenarios={},
                  excluded_costs=["existing_subscription", "extra_Codex_credits_if_any", "electricity", "broker_data", "actual_trading_fees", "web_search_tools"],
                  data_options_twd=dict(free=0, finmind_backer_month=699, finmind_backer_year=5499,
                                        finmind_sponsor_month=999, finlab_vip_month=888,
                                        finlab_vip_minimum_months=12, finlab_vip_minimum_total=10656),
                  sources={"api_sol": "https://developers.openai.com/api/docs/models/gpt-6-sol",
                           "api_luna": "https://developers.openai.com/api/docs/models/gpt-6-luna",
                           "codex": "https://learn.chatgpt.com/docs/pricing",
                           "finmind": "https://finmind.github.io/Pricing/",
                           "finlab": "https://finlab.finance/pricing"},
                  note="CLI tokens include transport/system overhead; API-equivalent budgets are approximations, not identical API workloads. Luna quality was not tested.")
    for model in ("gpt-6-sol", "gpt-6-luna"):
        uncached = api_estimate(total_in, total_out, model=model)
        result["api_equivalent_not_a_bill"][model] = dict(uncached_usd=uncached,
            same_cached_fraction_usd=api_estimate(total_in, total_out, cached, model))
        result["monthly_scenarios"][model] = dict(one_4_call_pilot_per_day_22_days_usd=uncached * 22,
                                                     seven_4_call_pilots_per_day_22_days_usd=uncached * 22 * 7)
    write_json(output, result)
    return result
