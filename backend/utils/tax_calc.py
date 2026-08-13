"""
Verified Indian income-tax calculator (New Regime & Old Regime).

Same rationale as utils/market_data.py's fetch_index_quotes(): tax slabs,
rebate thresholds, and standard-deduction amounts are exactly the kind of
"specific number" the model's training data goes stale on (they change with
every Union Budget), and Tavily snippets on personal-finance blogs are
inconsistent about which FY they're describing. Rather than asking the LLM
to recall or compute slab arithmetic itself, we compute it here in plain
Python and hand the model verified numbers to narrate around — the report's
structure, framing, and choice of what to discuss are still entirely up to
the model; only the arithmetic is fixed.

Rules below reflect the New Regime slab restructuring from Union Budget 2025
(effective FY 2025-26 / AY 2026-27), which the Union Budget 2026 explicitly
left unchanged for FY 2026-27. THIS IS THE PART THAT WILL GO STALE NEXT — if
a future Budget revises slabs again, update RULES_BY_FY below (add a new
entry) rather than the callers; nothing else in this file or in report.py
should need to change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

CESS_RATE = 0.04


@dataclass(frozen=True)
class RegimeRules:
    label: str
    standard_deduction: int
    slabs: list[tuple[float, float, float]]  # (lower, upper[inf], rate)
    rebate_taxable_income_ceiling: Optional[int]  # taxable income <= this => zero tax
    rebate_max_amount: Optional[int]              # cap on the 87A rebate itself


# New Regime, FY 2026-27 (AY 2027-28) — Union Budget 2025 slabs, confirmed
# unchanged for FY26-27 by Union Budget 2026.
NEW_REGIME_FY27 = RegimeRules(
    label="New Regime (FY 2026-27)",
    standard_deduction=75_000,
    slabs=[
        (0, 400_000, 0.0),
        (400_000, 800_000, 0.05),
        (800_000, 1_200_000, 0.10),
        (1_200_000, 1_600_000, 0.15),
        (1_600_000, 2_000_000, 0.20),
        (2_000_000, 2_400_000, 0.25),
        (2_400_000, float("inf"), 0.30),
    ],
    rebate_taxable_income_ceiling=1_200_000,
    rebate_max_amount=60_000,
)

# Old Regime — slabs unchanged by Budget 2025/2026.
OLD_REGIME_FY27 = RegimeRules(
    label="Old Regime (FY 2026-27)",
    standard_deduction=50_000,
    slabs=[
        (0, 250_000, 0.0),
        (250_000, 500_000, 0.05),
        (500_000, 1_000_000, 0.20),
        (1_000_000, float("inf"), 0.30),
    ],
    rebate_taxable_income_ceiling=500_000,
    rebate_max_amount=12_500,
)


@dataclass
class TaxResult:
    regime_label: str
    gross_income: float
    other_deductions: float
    standard_deduction: float
    taxable_income: float
    base_tax: float
    rebate_applied: float
    cess: float
    total_tax: float
    slab_breakdown: list[str] = field(default_factory=list)


def _slab_tax(taxable_income: float, rules: RegimeRules) -> tuple[float, list[str]]:
    tax = 0.0
    lines: list[str] = []
    for lower, upper, rate in rules.slabs:
        if taxable_income <= lower:
            break
        band_top = min(taxable_income, upper)
        band_amount = max(0.0, band_top - lower)
        if band_amount <= 0:
            continue
        band_tax = band_amount * rate
        tax += band_tax
        if rate > 0:
            upper_label = "above" if upper == float("inf") else f"{upper:,.0f}"
            lines.append(f"{lower:,.0f}-{upper_label} @ {rate*100:.0f}% = {band_tax:,.0f}")
    return tax, lines


def compute_tax(gross_income: float, rules: RegimeRules, other_deductions: float = 0.0) -> TaxResult:
    """other_deductions = 80C/80D/24(b)/HRA etc., ignored entirely under the
    New Regime by design (only NPS employer contribution is allowed there,
    which this simplified calculator does not model)."""
    effective_other = other_deductions if rules is OLD_REGIME_FY27 else 0.0
    taxable_income = max(0.0, gross_income - rules.standard_deduction - effective_other)
    base_tax, lines = _slab_tax(taxable_income, rules)

    rebate = 0.0
    if rules.rebate_taxable_income_ceiling is not None and taxable_income <= rules.rebate_taxable_income_ceiling:
        rebate = min(base_tax, rules.rebate_max_amount or base_tax)

    tax_after_rebate = max(0.0, base_tax - rebate)
    cess = tax_after_rebate * CESS_RATE
    total = tax_after_rebate + cess

    return TaxResult(
        regime_label=rules.label,
        gross_income=gross_income,
        other_deductions=effective_other,
        standard_deduction=rules.standard_deduction,
        taxable_income=taxable_income,
        base_tax=base_tax,
        rebate_applied=rebate,
        cess=cess,
        total_tax=total,
        slab_breakdown=lines,
    )


def find_break_even_deduction(gross_income: float, target_new_regime_tax: float,
                               step: int = 1_000, max_deduction: int = 1_000_000) -> Optional[int]:
    """Smallest Old-Regime deduction (on top of the standard deduction) at
    which Old Regime tax drops to/below the New Regime's tax for the same
    gross income. Returns None if even the max_deduction cap isn't enough."""
    d = 0
    while d <= max_deduction:
        result = compute_tax(gross_income, OLD_REGIME_FY27, other_deductions=d)
        if result.total_tax <= target_new_regime_tax:
            return d
        d += step
    return None


_LAKH_RE = re.compile(
    r"(?:rs\.?|₹|inr)?\s*([\d,]+(?:\.\d+)?)\s*(lakh|lac|l\b|crore|cr\b)",
    re.IGNORECASE,
)


def extract_salary_figures(text: str) -> list[float]:
    """Pulls out any '₹15 lakh' / 'Rs.20L' / '12 lakh salary' style figures
    from a question/title as candidate gross-income amounts to compute for.
    Returns absolute rupee amounts, deduplicated, in the order first seen."""
    out: list[float] = []
    for match in _LAKH_RE.finditer(text):
        num_str, unit = match.group(1), match.group(2).lower()
        try:
            num = float(num_str.replace(",", ""))
        except ValueError:
            continue
        multiplier = 10_000_000 if unit.startswith("cr") else 100_000
        amount = num * multiplier
        if amount not in out:
            out.append(amount)
    return out


def format_tax_comparison_as_source(gross_incomes: list[float]) -> Optional[dict]:
    """Wraps computed New-vs-Old regime figures for one or more salary
    amounts as a synthetic, high-trust 'source' dict in the same shape as
    Tavily results — prepend to the sources list feeding the report prompt,
    same pattern as market_data.format_quotes_as_source."""
    if not gross_incomes:
        return None

    lines: list[str] = [
        f"Rules basis: New Regime = FY 2026-27 (post-Budget-2025 slabs, standard deduction "
        f"Rs.{NEW_REGIME_FY27.standard_deduction:,}, 87A rebate zeroes tax for taxable income up to "
        f"Rs.{NEW_REGIME_FY27.rebate_taxable_income_ceiling:,}). "
        f"Old Regime = unchanged slabs, standard deduction Rs.{OLD_REGIME_FY27.standard_deduction:,}, "
        f"87A rebate zeroes tax for taxable income up to Rs.{OLD_REGIME_FY27.rebate_taxable_income_ceiling:,}.",
        "New Regime slabs: 0-4L nil, 4-8L 5%, 8-12L 10%, 12-16L 15%, 16-20L 20%, 20-24L 25%, above 24L 30%.",
        "Old Regime slabs: 0-2.5L nil, 2.5-5L 5%, 5-10L 20%, above 10L 30%.",
    ]

    for gross in gross_incomes[:5]:  # cap — a handful of worked examples is plenty
        new_r = compute_tax(gross, NEW_REGIME_FY27)
        old_r_zero = compute_tax(gross, OLD_REGIME_FY27, other_deductions=0)
        break_even = find_break_even_deduction(gross, new_r.total_tax)

        lines.append(
            f"--- Gross salary Rs.{gross:,.0f} ---"
        )
        lines.append(
            f"New Regime: taxable income Rs.{new_r.taxable_income:,.0f} "
            f"(after Rs.{new_r.standard_deduction:,.0f} standard deduction), "
            f"base tax Rs.{new_r.base_tax:,.0f}, rebate Rs.{new_r.rebate_applied:,.0f}, "
            f"cess Rs.{new_r.cess:,.0f}, total tax Rs.{new_r.total_tax:,.0f}."
        )
        lines.append(
            f"Old Regime (no deductions beyond standard): taxable income Rs.{old_r_zero.taxable_income:,.0f}, "
            f"base tax Rs.{old_r_zero.base_tax:,.0f}, cess Rs.{old_r_zero.cess:,.0f}, "
            f"total tax Rs.{old_r_zero.total_tax:,.0f}."
        )
        lines.append(
            f"Tax saved by choosing New Regime with zero extra deductions: "
            f"Rs.{old_r_zero.total_tax - new_r.total_tax:,.0f}."
        )
        if break_even is not None:
            old_at_break_even = compute_tax(gross, OLD_REGIME_FY27, other_deductions=break_even)
            lines.append(
                f"Break-even: Old Regime deductions of Rs.{break_even:,.0f} (beyond the standard "
                f"deduction) bring Old Regime tax down to Rs.{old_at_break_even.total_tax:,.0f}, "
                f"matching or beating the New Regime's Rs.{new_r.total_tax:,.0f}. Below that "
                f"deduction level, New Regime is cheaper; above it, Old Regime is cheaper."
            )
        else:
            lines.append(
                "Break-even: not reached within a Rs.10,00,000 deduction cap — New Regime is "
                "cheaper at essentially any realistic deduction level for this income."
            )

    return {
        "title": (
            "VERIFIED TAX CALCULATION (authoritative — use these exact figures for any Old vs "
            "New Regime tax liability, slab, rebate, standard-deduction, or break-even number; "
            "do not compute your own slab arithmetic or use any other source for these numbers)"
        ),
        "url": "internal://verified-tax-calculation",
        "snippet": " | ".join(lines)[:2000],
        "fullContent": "\n".join(lines),
    }
