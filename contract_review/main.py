from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import get_settings
from .db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_settings().ensure_dirs(); init_db()
    yield


app = FastAPI(title=get_settings().app_name, version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")


def run() -> None:
    uvicorn.run("contract_review.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    run()
