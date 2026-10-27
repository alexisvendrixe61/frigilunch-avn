from datetime import date

from create_plan import exclusive_horizon_end


def test_monday_delivery_gets_the_following_monday_as_exclusive_end():
    assert exclusive_horizon_end(date(2026, 10, 5)) == date(2026, 10, 12)


def test_friday_delivery_keeps_its_following_weekend_inside_the_horizon():
    assert exclusive_horizon_end(date(2026, 10, 2)) == date(2026, 10, 5)
