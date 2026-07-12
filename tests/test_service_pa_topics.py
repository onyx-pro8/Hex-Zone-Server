"""PA / SERVICE topic and subject validation."""
import pytest

from app.domain.message_types import CanonicalMessageType
from app.domain.service_pa_topics import (
    ServicePaValidationError,
    display_text_for_service_pa,
    validate_service_pa_message_fields,
)


def test_validate_service_requires_subject_topic_and_body():
    with pytest.raises(ServicePaValidationError, match="Subject is required"):
        validate_service_pa_message_fields(
            CanonicalMessageType.SERVICE,
            {"topic": "cleaning", "description": "Need help"},
        )

    with pytest.raises(ServicePaValidationError, match="Topic is required"):
        validate_service_pa_message_fields(
            CanonicalMessageType.SERVICE,
            {"subject": "Help", "description": "Need help"},
        )

    with pytest.raises(ServicePaValidationError, match="Subtopic is required"):
        validate_service_pa_message_fields(
            CanonicalMessageType.SERVICE,
            {
                "subject": "Fresh fruit",
                "topic": "products",
                "description": "Apples for sale",
            },
        )


def test_validate_service_products_subtopic_ok():
    validate_service_pa_message_fields(
        CanonicalMessageType.SERVICE,
        {
            "subject": "Fresh fruit",
            "topic": "products",
            "subtopic": "fruits",
            "description": "Apples for sale",
        },
    )


def test_validate_pa_requires_subject_but_not_topic():
    validate_service_pa_message_fields(
        CanonicalMessageType.PA,
        {
            "subject": "Garage sale",
            "description": "Saturday morning",
        },
    )


def test_validate_pa_accepts_optional_topic():
    validate_service_pa_message_fields(
        CanonicalMessageType.PA,
        {
            "subject": "Garage sale",
            "topic": "products",
            "description": "Saturday morning",
        },
    )


def test_display_text_prefers_subject():
    assert display_text_for_service_pa(
        {"subject": "Garage sale", "description": "Saturday morning"}
    ) == "Garage sale"
