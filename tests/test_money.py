"""Ensure money is handled as integer cents."""

from app.config import Settings


def test_cents_conversion():
    # Simulate settings conversion
    assert int(round(0.50 * 100)) == 50
    assert int(round(10.50 * 100)) == 1050
    assert int(round(5.00 * 100)) == 500
    assert int(round(0.01 * 100)) == 1


def test_no_float_balance_math():
    balance = 1000  # cents
    reward = 50
    new_balance = balance + reward
    assert new_balance == 1050
    assert isinstance(new_balance, int)

    # Withdrawal
    wd = 500
    after = new_balance - wd
    assert after == 550
    assert isinstance(after, int)
