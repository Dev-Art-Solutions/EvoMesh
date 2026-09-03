"""The 5-field cron matcher: no dependency, so it earns its own coverage."""

from datetime import UTC, datetime

import pytest

from evomesh.cron import InvalidCronError, next_after, parse


def test_every_hour_on_the_hour() -> None:
    now = datetime(2026, 9, 3, 14, 20, tzinfo=UTC)
    assert next_after("0 * * * *", now) == datetime(2026, 9, 3, 15, 0, tzinfo=UTC)


def test_daily_at_a_fixed_time_rolls_to_tomorrow_once_todays_has_passed() -> None:
    now = datetime(2026, 9, 3, 14, 20, tzinfo=UTC)
    assert next_after("30 9 * * *", now) == datetime(2026, 9, 4, 9, 30, tzinfo=UTC)


def test_daily_at_a_fixed_time_still_fires_today_if_not_passed_yet() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    assert next_after("30 9 * * *", now) == datetime(2026, 9, 3, 9, 30, tzinfo=UTC)


def test_a_step_expression_finds_the_next_quarter_hour() -> None:
    now = datetime(2026, 9, 3, 14, 20, tzinfo=UTC)
    assert next_after("*/15 * * * *", now) == datetime(2026, 9, 3, 14, 30, tzinfo=UTC)


def test_a_weekday_field_finds_the_next_matching_weekday() -> None:
    # 2026-09-03 is a Thursday; the next Monday is four days out.
    now = datetime(2026, 9, 3, 14, 20, tzinfo=UTC)
    assert next_after("0 9 * * 1", now) == datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


def test_sunday_can_be_written_as_0_or_7() -> None:
    now = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    assert next_after("0 0 * * 0", now) == next_after("0 0 * * 7", now)


def test_day_of_month_and_weekday_are_ored_not_anded_when_both_are_restricted() -> None:
    """The standard, surprising cron rule: with both fields restricted, a day
    matching either one is enough -- the closer of the two wins."""
    # Thursday 2026-09-03: the 1st of the month is weeks away, but Friday
    # (weekday 5) is tomorrow, so the OR combination should pick Friday.
    now = datetime(2026, 9, 3, 14, 20, tzinfo=UTC)
    assert next_after("0 0 1 * 5", now) == datetime(2026, 9, 4, 0, 0, tzinfo=UTC)


def test_a_wildcard_field_drops_out_of_the_or_combination() -> None:
    """With day-of-week wild, only day-of-month constrains -- no OR effect."""
    now = datetime(2026, 9, 3, 14, 20, tzinfo=UTC)
    assert next_after("0 0 10 * *", now) == datetime(2026, 9, 10, 0, 0, tzinfo=UTC)


def test_rejects_anything_other_than_five_fields() -> None:
    with pytest.raises(InvalidCronError):
        parse("* * * *")


def test_rejects_a_field_out_of_range() -> None:
    with pytest.raises(InvalidCronError):
        parse("60 * * * *")


def test_rejects_a_malformed_field() -> None:
    with pytest.raises(InvalidCronError):
        parse("abc * * * *")
