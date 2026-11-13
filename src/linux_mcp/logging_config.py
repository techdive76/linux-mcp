import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from uvicorn.logging import AccessFormatter

from .config import AppSettings

# Attributes every LogRecord has; anything else on a record came from `extra=` and is emitted as a field
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "color_message"}

# uvicorn's default access log format (uvicorn.config.LOGGING_CONFIG)
_UVICORN_ACCESS_FMT = '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger, message, plus any `extra=` fields.

    With plain_access=True, uvicorn's access records keep uvicorn's standard plain format.
    """

    def __init__(self, plain_access: bool = False):
        super().__init__()
        self._access = AccessFormatter(_UVICORN_ACCESS_FMT, use_colors=False) if plain_access else None

    def format(self, record: logging.LogRecord) -> str:
        if self._access and record.name == "uvicorn.access":
            return self._access.format(record)
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in _RESERVED and not k.startswith("_")})
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def setup_logging(settings: AppSettings) -> None:
    """Route all loggers (app, uvicorn, mcp, paramiko) through the JSON formatter."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if settings.log_file:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            settings.log_file,
            maxBytes=settings.log_file_max_bytes,
            backupCount=settings.log_file_backups,
            encoding="utf-8",
        ))
    plain_access = settings.access_log_format == "plain"
    formatter = JsonFormatter(plain_access=plain_access)
    for handler in handlers:
        handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(settings.log_level)

    # uvicorn installs its own plain-text handlers; send its records to the root logger instead
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    # Exactly one access log: uvicorn's plain one, or AuthMiddleware's JSON one (with client identity)
    logging.getLogger("uvicorn.access").disabled = not plain_access
    logging.getLogger("linux_mcp.access").disabled = plain_access


def output_fields(name: str, text: str, limit: int) -> dict:
    """Log fields for a command output stream, truncated to `limit` characters (0 = no limit)."""
    if limit and len(text) > limit:
        return {name: text[:limit], f"{name}_truncated": True, f"{name}_chars": len(text)}
    return {name: text}
