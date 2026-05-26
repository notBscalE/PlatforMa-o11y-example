"""Tests for the FastAPI webhook endpoints."""

import pytest


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


def test_alertmanager_webhook_firing(client, mock_orchestrator):
    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "PlatformmaAppDown", "namespace": "platformma"},
                "annotations": {"summary": "App is down"},
            }
        ],
        "groupLabels": {"alertname": "PlatformmaAppDown"},
        "commonLabels": {},
        "commonAnnotations": {},
    }
    resp = client.post("/webhook/alertmanager", json=payload)
    assert resp.status_code == 200
    assert resp.json()["firing_alerts"] == 1
    assert resp.json()["status"] == "accepted"


def test_alertmanager_webhook_resolved_only(client, mock_orchestrator):
    """Resolved-only payloads should be accepted but not trigger an investigation."""
    payload = {
        "alerts": [{"status": "resolved", "labels": {"alertname": "PlatformmaAppDown"}}],
        "groupLabels": {},
        "commonLabels": {},
    }
    resp = client.post("/webhook/alertmanager", json=payload)
    assert resp.status_code == 200
    assert resp.json()["firing_alerts"] == 0
    mock_orchestrator.handle_trigger.assert_not_called()


def test_alertmanager_webhook_invalid_json(client):
    resp = client.post(
        "/webhook/alertmanager",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_argocd_webhook_degraded(client, mock_orchestrator):
    payload = {
        "application": {
            "metadata": {"name": "platformma-app"},
            "status": {"health": {"status": "Degraded"}},
        }
    }
    resp = client.post("/webhook/argocd", json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert resp.json()["health_status"] == "Degraded"


def test_argocd_webhook_healthy_ignored(client, mock_orchestrator):
    """Healthy status should not trigger an investigation."""
    payload = {
        "application": {
            "metadata": {"name": "platformma-app"},
            "status": {"health": {"status": "Healthy"}},
        }
    }
    resp = client.post("/webhook/argocd", json=payload)
    assert resp.status_code == 200
    mock_orchestrator.handle_trigger.assert_not_called()


def test_check_endpoint(client):
    resp = client.get("/check")
    assert resp.status_code == 200
    assert resp.json()["status"] == "check started"
