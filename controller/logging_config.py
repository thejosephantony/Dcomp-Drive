"""RF12: formatação JSON padronizada de eventos para controlador e storages."""
from datetime import datetime, timezone
import json
import logging
import os
import re
from collections import deque
from threading import Lock

_ATTRIBUTES = re.compile(r"(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)=(?P<value>\S+)")


_PUBLIC_EVENTS: deque[dict] = deque(maxlen=150)
_PUBLIC_EVENTS_LOCK = Lock()
_PUBLIC_EVENT_NAMES = re.compile(
    r"^(node_|registered|repair_|file_|demo_|scrub_|upload_|download_|heartbeat_|"
    r"storage_|replica_|write_|reconcile|corruption_)"
)


class DemoEventHandler(logging.Handler):
    """Eventos mínimos estruturados; jamais expõe mensagens completas ou segredos."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not record.name.startswith("controller."):
                return
            message = record.getMessage()
            event = message.split(maxsplit=1)[0] if message else ""
            if not _PUBLIC_EVENT_NAMES.match(event):
                return
            attrs = {m.group("key"): m.group("value")
                     for m in _ATTRIBUTES.finditer(message)}
            item = {
                "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "component": record.name,
                "level": record.levelname,
                "event": event[:80],
            }
            for name in ("node", "version", "object_id", "target", "from", "to", "code"):
                if name in attrs:
                    item[name] = attrs[name][:128]
            with _PUBLIC_EVENTS_LOCK:
                _PUBLIC_EVENTS.append(item)
        except Exception:
            self.handleError(record)


def public_events_snapshot(limit: int = 50) -> list[dict]:
    with _PUBLIC_EVENTS_LOCK:
        return list(_PUBLIC_EVENTS)[-max(1, min(limit, 100)):][::-1]


class JsonEventFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        fields = {m.group("key"): m.group("value")
                  for m in _ATTRIBUTES.finditer(message)}
        event = message.split(maxsplit=1)[0] if message.strip() else "log"
        result = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "component": record.name,
            "level": record.levelname,
            "event": event,
            "message": message,
        }
        result.update(fields)
        if record.exc_info:
            result["exception"] = self.formatException(record.exc_info)
        return json.dumps(result, ensure_ascii=False)


def configure_logging() -> None:
    if os.getenv("LOG_FORMAT", "json").lower() != "json":
        logging.basicConfig(level=logging.INFO)
        return
    formatter = JsonEventFormatter()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(handler, DemoEventHandler) for handler in root.handlers):
        root.addHandler(DemoEventHandler())
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for logger in (root, logging.getLogger("uvicorn"), logging.getLogger("uvicorn.access"),
                   logging.getLogger("uvicorn.error")):
        for handler in logger.handlers:
            handler.setFormatter(formatter)
