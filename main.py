"""服务入口。"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api import router
from app.logging_setup import configure_logging

configure_logging(logging.INFO)

app = FastAPI(title="Clinical Data Interop Ingestion", version="0.1.0")
app.include_router(router)


@app.get("/")
def read_root() -> dict[str, str]:
    return {"service": "clinical-data-interop", "status": "ok"}
