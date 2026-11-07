from app.receipts import verify

STDOUT = """Total overdue: 48213.55
Duplicate invoices: 7
Share disputed: 0.0412
Growth rate: 12.84
Monthly MRR: 1234567.0
"""


def test_exact_and_rounded_numbers_match():
    r = verify("**$48,213.55 is overdue** across 7 duplicates; growth 12.8%.", STDOUT)
    assert r["checked"] == 2 and r["matched"] == 2  # 7 is a small integer and is skipped


def test_percent_and_suffix_conversions():
    r = verify("Disputed share is 4.1% and MRR is $1.2M.", STDOUT)
    assert r["matched"] == r["checked"] == 2


def test_hallucinated_number_is_caught():
    r = verify("Revenue grew to $9,999,000.", STDOUT)
    assert r["unmatched"] == ["$9,999,000"]
    assert r["score"] == 0


def test_years_are_not_claims():
    assert verify("In 2026 things changed.", STDOUT)["checked"] == 0
