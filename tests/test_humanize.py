from datetime import UTC, datetime, timedelta

from evomesh.humanize import humanize_timestamp


def test_humanize_timestamp_for_a_past_timestamp_says_ago():
    # One day before "now" should render as "<n> days ago".
    one_day_ago = datetime.now(tz=UTC) - timedelta(days=1)
    result = humanize_timestamp(one_day_ago.timestamp())
    assert result.endswith(" days ago")
