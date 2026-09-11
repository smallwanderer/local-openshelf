import pytest

from dotori_sync.debounce import DebouncedTrigger


def test_not_ready_before_any_activity():
    trigger = DebouncedTrigger(5.0)
    assert trigger.ready(100.0) is False
    assert trigger.pending is False


def test_not_ready_until_quiet_period_elapses():
    trigger = DebouncedTrigger(5.0)
    trigger.mark(10.0)
    assert trigger.ready(12.0) is False
    assert trigger.ready(14.9) is False
    assert trigger.ready(15.0) is True


def test_burst_of_activity_resets_the_quiet_window():
    trigger = DebouncedTrigger(5.0)
    trigger.mark(10.0)
    trigger.mark(13.0)  # another event arrives before the first window closes
    assert trigger.ready(15.0) is False  # still within 5s of the second mark
    assert trigger.ready(18.0) is True


def test_consume_clears_pending_until_marked_again():
    trigger = DebouncedTrigger(5.0)
    trigger.mark(10.0)
    assert trigger.ready(15.0) is True
    trigger.consume()
    assert trigger.pending is False
    assert trigger.ready(100.0) is False
    trigger.mark(101.0)
    assert trigger.ready(106.0) is True


def test_rejects_negative_quiet_seconds():
    with pytest.raises(ValueError):
        DebouncedTrigger(-1.0)
