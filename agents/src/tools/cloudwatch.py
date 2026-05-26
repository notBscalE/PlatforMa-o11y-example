"""AWS CloudWatch Logs tools for the observability agents."""

import asyncio
import os
import time

import boto3

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
EKS_LOG_GROUP = "/aws/eks/platformma/cluster"


def _get_client():
    return boto3.client("logs", region_name=AWS_REGION)


def _filter_log_events_sync(
    log_group: str,
    filter_pattern: str,
    start_time_ms: int,
    end_time_ms: int,
    limit: int = 200,
) -> list:
    """Synchronous CloudWatch Logs filter_log_events call."""
    client = _get_client()
    kwargs = {
        "logGroupName": log_group,
        "startTime": start_time_ms,
        "endTime": end_time_ms,
        "limit": limit,
    }
    if filter_pattern:
        kwargs["filterPattern"] = filter_pattern

    events = []
    try:
        paginator = client.get_paginator("filter_log_events")
        pages = paginator.paginate(**kwargs, PaginationConfig={"MaxItems": limit})
        for page in pages:
            events.extend(page.get("events", []))
            if len(events) >= limit:
                break
    except Exception:
        # Some log groups may not have a paginator; fall back to direct call
        resp = client.filter_log_events(**kwargs)
        events = resp.get("events", [])
    return events


async def get_eks_logs(hours: int = 1, filter_pattern: str = "") -> dict:
    """Query the EKS control plane CloudWatch log group.

    Args:
        hours: How many hours of logs to retrieve (default 1).
        filter_pattern: CloudWatch filter pattern (default: all logs).

    Returns:
        {log_group, events: [{timestamp, message, stream}], count} or {error}.
    """
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (hours * 3600 * 1000)
    try:
        loop = asyncio.get_event_loop()
        events = await loop.run_in_executor(
            None,
            lambda: _filter_log_events_sync(EKS_LOG_GROUP, filter_pattern, start_ms, now_ms),
        )
        formatted = [
            {
                "timestamp": e.get("timestamp"),
                "message": e.get("message", "").strip(),
                "stream": e.get("logStreamName"),
            }
            for e in events
        ]
        return {
            "log_group": EKS_LOG_GROUP,
            "events": formatted,
            "count": len(formatted),
            "hours_queried": hours,
        }
    except Exception as e:
        return {"error": str(e)}


async def search_logs(log_group: str, filter_pattern: str, hours: int = 1) -> dict:
    """Generic CloudWatch Logs search.

    Args:
        log_group: The CloudWatch log group name.
        filter_pattern: CloudWatch filter pattern string.
        hours: How many hours to search back.

    Returns:
        {log_group, events: [{timestamp, message, stream}], count} or {error}.
    """
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (hours * 3600 * 1000)
    try:
        loop = asyncio.get_event_loop()
        events = await loop.run_in_executor(
            None,
            lambda: _filter_log_events_sync(log_group, filter_pattern, start_ms, now_ms),
        )
        formatted = [
            {
                "timestamp": e.get("timestamp"),
                "message": e.get("message", "").strip(),
                "stream": e.get("logStreamName"),
            }
            for e in events
        ]
        return {
            "log_group": log_group,
            "events": formatted,
            "count": len(formatted),
            "hours_queried": hours,
        }
    except Exception as e:
        return {"error": str(e)}


async def get_recent_errors(hours: int = 1) -> dict:
    """Retrieve error and warning messages from the EKS control plane logs.

    Args:
        hours: How many hours of logs to scan for errors.

    Returns:
        {errors: [{timestamp, message, stream}], count} or {error}.
    """
    # CloudWatch filter pattern matching ERROR or WARNING level messages
    error_pattern = '?"ERROR" ?"error" ?"WARN" ?"warning" ?"Exception" ?"panic" ?"fatal"'
    result = await get_eks_logs(hours=hours, filter_pattern=error_pattern)
    if "error" in result:
        return result
    return {
        "errors": result.get("events", []),
        "count": result.get("count", 0),
        "hours_queried": hours,
    }
