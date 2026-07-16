from __future__ import annotations

import threading
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.export_service import export_contacts_csv, export_contacts_xlsx

router = APIRouter(prefix="/export", tags=["export"])

# Prevent concurrent heavy export builds from exhausting the single App Service worker
_export_lock = threading.Lock()


def _with_export_lock(build):
    if not _export_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="Another export is already in progress. Please wait and try again.",
        )
    try:
        return build()
    finally:
        _export_lock.release()


@router.get("/contacts.xlsx")
def export_xlsx(db: Session = Depends(get_db)):
    content = _with_export_lock(lambda: export_contacts_xlsx(db))
    filename = f"relationship-crm-contacts-{datetime.utcnow().strftime('%Y%m%d')}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/contacts.csv")
def export_csv(db: Session = Depends(get_db)):
    content = _with_export_lock(lambda: export_contacts_csv(db))
    filename = f"relationship-crm-contacts-{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
