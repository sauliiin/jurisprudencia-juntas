"""python -m api: sobe o servidor com JURIS_HOST, JURIS_PORT e JURIS_WORKERS."""

import uvicorn

from api.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "api.main:app",
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
        log_config=None,
        proxy_headers=True,
    )
