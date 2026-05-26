"""ArgoCD API tools for the observability agents."""

import os

import httpx

ARGOCD_URL = os.environ.get(
    "ARGOCD_URL",
    "http://argocd-server.argocd.svc.cluster.local:80",
)
ARGOCD_TOKEN = os.environ.get("ARGOCD_TOKEN", "")


def _headers() -> dict:
    if ARGOCD_TOKEN:
        return {"Authorization": f"Bearer {ARGOCD_TOKEN}"}
    return {}


async def get_app_health(app_name: str) -> dict:
    """Return health status, sync status, and message for an ArgoCD application.

    Args:
        app_name: ArgoCD application name, e.g. "platformma-app".

    Returns:
        {health_status, sync_status, message, conditions} or {error}.
    """
    url = f"{ARGOCD_URL}/api/v1/applications/{app_name}"
    try:
        async with httpx.AsyncClient(timeout=30.0, verify=False, follow_redirects=True) as client:
            resp = await client.get(url, headers=_headers())
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", {})
            health = status.get("health", {})
            sync = status.get("sync", {})
            conditions = status.get("conditions", [])
            return {
                "health_status": health.get("status"),
                "health_message": health.get("message"),
                "sync_status": sync.get("status"),
                "revision": sync.get("revision"),
                "conditions": conditions,
                "operation_state": status.get("operationState", {}).get("phase"),
                "operation_message": status.get("operationState", {}).get("message"),
            }
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}


async def get_app_events(app_name: str) -> dict:
    """Return recent Kubernetes events for an ArgoCD application.

    Args:
        app_name: ArgoCD application name.

    Returns:
        {events: [...]} or {error}.
    """
    url = f"{ARGOCD_URL}/api/v1/applications/{app_name}/events"
    try:
        async with httpx.AsyncClient(timeout=30.0, verify=False, follow_redirects=True) as client:
            resp = await client.get(url, headers=_headers())
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            events = [
                {
                    "reason": e.get("reason"),
                    "message": e.get("message"),
                    "type": e.get("type"),
                    "count": e.get("count"),
                    "first_time": e.get("firstTimestamp"),
                    "last_time": e.get("lastTimestamp"),
                    "involved_object": e.get("involvedObject", {}).get("name"),
                }
                for e in items
            ]
            return {"events": events, "count": len(events)}
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}


async def list_apps() -> dict:
    """Return all ArgoCD applications with their health and sync status.

    Returns:
        {apps: [{name, health, sync, namespace}]} or {error}.
    """
    url = f"{ARGOCD_URL}/api/v1/applications"
    try:
        async with httpx.AsyncClient(timeout=30.0, verify=False, follow_redirects=True) as client:
            resp = await client.get(url, headers=_headers())
            resp.raise_for_status()
            data = resp.json()
            apps = []
            for item in data.get("items", []):
                status = item.get("status", {})
                apps.append(
                    {
                        "name": item.get("metadata", {}).get("name"),
                        "namespace": item.get("spec", {}).get("destination", {}).get("namespace"),
                        "health_status": status.get("health", {}).get("status"),
                        "sync_status": status.get("sync", {}).get("status"),
                        "repo": item.get("spec", {}).get("source", {}).get("repoURL"),
                        "path": item.get("spec", {}).get("source", {}).get("path"),
                    }
                )
            return {"apps": apps, "count": len(apps)}
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}


async def get_app_pods(app_name: str, namespace: str) -> dict:
    """Return pods managed by an ArgoCD application.

    Args:
        app_name: ArgoCD application name.
        namespace: Kubernetes namespace where the app runs.

    Returns:
        {pods: [{name, status, ready, restarts, node}]} or {error}.
    """
    url = f"{ARGOCD_URL}/api/v1/applications/{app_name}/pods"
    try:
        async with httpx.AsyncClient(timeout=30.0, verify=False, follow_redirects=True) as client:
            resp = await client.get(url, headers=_headers(), params={"namespace": namespace})
            resp.raise_for_status()
            data = resp.json()
            pods = []
            for item in data.get("items", []):
                metadata = item.get("metadata", {})
                spec = item.get("spec", {})
                status = item.get("status", {})
                containers = status.get("containerStatuses", [])
                total_restarts = sum(c.get("restartCount", 0) for c in containers)
                ready_count = sum(1 for c in containers if c.get("ready", False))
                pods.append(
                    {
                        "name": metadata.get("name"),
                        "namespace": metadata.get("namespace"),
                        "phase": status.get("phase"),
                        "ready": f"{ready_count}/{len(containers)}",
                        "restarts": total_restarts,
                        "node": spec.get("nodeName"),
                        "conditions": [
                            {"type": c.get("type"), "status": c.get("status")}
                            for c in status.get("conditions", [])
                        ],
                    }
                )
            return {"pods": pods, "count": len(pods)}
    except httpx.HTTPStatusError as e:
        # Fall back: pods endpoint may not exist in all ArgoCD versions
        return {"error": f"HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}
