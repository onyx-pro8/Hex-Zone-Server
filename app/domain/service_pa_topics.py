"""Topic taxonomy for PA and SERVICE marketplace-style messages."""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.message_types import CanonicalMessageType


@dataclass(frozen=True)
class SubtopicOption:
    id: str
    label: str


@dataclass(frozen=True)
class TopicOption:
    id: str
    label: str
    subtopics: tuple[SubtopicOption, ...] = ()


SERVICE_PA_TOPICS: tuple[TopicOption, ...] = (
    TopicOption("skilled_trades", "Skilled Trades"),
    TopicOption("childcare", "Childcare"),
    TopicOption("cleaning", "Cleaning"),
    TopicOption("beauty", "Beauty"),
    TopicOption("fitness", "Fitness"),
    TopicOption("tutors", "Tutors"),
    TopicOption(
        "products",
        "Products",
        (
            SubtopicOption("fruits", "Fruits"),
            SubtopicOption("vegetable", "Vegetable"),
            SubtopicOption("poultry_meat", "Poultry Meat"),
            SubtopicOption("seafood", "Seafood"),
            SubtopicOption("others", "Others"),
        ),
    ),
    TopicOption("health_lifestyle", "Health & Lifestyle"),
    TopicOption("entertainment", "Entertainment"),
    TopicOption("others", "Others"),
)

_TOPIC_BY_ID = {topic.id: topic for topic in SERVICE_PA_TOPICS}


def topic_label(topic_id: str) -> str | None:
    topic = _TOPIC_BY_ID.get(topic_id)
    return topic.label if topic else None


def subtopic_label(topic_id: str, subtopic_id: str) -> str | None:
    topic = _TOPIC_BY_ID.get(topic_id)
    if not topic:
        return None
    for sub in topic.subtopics:
        if sub.id == subtopic_id:
            return sub.label
    return None


def service_topic_requires_subtopic(topic_id: str) -> bool:
    topic = _TOPIC_BY_ID.get(topic_id)
    return bool(topic and topic.subtopics)


class ServicePaValidationError(ValueError):
    """PA / SERVICE subject, topic, or body failed validation."""


def validate_service_pa_message_fields(
    msg_type: CanonicalMessageType,
    msg: dict,
) -> None:
    """Require subject + topic (and Products subtopic for SERVICE)."""
    if msg_type not in (CanonicalMessageType.PA, CanonicalMessageType.SERVICE):
        return

    subject = str(msg.get("subject") or "").strip()
    topic = str(msg.get("topic") or "").strip()
    subtopic = str(msg.get("subtopic") or "").strip()

    if not subject:
        raise ServicePaValidationError("Subject is required for PA and SERVICE messages.")
    if len(subject) > 200:
        raise ServicePaValidationError("Subject must be 200 characters or fewer.")
    if msg_type == CanonicalMessageType.SERVICE:
        if not topic:
            raise ServicePaValidationError("Topic is required for SERVICE messages.")
        if topic not in _TOPIC_BY_ID:
            raise ServicePaValidationError("Invalid topic for SERVICE message.")
        if service_topic_requires_subtopic(topic):
            if not subtopic:
                raise ServicePaValidationError("Subtopic is required for SERVICE Products messages.")
            if subtopic_label(topic, subtopic) is None:
                raise ServicePaValidationError("Invalid subtopic for SERVICE Products message.")
    elif topic and topic not in _TOPIC_BY_ID:
        raise ServicePaValidationError("Invalid topic for PA message.")

    description = str(msg.get("description") or msg.get("text") or "").strip()
    if not description:
        raise ServicePaValidationError("Message body is required for PA and SERVICE messages.")


def display_text_for_service_pa(msg: dict) -> str:
    subject = str(msg.get("subject") or "").strip()
    description = str(msg.get("description") or msg.get("text") or "").strip()
    if subject and description and subject != description:
        return subject
    return subject or description
