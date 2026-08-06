from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ... import __version__
from ...database import get_db
from ...schemas import HealthOut

router = APIRouter()


@router.get("/health", response_model=HealthOut)
def health(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> HealthOut:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="database unavailable") from None
    settings = request.app.state.settings
    return HealthOut(
        status="ok",
        database="ok",
        version=__version__,
        database_dialect=db.bind.dialect.name if db.bind else "unknown",
        tls_required=settings.db_ssl_required,
    )
