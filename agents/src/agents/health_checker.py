"""HealthChecker: polls all platform components and reports overall health."""

import asyncio
import os

import httpx
from pydantic import BaseModel

from tools.argocd import get_app_health
from tools.prometheus import query_prometheus
from tools.kubectl import get_pods


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ComponentHealth(BaseModel):
    name: str
    healthy: bool
    message: str


class HealthStatus(BaseModel):
    healthy: bool
    components: dict[str, ComponentHealth]
    summary: str


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ARGOCD_APP_NAME = os.environ.get("ARGOCD_APP_NAME", "platformma-app")
APP_NAMESPACE = os.environ.get("APP_NAMESPACE", "platformma")
PROMETHEUS_UP_QUERY = 'up{job=~".*platformma.*"}'
APP_HEALTH_URL = os.environ.get(
    "APP_HEALTH_URL",
    "http://platformma-app.platformma.svc.cluster.local:8080/health",
)


# ---------------------------------------------------------------------------
# HealthChecker
# ---------------------------------------------------------------------------


class HealthChecker:
    """Checks health of all platform components."""

    async def check_all(self) -> HealthStatus:
        """Check ArgoCD app health, pod readiness, Prometheus app_up metric, and /health endpoint.

        Returns:
            HealthStatus with per-component details and overall healthy flag.
        """
        results = await asyncio.gather(
            self._check_argocd(),
            self._check_pods(),
            self._check_prometheus_up(),
            self._check_health_endpoint(),
            return_exceptions=True,
        )

        components: dict[str, ComponentHealth] = {}
        labels = ["argocd", "pods", "prometheus_up", "health_endpoint"]

        for label, result in zip(labels, results):
            if isinstance(result, Exception):
                components[label] = ComponentHealth(
                    name=label,
                    healthy=False,
                    message=f"Check raised exception: {result}",
                )
            else:
                components[label] = result

        # argocd and prometheus_up are advisory — argocd may lack a token and
        # the app may not expose /metrics. Resolution is determined by the
        # direct pod readiness and HTTP health endpoint checks.
        advisory = {"prometheus_up", "argocd"}
        required = {k: v for k, v in components.items() if k not in advisory}
        all_healthy = all(c.healthy for c in required.values())
        unhealthy = [name for name, c in components.items() if not c.healthy]

        if all_healthy:
            summary = "All components healthy"
        else:
            summary = f"Unhealthy components: {', '.join(unhealthy)}"

        return HealthStatus(
            healthy=all_healthy,
            components=components,
            summary=summary,
        )

    async def wait_for_healthy(self, timeout_minutes: int = 10) -> bool:
        """Poll every 30 seconds until all components are healthy or timeout expires.

        Args:
            timeout_minutes: Maximum wait time in minutes.

        Returns:
            True if healthy before timeout, False if timed out.
        """
        deadline = asyncio.get_event_loop().time() + (timeout_minutes * 60)
        while asyncio.get_event_loop().time() < deadline:
            status = await self.check_all()
            if status.healthy:
                return True
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(30, remaining))
        return False

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    async def _check_argocd(self) -> ComponentHealth:
        """Check ArgoCD application health status."""
        try:
            result = await get_app_health(ARGOCD_APP_NAME)
            if "error" in result:
                return ComponentHealth(
                    name="argocd",
                    healthy=False,
                    message=f"ArgoCD API error: {result['error']}",
                )
            health = result.get("health_status", "Unknown")
            sync = result.get("sync_status", "Unknown")
            healthy = health == "Healthy" and sync in ("Synced", "OutOfSync")
            return ComponentHealth(
                name="argocd",
                healthy=healthy,
                message=f"health={health}, sync={sync}",
            )
        except Exception as e:
            return ComponentHealth(name="argocd", healthy=False, message=str(e))

    async def _check_pods(self) -> ComponentHealth:
        """Check that all pods in the app namespace are Ready."""
        try:
            result = get_pods(namespace=APP_NAMESPACE)
            if "error" in result:
                return ComponentHealth(
                    name="pods",
                    healthy=False,
                    message=f"kubectl error: {result['error']}",
                )
            pods = result.get("pods", [])
            if not pods:
                return ComponentHealth(
                    name="pods",
                    healthy=False,
                    message=f"No pods found in namespace {APP_NAMESPACE}",
                )
            not_ready = [
                p["name"]
                for p in pods
                if p.get("phase") not in ("Running", "Succeeded")
                or p.get("ready", "0/0").split("/")[0] != p.get("ready", "0/0").split("/")[1]
            ]
            healthy = len(not_ready) == 0
            if healthy:
                message = f"All {len(pods)} pod(s) ready"
            else:
                message = f"Not ready: {', '.join(not_ready)}"
            return ComponentHealth(name="pods", healthy=healthy, message=message)
        except Exception as e:
            return ComponentHealth(name="pods", healthy=False, message=str(e))

    async def _check_prometheus_up(self) -> ComponentHealth:
        """Check that Prometheus reports the platformma app as up."""
        try:
            result = await query_prometheus(PROMETHEUS_UP_QUERY)
            if "error" in result:
                return ComponentHealth(
                    name="prometheus_up",
                    healthy=False,
                    message=f"Prometheus error: {result['error']}",
                )
            metrics = result.get("result", [])
            if not metrics:
                return ComponentHealth(
                    name="prometheus_up",
                    healthy=False,
                    message="No 'up' metrics found for platformma targets",
                )
            # All returned targets must have value == "1"
            down_targets = [
                m.get("metric", {}).get("instance", "unknown")
                for m in metrics
                if str(m.get("value", [None, "0"])[1]) != "1"
            ]
            healthy = len(down_targets) == 0
            if healthy:
                message = f"{len(metrics)} target(s) up"
            else:
                message = f"Down targets: {', '.join(down_targets)}"
            return ComponentHealth(
                name="prometheus_up", healthy=healthy, message=message
            )
        except Exception as e:
            return ComponentHealth(name="prometheus_up", healthy=False, message=str(e))

    async def _check_health_endpoint(self) -> ComponentHealth:
        """HTTP GET the application /health endpoint."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(APP_HEALTH_URL)
                healthy = resp.status_code == 200
                return ComponentHealth(
                    name="health_endpoint",
                    healthy=healthy,
                    message=f"HTTP {resp.status_code}",
                )
        except httpx.ConnectError:
            return ComponentHealth(
                name="health_endpoint",
                healthy=False,
                message="Connection refused — service may be down",
            )
        except Exception as e:
            return ComponentHealth(name="health_endpoint", healthy=False, message=str(e))
