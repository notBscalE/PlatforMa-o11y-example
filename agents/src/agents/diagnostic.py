"""DiagnosticAgent: uses Claude with tools to investigate platform incidents."""

import json
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from tools.prometheus import (
    get_active_alerts,
    get_metric_value,
    query_prometheus,
    query_prometheus_range,
)
from tools.argocd import (
    get_app_events,
    get_app_health,
    get_app_pods,
    list_apps,
)
from tools.cloudwatch import get_eks_logs, get_recent_errors, search_logs
from tools.github import list_directory, read_file
from tools.kubectl import (
    describe_pod,
    get_deployments,
    get_events,
    get_nodes,
    get_pod_logs,
    get_pods,
)

MODEL_ID = "claude-opus-4-7"
MAX_TURNS = 15

SYSTEM_PROMPT = """You are a senior platform engineer on-call for the PlatforMa platform.
You have been triggered by an alert or health check failure.
Your job is to investigate the issue using the available tools, identify the root cause,
and propose a concrete fix.

ARCHITECTURE:
{architecture_yaml}

TRIGGER:
{trigger_json}

Instructions:
1. Use the available tools to gather metrics, logs, and cluster state
2. Query Prometheus for relevant metrics
3. Check ArgoCD application status
4. Check pod status and events in the affected namespace
5. Check CloudWatch for cluster-level errors if relevant
6. Read relevant source files from the repository using list_directory and read_file — always check application code (app/main.go), Helm values, and Dockerfiles when the symptom could be a code or config bug
7. Once you have enough evidence, synthesize your findings
8. Classify the issue: infrastructure (cluster/node/network/resources), code (application bug), transient (temporary spike, already resolving), or unknown
9. Propose a specific fix with exact file changes if applicable
10. Identify any immediate kubectl actions to reduce blast radius right now

When you have enough information, output ONLY a JSON object matching the DiagnosisResult schema.
Be specific — name exact files, exact metrics, exact error messages.

DiagnosisResult schema:
{
  "root_cause": "string describing root cause",
  "category": "infrastructure" | "code" | "transient" | "unknown",
  "severity": "critical" | "high" | "medium" | "low",
  "affected_components": ["list of affected component names"],
  "proposed_fix": "string describing the fix",
  "proposed_fix_details": {
    "files_to_change": [
      {"path": "path/to/file", "change_description": "what to change", "new_content": "full new file content"}
    ]
  },
  "immediate_actions": ["kubectl commands or descriptions of immediate actions"],
  "evidence": ["key facts found during investigation"],
  "can_auto_remediate": true | false
}"""


class DiagnosisResult(BaseModel):
    root_cause: str
    category: Literal["infrastructure", "code", "transient", "unknown"]
    severity: Literal["critical", "high", "medium", "low"]
    affected_components: list[str]
    proposed_fix: str
    proposed_fix_details: dict
    immediate_actions: list[str]
    evidence: list[str]
    can_auto_remediate: bool


# ---------------------------------------------------------------------------
# Tool definitions for Claude
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "query_prometheus",
        "description": "Execute a PromQL instant query against Prometheus. Returns {status, result, resultType}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "PromQL expression to evaluate."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "query_prometheus_range",
        "description": "Execute a PromQL range query. Returns {status, result, resultType}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "PromQL expression."},
                "start_minutes_ago": {"type": "integer", "description": "Range start in minutes ago."},
                "end_minutes_ago": {"type": "integer", "description": "Range end in minutes ago (0 = now)."},
                "step": {"type": "string", "description": "Resolution step, e.g. '30s', '1m'."},
            },
            "required": ["query", "start_minutes_ago", "end_minutes_ago", "step"],
        },
    },
    {
        "name": "get_active_alerts",
        "description": "Return all currently firing Prometheus alerts.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_metric_value",
        "description": "Convenience: query current value of a metric with optional label filters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric_name": {"type": "string", "description": "Prometheus metric name."},
                "labels": {
                    "type": "object",
                    "description": "Optional label key/value pairs.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["metric_name"],
        },
    },
    {
        "name": "get_app_health",
        "description": "Return ArgoCD application health status, sync status, and conditions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "ArgoCD application name."}
            },
            "required": ["app_name"],
        },
    },
    {
        "name": "get_app_events",
        "description": "Return recent Kubernetes events for an ArgoCD application.",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "ArgoCD application name."}
            },
            "required": ["app_name"],
        },
    },
    {
        "name": "list_apps",
        "description": "List all ArgoCD applications with their health and sync status.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_app_pods",
        "description": "Return pods managed by an ArgoCD application.",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_name": {"type": "string"},
                "namespace": {"type": "string"},
            },
            "required": ["app_name", "namespace"],
        },
    },
    {
        "name": "get_eks_logs",
        "description": "Query EKS control plane CloudWatch logs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Hours of logs to retrieve."},
                "filter_pattern": {"type": "string", "description": "CloudWatch filter pattern."},
            },
        },
    },
    {
        "name": "search_logs",
        "description": "Search a CloudWatch log group.",
        "input_schema": {
            "type": "object",
            "properties": {
                "log_group": {"type": "string"},
                "filter_pattern": {"type": "string"},
                "hours": {"type": "integer"},
            },
            "required": ["log_group", "filter_pattern"],
        },
    },
    {
        "name": "get_recent_errors",
        "description": "Retrieve error and warning messages from EKS CloudWatch logs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Hours of logs to scan."}
            },
        },
    },
    {
        "name": "get_pods",
        "description": "List Kubernetes pods with their status and restart counts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace (omit for all namespaces)."}
            },
        },
    },
    {
        "name": "get_events",
        "description": "List Kubernetes events, optionally filtered to Warning type.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "warning_only": {"type": "boolean"},
            },
        },
    },
    {
        "name": "describe_pod",
        "description": "Get detailed information about a pod: conditions, containers, resource config.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod_name": {"type": "string"},
            },
            "required": ["namespace", "pod_name"],
        },
    },
    {
        "name": "get_nodes",
        "description": "Return node status and conditions for all cluster nodes.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_pod_logs",
        "description": "Retrieve recent logs from a specific pod.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod_name": {"type": "string"},
                "tail_lines": {"type": "integer", "description": "Number of log lines to retrieve."},
            },
            "required": ["namespace", "pod_name"],
        },
    },
    {
        "name": "get_deployments",
        "description": "Return deployment status for one or all namespaces.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"}
            },
        },
    },
    {
        "name": "read_file",
        "description": "Read a source file from the GitHub repository. Use this to inspect application code, Helm chart values, Dockerfiles, or config files when diagnosing a potential code or configuration bug.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root, e.g. 'app/main.go' or 'helm/platformma-app/values.yaml'."},
                "branch": {"type": "string", "description": "Branch to read from (default: main)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and directories at a path in the GitHub repository. Use this to discover what source files exist before reading them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path relative to repo root. Empty string for the root."},
                "branch": {"type": "string", "description": "Branch to read from (default: main)."},
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


async def _dispatch_tool(name: str, inputs: dict) -> Any:
    """Call the appropriate tool function and return the result."""
    if name == "query_prometheus":
        return await query_prometheus(**inputs)
    elif name == "query_prometheus_range":
        return await query_prometheus_range(**inputs)
    elif name == "get_active_alerts":
        return await get_active_alerts()
    elif name == "get_metric_value":
        return await get_metric_value(**inputs)
    elif name == "get_app_health":
        return await get_app_health(**inputs)
    elif name == "get_app_events":
        return await get_app_events(**inputs)
    elif name == "list_apps":
        return await list_apps()
    elif name == "get_app_pods":
        return await get_app_pods(**inputs)
    elif name == "get_eks_logs":
        return await get_eks_logs(**inputs)
    elif name == "search_logs":
        return await search_logs(**inputs)
    elif name == "get_recent_errors":
        return await get_recent_errors(**inputs)
    elif name == "get_pods":
        return get_pods(**inputs)
    elif name == "get_events":
        return get_events(**inputs)
    elif name == "describe_pod":
        return describe_pod(**inputs)
    elif name == "get_nodes":
        return get_nodes()
    elif name == "get_pod_logs":
        return get_pod_logs(**inputs)
    elif name == "get_deployments":
        return get_deployments(**inputs)
    elif name == "read_file":
        return read_file(**inputs)
    elif name == "list_directory":
        return list_directory(**inputs)
    else:
        return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# DiagnosticAgent
# ---------------------------------------------------------------------------


class DiagnosticAgent:
    def __init__(self, anthropic_client, architecture: dict):
        self._client = anthropic_client
        self._architecture = architecture

    async def investigate(self, trigger: dict) -> DiagnosisResult:
        """Run a multi-turn Claude conversation with tools to diagnose an incident.

        Args:
            trigger: Dict describing the triggering event (source, alerts, etc.).

        Returns:
            DiagnosisResult with root cause, category, proposed fix, etc.
        """
        architecture_yaml = yaml.dump(self._architecture, default_flow_style=False)
        trigger_json = json.dumps(trigger, indent=2, default=str)

        system = (
            SYSTEM_PROMPT
            .replace("{architecture_yaml}", architecture_yaml)
            .replace("{trigger_json}", trigger_json)
        )

        messages = [
            {
                "role": "user",
                "content": (
                    "A new incident has been detected. Please investigate using the available tools "
                    "and return your diagnosis as a JSON object matching the DiagnosisResult schema."
                ),
            }
        ]

        for turn in range(MAX_TURNS):
            response = self._client.messages.create(
                model=MODEL_ID,
                max_tokens=8096,
                system=system,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )

            # Append assistant response to conversation
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                # Extract JSON from last text block
                diagnosis_json = self._extract_diagnosis_json(response.content)
                if diagnosis_json:
                    return DiagnosisResult(**diagnosis_json)
                # If no JSON found, keep iterating (shouldn't happen with the prompt)
                messages.append(
                    {
                        "role": "user",
                        "content": "Please provide your diagnosis as a JSON object now.",
                    }
                )
                continue

            if response.stop_reason == "tool_use":
                # Collect all tool calls and dispatch them
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = await _dispatch_tool(block.name, block.input)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result, default=str),
                            }
                        )

                messages.append({"role": "user", "content": tool_results})
                continue

            # Any other stop reason — ask for final answer
            messages.append(
                {
                    "role": "user",
                    "content": "Please finalize your investigation and provide the DiagnosisResult JSON.",
                }
            )

        # Exhausted turns — ask one final time without tools
        response = self._client.messages.create(
            model=MODEL_ID,
            max_tokens=4096,
            system=system,
            messages=messages
            + [
                {
                    "role": "user",
                    "content": "Provide your best diagnosis now as a JSON object matching DiagnosisResult.",
                }
            ],
        )
        diagnosis_json = self._extract_diagnosis_json(response.content)
        if diagnosis_json:
            return DiagnosisResult(**diagnosis_json)

        # Fallback: return an unknown diagnosis
        return DiagnosisResult(
            root_cause="Could not determine root cause within investigation limits",
            category="unknown",
            severity="high",
            affected_components=["unknown"],
            proposed_fix="Manual investigation required",
            proposed_fix_details={"files_to_change": []},
            immediate_actions=[],
            evidence=[],
            can_auto_remediate=False,
        )

    @staticmethod
    def _extract_diagnosis_json(content: list) -> dict | None:
        """Extract the first valid JSON object from Claude's text blocks."""
        for block in content:
            if hasattr(block, "text"):
                text = block.text.strip()
                # Strip markdown code fences if present
                if text.startswith("```"):
                    lines = text.splitlines()
                    text = "\n".join(
                        line for line in lines if not line.startswith("```")
                    )
                # Find the outermost { ... }
                start = text.find("{")
                if start == -1:
                    continue
                # Find the matching closing brace
                depth = 0
                for i, ch in enumerate(text[start:], start):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(text[start : i + 1])
                            except json.JSONDecodeError:
                                break
        return None
