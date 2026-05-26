"""Prometheus query tools for the observability agents."""

import os
import time
from typing import Optional

import httpx

PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL",
    "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090",
)


async def query_prometheus(query: str) -> dict:
    """Execute a PromQL instant query and return {status, result}."""
    url = f"{PROMETHEUS_URL}/api/v1/query"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params={"query": query})
            resp.raise_for_status()
            data = resp.json()
            return {
                "status": data.get("status"),
                "result": data.get("data", {}).get("result", []),
                "resultType": data.get("data", {}).get("resultType"),
            }
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}


async def query_prometheus_range(
    query: str,
    start_minutes_ago: int,
    end_minutes_ago: int,
    step: str,
) -> dict:
    """Execute a PromQL range query.

    Args:
        query: PromQL expression.
        start_minutes_ago: How many minutes ago the range starts (e.g. 60).
        end_minutes_ago: How many minutes ago the range ends (e.g. 0 for now).
        step: Resolution step, e.g. "30s", "1m", "5m".

    Returns:
        {status, result, resultType} or {error}.
    """
    url = f"{PROMETHEUS_URL}/api/v1/query_range"
    now = time.time()
    start = now - (start_minutes_ago * 60)
    end = now - (end_minutes_ago * 60)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                url,
                params={
                    "query": query,
                    "start": start,
                    "end": end,
                    "step": step,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "status": data.get("status"),
                "result": data.get("data", {}).get("result", []),
                "resultType": data.get("data", {}).get("resultType"),
            }
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}


async def get_active_alerts() -> dict:
    """Return all currently firing alerts from Alertmanager-connected Prometheus.

    Returns:
        {alerts: [...], count: int} or {error}.
    """
    url = f"{PROMETHEUS_URL}/api/v1/alerts"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            alerts = data.get("data", {}).get("alerts", [])
            firing = [a for a in alerts if a.get("state") == "firing"]
            return {"alerts": firing, "count": len(firing)}
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}


async def get_metric_value(metric_name: str, labels: Optional[dict] = None) -> dict:
    """Convenience wrapper: query the current value of a metric with optional label selectors.

    Args:
        metric_name: Prometheus metric name, e.g. "kube_pod_container_status_restarts_total".
        labels: Dict of label key/value pairs to filter on, e.g. {"namespace": "platformma"}.

    Returns:
        {status, result} or {error}.
    """
    if labels:
        label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
        query = f"{metric_name}{{{label_str}}}"
    else:
        query = metric_name

    return await query_prometheus(query)
