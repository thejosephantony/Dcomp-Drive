"""RF12: formatação JSON padronizada de eventos para controlador e storages."""
from datetime import datetime, timezone
import json
import logging
import os
import re

_ATTRIBUTES = re.compile(r"(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)=(?P<value>\S+)")


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
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for logger in (root, logging.getLogger("uvicorn"), logging.getLogger("uvicorn.access"),
                   logging.getLogger("uvicorn.error")):
        for handler in logger.handlers:
            handler.setFormatter(formatter)
