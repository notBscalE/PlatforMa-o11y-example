"""Kubernetes kubectl tools using the official Python client."""

import datetime
from typing import Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException

# ---------------------------------------------------------------------------
# Client initialisation — lazy so importing this module doesn't require a
# reachable cluster (important for tests and local development).
# ---------------------------------------------------------------------------

_core: client.CoreV1Api | None = None
_apps: client.AppsV1Api | None = None


def _clients() -> tuple[client.CoreV1Api, client.AppsV1Api]:
    """Return (core, apps) API clients, initialising them on first call."""
    global _core, _apps
    if _core is None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        _core = client.CoreV1Api()
        _apps = client.AppsV1Api()
    return _core, _apps


# ---------------------------------------------------------------------------
# Pod operations
# ---------------------------------------------------------------------------


def get_pods(namespace: Optional[str] = None) -> dict:
    """List pods with their status and restart counts.

    Args:
        namespace: Namespace to query; None means all namespaces.

    Returns:
        {pods: [{name, namespace, phase, ready, restarts, node, age}]} or {error}.
    """
    try:
        core, _ = _clients()
        if namespace:
            pod_list = core.list_namespaced_pod(namespace=namespace)
        else:
            pod_list = core.list_pod_for_all_namespaces()

        pods = []
        for pod in pod_list.items:
            containers = pod.status.container_statuses or []
            total_restarts = sum(c.restart_count for c in containers)
            ready_count = sum(1 for c in containers if c.ready)
            pods.append(
                {
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "phase": pod.status.phase,
                    "ready": f"{ready_count}/{len(containers)}",
                    "restarts": total_restarts,
                    "node": pod.spec.node_name,
                    "creation_timestamp": str(pod.metadata.creation_timestamp),
                    "conditions": [
                        {"type": c.type, "status": c.status}
                        for c in (pod.status.conditions or [])
                    ],
                }
            )
        return {"pods": pods, "count": len(pods)}
    except ApiException as e:
        return {"error": f"ApiException {e.status}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def get_pod_logs(namespace: str, pod_name: str, tail_lines: int = 100) -> dict:
    """Retrieve recent logs from a pod's first container.

    Args:
        namespace: Pod namespace.
        pod_name: Pod name.
        tail_lines: Number of tail lines to retrieve.

    Returns:
        {logs: str, pod_name, namespace} or {error}.
    """
    try:
        core, _ = _clients()
        logs = core.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines,
            timestamps=True,
        )
        return {"pod_name": pod_name, "namespace": namespace, "logs": logs}
    except ApiException as e:
        return {"error": f"ApiException {e.status}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def describe_pod(namespace: str, pod_name: str) -> dict:
    """Return detailed information about a pod: conditions, containers, resource usage.

    Args:
        namespace: Pod namespace.
        pod_name: Pod name.

    Returns:
        {name, namespace, phase, conditions, containers, node, volumes} or {error}.
    """
    try:
        core, _ = _clients()
        pod = core.read_namespaced_pod(name=pod_name, namespace=namespace)
        containers = []
        for c in pod.spec.containers:
            status = next(
                (s for s in (pod.status.container_statuses or []) if s.name == c.name),
                None,
            )
            state = {}
            if status and status.state:
                if status.state.running:
                    state = {"running": {"started_at": str(status.state.running.started_at)}}
                elif status.state.waiting:
                    state = {
                        "waiting": {
                            "reason": status.state.waiting.reason,
                            "message": status.state.waiting.message,
                        }
                    }
                elif status.state.terminated:
                    state = {
                        "terminated": {
                            "reason": status.state.terminated.reason,
                            "exit_code": status.state.terminated.exit_code,
                            "message": status.state.terminated.message,
                        }
                    }
            containers.append(
                {
                    "name": c.name,
                    "image": c.image,
                    "state": state,
                    "ready": status.ready if status else None,
                    "restart_count": status.restart_count if status else 0,
                    "requests": (
                        {
                            "cpu": c.resources.requests.get("cpu") if c.resources and c.resources.requests else None,
                            "memory": c.resources.requests.get("memory") if c.resources and c.resources.requests else None,
                        }
                        if c.resources
                        else {}
                    ),
                    "limits": (
                        {
                            "cpu": c.resources.limits.get("cpu") if c.resources and c.resources.limits else None,
                            "memory": c.resources.limits.get("memory") if c.resources and c.resources.limits else None,
                        }
                        if c.resources
                        else {}
                    ),
                }
            )
        return {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "phase": pod.status.phase,
            "node": pod.spec.node_name,
            "creation_timestamp": str(pod.metadata.creation_timestamp),
            "conditions": [
                {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                for c in (pod.status.conditions or [])
            ],
            "containers": containers,
        }
    except ApiException as e:
        return {"error": f"ApiException {e.status}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def get_events(namespace: Optional[str] = None, warning_only: bool = True) -> dict:
    """List Kubernetes events, optionally filtered to Warning type.

    Args:
        namespace: Namespace to query; None means all namespaces.
        warning_only: If True, return only Warning-type events.

    Returns:
        {events: [{namespace, name, reason, message, type, count, first_time, last_time, involved_object}]} or {error}.
    """
    try:
        core, _ = _clients()
        if namespace:
            event_list = core.list_namespaced_event(namespace=namespace)
        else:
            event_list = core.list_event_for_all_namespaces()

        events = []
        for e in event_list.items:
            if warning_only and e.type != "Warning":
                continue
            events.append(
                {
                    "namespace": e.metadata.namespace,
                    "name": e.metadata.name,
                    "reason": e.reason,
                    "message": e.message,
                    "type": e.type,
                    "count": e.count,
                    "first_time": str(e.first_timestamp),
                    "last_time": str(e.last_timestamp),
                    "involved_object": f"{e.involved_object.kind}/{e.involved_object.name}",
                }
            )
        events.sort(key=lambda x: x.get("last_time", "") or "", reverse=True)
        return {"events": events, "count": len(events)}
    except ApiException as e:
        return {"error": f"ApiException {e.status}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def get_nodes() -> dict:
    """Return node status and conditions for all cluster nodes.

    Returns:
        {nodes: [{name, status, conditions, capacity, allocatable, taints}]} or {error}.
    """
    try:
        core, _ = _clients()
        node_list = core.list_node()
        nodes = []
        for node in node_list.items:
            ready_condition = next(
                (c for c in (node.status.conditions or []) if c.type == "Ready"),
                None,
            )
            nodes.append(
                {
                    "name": node.metadata.name,
                    "ready": ready_condition.status if ready_condition else "Unknown",
                    "conditions": [
                        {"type": c.type, "status": c.status, "reason": c.reason}
                        for c in (node.status.conditions or [])
                    ],
                    "capacity": node.status.capacity,
                    "allocatable": node.status.allocatable,
                    "taints": [
                        {"key": t.key, "effect": t.effect, "value": t.value}
                        for t in (node.spec.taints or [])
                    ],
                    "labels": node.metadata.labels,
                }
            )
        return {"nodes": nodes, "count": len(nodes)}
    except ApiException as e:
        return {"error": f"ApiException {e.status}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Deployments
# ---------------------------------------------------------------------------


def get_deployments(namespace: Optional[str] = None) -> dict:
    """Return deployment status.

    Args:
        namespace: Namespace to query; None means all namespaces.

    Returns:
        {deployments: [{name, namespace, desired, ready, available, updated}]} or {error}.
    """
    try:
        _, apps = _clients()
        if namespace:
            dep_list = apps.list_namespaced_deployment(namespace=namespace)
        else:
            dep_list = apps.list_deployment_for_all_namespaces()

        deployments = []
        for d in dep_list.items:
            deployments.append(
                {
                    "name": d.metadata.name,
                    "namespace": d.metadata.namespace,
                    "desired": d.spec.replicas,
                    "ready": d.status.ready_replicas or 0,
                    "available": d.status.available_replicas or 0,
                    "updated": d.status.updated_replicas or 0,
                    "conditions": [
                        {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                        for c in (d.status.conditions or [])
                    ],
                }
            )
        return {"deployments": deployments, "count": len(deployments)}
    except ApiException as e:
        return {"error": f"ApiException {e.status}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def patch_deployment_resources(
    namespace: str,
    name: str,
    requests_cpu: Optional[str] = None,
    requests_memory: Optional[str] = None,
    limits_cpu: Optional[str] = None,
    limits_memory: Optional[str] = None,
) -> dict:
    """Patch resource requests/limits on all containers of a deployment.

    Args:
        namespace: Deployment namespace.
        name: Deployment name.
        requests_cpu: e.g. "200m".
        requests_memory: e.g. "256Mi".
        limits_cpu: e.g. "500m".
        limits_memory: e.g. "512Mi".

    Returns:
        {success: True, name, namespace} or {error}.
    """
    try:
        _, apps = _clients()
        deployment = apps.read_namespaced_deployment(name=name, namespace=namespace)
        for container in deployment.spec.template.spec.containers:
            if container.resources is None:
                container.resources = client.V1ResourceRequirements()
            if container.resources.requests is None:
                container.resources.requests = {}
            if container.resources.limits is None:
                container.resources.limits = {}
            if requests_cpu is not None:
                container.resources.requests["cpu"] = requests_cpu
            if requests_memory is not None:
                container.resources.requests["memory"] = requests_memory
            if limits_cpu is not None:
                container.resources.limits["cpu"] = limits_cpu
            if limits_memory is not None:
                container.resources.limits["memory"] = limits_memory

        # Enforce rolling strategy so the resource-change rollout is up-then-down
        deployment.spec.strategy = client.V1DeploymentStrategy(
            type="RollingUpdate",
            rolling_update=client.V1RollingUpdateDeployment(max_surge=1, max_unavailable=0),
        )
        apps.patch_namespaced_deployment(name=name, namespace=namespace, body=deployment)
        return {
            "success": True,
            "name": name,
            "namespace": namespace,
            "patched": {
                "requests_cpu": requests_cpu,
                "requests_memory": requests_memory,
                "limits_cpu": limits_cpu,
                "limits_memory": limits_memory,
            },
        }
    except ApiException as e:
        return {"error": f"ApiException {e.status}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def scale_deployment(namespace: str, name: str, replicas: int) -> dict:
    """Scale a deployment to the specified number of replicas.

    Args:
        namespace: Deployment namespace.
        name: Deployment name.
        replicas: Target replica count.

    Returns:
        {success: True, name, namespace, replicas} or {error}.
    """
    try:
        _, apps = _clients()
        body = {"spec": {"replicas": replicas}}
        apps.patch_namespaced_deployment_scale(name=name, namespace=namespace, body=body)
        return {"success": True, "name": name, "namespace": namespace, "replicas": replicas}
    except ApiException as e:
        return {"error": f"ApiException {e.status}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def rollback_deployment(namespace: str, name: str) -> dict:
    """Roll back a deployment to its previous revision using a rolling update (up-then-down).

    Finds the ReplicaSet for revision N-1, restores its container images, and enforces
    maxSurge=1 / maxUnavailable=0 so no existing pod is terminated before a replacement is ready.

    Args:
        namespace: Deployment namespace.
        name: Deployment name.

    Returns:
        {success: True, name, namespace, message} or {error}.
    """
    try:
        _, apps = _clients()
        deployment = apps.read_namespaced_deployment(name=name, namespace=namespace)
        annotations = deployment.metadata.annotations or {}
        current_revision = int(annotations.get("deployment.kubernetes.io/revision", "1"))
        target_revision = current_revision - 1

        if target_revision < 1:
            return {"error": "Already at revision 1 — no previous revision to roll back to"}

        # Find the ReplicaSet that corresponds to the target revision
        match_labels = deployment.spec.selector.match_labels or {}
        label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
        rs_list = apps.list_namespaced_replica_set(
            namespace=namespace, label_selector=label_selector
        )
        prev_rs = None
        for rs in rs_list.items:
            rs_rev = int((rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0"))
            if rs_rev == target_revision:
                prev_rs = rs
                break

        if prev_rs is None:
            return {
                "error": f"ReplicaSet for revision {target_revision} not found — it may have been garbage-collected"
            }

        # Restore previous container images with a rolling strategy so pods come up before going down
        containers_patch = [
            {"name": c.name, "image": c.image}
            for c in prev_rs.spec.template.spec.containers
        ]
        body = {
            "spec": {
                "strategy": {
                    "type": "RollingUpdate",
                    "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
                },
                "template": {"spec": {"containers": containers_patch}},
            }
        }
        apps.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
        return {
            "success": True,
            "name": name,
            "namespace": namespace,
            "message": (
                f"Rolling rollback from revision {current_revision} → {target_revision} "
                "(maxSurge=1, maxUnavailable=0 — up-then-down)"
            ),
        }
    except ApiException as e:
        return {"error": f"ApiException {e.status}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def rolling_restart_deployment(namespace: str, name: str) -> dict:
    """Restart a deployment with a rolling update (up-then-down).

    Bumps the restartedAt annotation and enforces maxSurge=1 / maxUnavailable=0
    so no pod is terminated before its replacement is ready.

    Args:
        namespace: Deployment namespace.
        name: Deployment name.

    Returns:
        {success: True, name, namespace, message} or {error}.
    """
    try:
        _, apps = _clients()
        body = {
            "spec": {
                "strategy": {
                    "type": "RollingUpdate",
                    "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
                },
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": datetime.datetime.utcnow().isoformat()
                        }
                    }
                },
            }
        }
        apps.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
        return {
            "success": True,
            "name": name,
            "namespace": namespace,
            "message": "Rolling restart triggered (maxSurge=1, maxUnavailable=0 — up-then-down)",
        }
    except ApiException as e:
        return {"error": f"ApiException {e.status}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}
