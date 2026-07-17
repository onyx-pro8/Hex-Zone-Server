"""Wellness response tracking is limited to smart-home senders."""
import pytest

from app.domain.message_types import (
    CanonicalMessageType,
    enables_response_tracking,
    is_smart_home_sender_hid,
)


@pytest.mark.parametrize(
    ("hid", "expected"),
    [
        ("HOME-SENSOR-01", True),
        ("DEVICE-ABC123", True),
        ("MOB-ABCDEFGH", False),
        ("WEB-ABCDEFGH", False),
        ("", False),
        (None, False),
    ],
)
def test_is_smart_home_sender_hid(hid: str | None, expected: bool) -> None:
    assert is_smart_home_sender_hid(hid) is expected


def test_enables_response_tracking_only_for_smart_home_wellness() -> None:
    assert enables_response_tracking(
        CanonicalMessageType.WELLNESS_CHECK,
        sender_hid="HOME-01",
    )
    assert not enables_response_tracking(
        CanonicalMessageType.WELLNESS_CHECK,
        sender_hid="MOB-ABCDEFGH",
    )
    assert not enables_response_tracking(
        CanonicalMessageType.WELLNESS_CHECK,
        sender_hid="WEB-ABCDEFGH",
    )
    assert not enables_response_tracking(
        CanonicalMessageType.PANIC,
        sender_hid="HOME-01",
    )
