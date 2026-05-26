"""FastAPI entrypoint for PlatforMa observability agents."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from orchestrator import Orchestrator

logger = logging.getLogger(__name__)

_orchestrator: Orchestrator | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator
    _orchestrator = Orchestrator()
    # Recover any incidents that were interrupted by a previous pod restart
    await _orchestrator.startup_recovery()
    # Start GitHub Issue comment polling in the background
    task = asyncio.create_task(_orchestrator.poll_issue_comments())
    yield
    task.cancel()
    _orchestrator = None


app = FastAPI(
    title="PlatforMa Observability Agents",
    description="Automated incident detection, diagnosis, and remediation.",
    version="0.1.0",
    lifespan=lifespan,
)


def get_orchestrator() -> Orchestrator:
    if _orchestrator is None:
        raise RuntimeError("Orchestrator not initialised — server is starting up")
    return _orchestrator


@app.get("/health")
async def health():
    return {"status": "alive"}


@app.post("/webhook/alertmanager")
async def alertmanager_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception as e:
        logger.error("Failed to parse AlertManager payload: %s", e)
        return JSONResponse(status_code=400, content={"error": "Invalid JSON payload"})

    firing = [a for a in payload.get("alerts", []) if a.get("status") == "firing"]
    if firing:
        trigger = {
            "source": "alertmanager",
            "alerts": firing,
            "timestamp": payload.get("commonLabels"),
            "group_labels": payload.get("groupLabels"),
            "common_annotations": payload.get("commonAnnotations"),
        }
        background_tasks.add_task(get_orchestrator().handle_trigger, trigger)
        logger.info("AlertManager webhook: %d firing alert(s) queued", len(firing))
        return {"status": "accepted", "firing_alerts": len(firing)}

    return {"status": "accepted", "firing_alerts": 0}


@app.post("/webhook/argocd")
async def argocd_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception as e:
        logger.error("Failed to parse ArgoCD payload: %s", e)
        return JSONResponse(status_code=400, content={"error": "Invalid JSON payload"})

    app_obj = payload.get("application", {})
    health_status = app_obj.get("status", {}).get("health", {}).get("status", "")

    if health_status in ("Degraded", "Missing"):
        trigger = {
            "source": "argocd",
            "app": app_obj,
            "health_status": health_status,
            "timestamp": None,
        }
        background_tasks.add_task(get_orchestrator().handle_trigger, trigger)
        logger.info("ArgoCD webhook: app health=%s — trigger queued", health_status)
        return {"status": "accepted", "health_status": health_status}

    return {"status": "accepted", "health_status": health_status}


@app.get("/check")
async def manual_check(background_tasks: BackgroundTasks):
    background_tasks.add_task(get_orchestrator().run_periodic_check)
    return {"status": "check started"}


@app.get("/status")
async def status():
    """Return current health status of all platform components."""
    s = await get_orchestrator()._health_checker.check_all()
    return s.dict()
