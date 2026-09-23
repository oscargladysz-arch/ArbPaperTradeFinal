from __future__ import annotations

from strategies.ref_mm.review.build_review_page import build


def test_build_page_structure() -> None:
    pairs = [
        {
            "pair_id": "K-1|9",
            "kalshi_ticker": "K-1",
            "poly_market_id": "9",
            "condition_id": "0xc",
            "reference_token_id": "111",
            "reference_outcome": "Yes",
            "poly_question": "Will <X>?",
            "kalshi_title": "X?",
            "poly_outcomes": '["Yes","No"]',
        }
    ]
    flagged = {"K-1|9": {"corr": -0.4, "median_abs_gap_cents": 3.2, "n_minutes": 100}}
    series = {
        "K-1|9": {
            "t": [1, 2],
            "mid": [50.0, 51.0],
            "fv": [49.0, 48.0],
            "labels": ["a", "b"],
            "t0": "a",
            "t1": "b",
        }
    }
    page = build(pairs, flagged, series)
    assert "rules-based reason" in page and "Will &lt;X&gt;?" in page
    assert "card flagged" in page and "Kalshi mid vs FV" in page
    assert "prefers-color-scheme: dark" in page and 'name="d-K-1|9"' in page
    plain = build(pairs, {}, {})
    assert "card flagged" not in plain
