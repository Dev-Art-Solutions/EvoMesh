from datetime import UTC, datetime, timedelta

from evomesh.humanize import humanize_bytes, humanize_timestamp


def test_humanize_timestamp_for_a_past_timestamp_says_ago():
    # One day before "now" should render as "<n> days ago".
    one_day_ago = datetime.now(tz=UTC) - timedelta(days=1)
    result = humanize_timestamp(one_day_ago.timestamp())
    assert result.endswith(" days ago")


def test_humanize_bytes_1024_renders_as_one_kib():
    # One KiB is the smallest non-byte value, rendered with one decimal place.
    result = humanize_bytes(1024)
    assert result == "1.0 KiB"
