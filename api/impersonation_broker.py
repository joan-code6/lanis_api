"""Private app: bind ONLY to a permission-restricted Unix socket, never TCP."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import Annotated

from fastapi import Depends, FastAPI, Response
from pydantic import BaseModel, Field

from .admin import AdminPrincipal, admin_dependency
from .impersonation import create, purge_expired
from .metrics import user_metrics_db


@asynccontextmanager
async def lifespan(_app):
    async def cleanup():
        while True:
            try:
                await purge_expired()
            except Exception:
                logging.getLogger(__name__).exception(
                    "Could not purge expired preview grants"
                )
            await asyncio.sleep(60)

    task = asyncio.create_task(cleanup())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


class PreviewRequest(BaseModel):
    user_id: str = Field(min_length=3, max_length=300)


@app.post("/launch")
async def launch(
    payload: PreviewRequest,
    response: Response,
    principal: Annotated[AdminPrincipal, Depends(admin_dependency)],
):
    # Audit must succeed before any usable grant is created.
    await user_metrics_db.record_admin_action(
        principal.user_id, "impersonation_start", payload.user_id
    )
    response.headers["Cache-Control"] = "no-store"
    return await create(principal.user_id, payload.user_id, principal.session_id)
