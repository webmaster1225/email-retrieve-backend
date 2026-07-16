from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models.sync import SyncRun
from app.schemas import SyncRunOut
from app.services.graph_client import GraphAuthError, GraphClient
from app.services.sync_service import SyncService, run_sync_in_background

router = APIRouter(prefix="/sync", tags=["sync"])


def _db_factory():
    return SessionLocal()


def _run_reprocess() -> None:
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent.parent / "scripts" / "reprocess_contacts.py"
    subprocess.run([sys.executable, str(script)], check=True)


@router.post("/reprocess-contacts")
def reprocess_contacts(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_reprocess)
    return {"status": "started", "message": "Re-extracting contacts from imported messages"}


@router.post("/start-inbox", response_model=SyncRunOut)
def start_inbox_sync(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    client = GraphClient(db)
    try:
        client.ensure_access_token()
    except GraphAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    service = SyncService(db)
    active = service.get_active_run()
    if active:
        return active

    sync_run = service.start_inbox_sync()
    background_tasks.add_task(run_sync_in_background, _db_factory, sync_run.id)
    return sync_run


@router.post("/start", response_model=SyncRunOut)
def start_sync(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    client = GraphClient(db)
    try:
        client.ensure_access_token()
    except GraphAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    service = SyncService(db)
    active = service.get_active_run()
    if active:
        return active

    sync_run = service.start_full_sync()
    background_tasks.add_task(run_sync_in_background, _db_factory, sync_run.id)
    return sync_run


@router.post("/fail-running", response_model=list[SyncRunOut])
def fail_running_syncs(db: Session = Depends(get_db)):
    """Clear zombie sync rows stuck at status=running so a new sync can start."""
    service = SyncService(db)
    return service.fail_running_syncs()


@router.get("/status", response_model=SyncRunOut | None)
def latest_sync_status(db: Session = Depends(get_db)):
    # Auto-fail hung runs so the UI doesn't show "syncing" forever
    SyncService(db).get_active_run()
    run = db.query(SyncRun).order_by(SyncRun.started_at.desc()).first()
    return run


@router.get("/runs", response_model=list[SyncRunOut])
def list_sync_runs(db: Session = Depends(get_db)):
    runs = db.query(SyncRun).order_by(SyncRun.started_at.desc()).limit(20).all()
    return runs
