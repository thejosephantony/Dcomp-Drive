"""RF12: logs estruturados permitem auditoria automática."""
import json
import logging

from controller.logging_config import JsonEventFormatter


def test_structured_event_contains_timestamp_component_event_and_object_id():
    record = logging.LogRecord(
        "storage.store", logging.ERROR, __file__, 12,
        "scrub_corruption_detected node=storage-1 object_id=abc123",
        (), None,
    )
    data = json.loads(JsonEventFormatter().format(record))
    assert data["component"] == "storage.store"
    assert data["event"] == "scrub_corruption_detected"
    assert data["node"] == "storage-1"
    assert data["object_id"] == "abc123"
    assert data["timestamp"]
    assert data["level"] == "ERROR"
