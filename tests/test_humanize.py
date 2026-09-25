from datetime import UTC, datetime, timedelta

from evomesh.humanize import humanize_bytes, humanize_duration, humanize_size, humanize_timestamp


def test_humanize_timestamp_for_a_past_timestamp_says_ago():
    one_day_ago = datetime.now(tz=UTC) - timedelta(days=1)
    result = humanize_timestamp(one_day_ago.timestamp())
    assert result == "1d ago"


def test_humanize_bytes_1024_renders_as_one_kib():
    # One KiB is the smallest non-byte value, rendered with one decimal place.
    result = humanize_bytes(1024)
    assert result == "1.0 KiB"


def test_humanize_duration_sub_second_renders_as_milliseconds():
    # A sub-second duration renders in whole milliseconds, not fractional seconds.
    result = humanize_duration(0.5)
    assert result == "500 ms"


def test_humanize_size_sub_1024_renders_as_whole_bytes():
    # Below one KiB the value stays in bytes and renders as a whole number, not a decimal.
    result = humanize_size(512)
    assert result == "512 B"


def test_humanize_duration_uses_real_unit_lengths():
    # Every divisor used to be one unit too small: 238 s was "0.0 days" and two
    # weeks "14w" -- harness_session shows these for every job's elapsed time.
    assert humanize_duration(59) == "59s"
    assert humanize_duration(238.5) == "3m 58s"
    assert humanize_duration(3600) == "1h"
    assert humanize_duration(90061) == "1d 1h 1m 1s"
    assert humanize_duration(14 * 86400) == "2w"
