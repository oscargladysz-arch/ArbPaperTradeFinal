from __future__ import annotations

from decimal import Decimal

from strategies.ref_mm.research.kat.kat1 import fee_per_contract_ticks


def test_fee_per_contract_matches_paper_example() -> None:
    # Paper: fee on 100 contracts at 50c rounds up so the rate is 3.54% of price, not 3.5%.
    f = fee_per_contract_ticks(
        5000, Decimal("0.07"), 100
    )  # 0.07*100*0.25 = 1.75 exactly -> 175 cents
    assert f == Decimal("175")  # 0.0175 dollars per contract = 175 ticks
    # 44c: 0.07 * 100 * 0.44 * 0.56 = 1.7248 -> 1.73 dollars -> 0.0173 per contract = 173 ticks
    assert fee_per_contract_ticks(4400, Decimal("0.07"), 100) == Decimal("173")
    # 5c: 0.07*100*0.05*0.95 = 0.3325 -> 0.34 dollars, the paper's "34c on $5" example
    assert fee_per_contract_ticks(500, Decimal("0.07"), 100) == Decimal("34")
