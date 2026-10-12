"""Proceso independiente. No extrae dentro de la petición HTTP."""

from __future__ import annotations

import socket
import time

from docia_e.adapters.extraction.service import ocr_status
from docia_e.application.extractions import execute_job
from docia_e.application.support import new_id
from docia_e.domain.timeutil import now_utc_z


def run_worker(gateway, blobs, settings, clock=now_utc_z) -> None:
    worker_id = f"{socket.gethostname()}-{new_id()}"
    while True:
        ocr = ocr_status()
        gateway.store.heartbeat(
            {
                "worker_id": worker_id,
                "seen_at": clock(),
                "ocr_available": ocr["available"],
                "ocr_languages": ocr.get("languages") or [],
            }
        )
        if gateway.store.maintenance_enabled():
            time.sleep(2)
            continue
        job = gateway.store.claim_job(worker_id, clock(), _until(clock))
        if not job:
            time.sleep(1)
            continue
        execute_job(gateway, blobs, settings, worker_id, job, clock, time.sleep)


def _until(clock) -> str:
    from datetime import datetime, timedelta

    moment = datetime.fromisoformat(clock().replace("Z", "+00:00"))
    return (moment + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
