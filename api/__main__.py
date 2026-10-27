"""python -m api: sobe o servidor com host e porta de JURIS_HOST e JURIS_PORT."""

import uvicorn

from api.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run("api.main:app", host=settings.host, port=settings.port, log_config=None, proxy_headers=True)
