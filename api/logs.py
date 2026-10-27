"""Logs estruturados: uma linha JSON por evento, prontos para CloudWatch ou Loki."""

from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

_CAMPOS_PADRAO = set(vars(logging.makeLogRecord({})))


class FormatoJson(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        evento = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "nivel": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if request_id.get():
            evento["request_id"] = request_id.get()
        # Campos passados em extra={...} entram no JSON.
        evento.update({k: v for k, v in vars(record).items() if k not in _CAMPOS_PADRAO})
        if record.exc_info:
            evento["exc"] = self.formatException(record.exc_info)
        return json.dumps(evento, ensure_ascii=False, default=str)


def configurar_logs(nivel: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(FormatoJson())
    raiz = logging.getLogger()
    raiz.handlers[:] = [handler]
    raiz.setLevel(nivel.upper())
    # O log de acesso do uvicorn é substituído pelo middleware da API.
    logging.getLogger("uvicorn.access").disabled = True
    for nome in ("uvicorn", "uvicorn.error"):
        logging.getLogger(nome).handlers[:] = []
        logging.getLogger(nome).propagate = True
