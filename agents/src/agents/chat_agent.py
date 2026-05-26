"""Interactive chat agent — responds to GitHub Issue comments during an incident."""

import json
import logging

import anthropic
import yaml

from agents.diagnostic import TOOL_DEFINITIONS, _dispatch_tool

logger = logging.getLogger(__name__)

MODEL_ID = "claude-opus-4-7"
MAX_TURNS = 20

SYSTEM_PROMPT = """\
You are an on-call platform engineer assistant for the PlatforMa platform.
A DevOps engineer is talking to you through a GitHub Issue comment thread during an incident.

PLATFORM ARCHITECTURE:
{architecture_yaml}

{incident_section}

You have access to tools to query the live system:
- Prometheus metrics and alerts
- ArgoCD application health and events
- Kubernetes pod logs, events, node status
- AWS CloudWatch logs

Guidelines:
- Be concise and direct — the engineer is under pressure
- Format responses in GitHub-flavoured markdown
- If asked to take a destructive action (restart, rollback, scale down), confirm intent \
by asking "Are you sure you want me to X? Reply **yes** to confirm." before acting
- If you do not know something, say so rather than guessing
"""

INCIDENT_SECTION = """\
CURRENT INCIDENT CONTEXT:
{incident_json}
"""

NO_INCIDENT_SECTION = """\
No active incident is currently being tracked. \
You can help with general platform questions or investigate potential issues.
"""


class ChatAgent:
    """Per-session conversational agent backed by Claude, used for GitHub Issue threads."""

    def __init__(self, anthropic_client: anthropic.Anthropic, architecture: dict):
        self._client = anthropic_client
        self._architecture = architecture
        # session_id → message history
        self._sessions: dict[str, list[dict]] = {}

    async def chat(
        self,
        session_id: str,
        user_message: str,
        incident_context: dict | None = None,
    ) -> str:
        """Process one user message and return the full assistant response.

        Conversation history is retained per session_id so follow-up questions
        in the same issue thread have full context.
        """
        architecture_yaml = yaml.dump(self._architecture, default_flow_style=False)

        incident_section = (
            INCIDENT_SECTION.format(
                incident_json=json.dumps(incident_context, indent=2, default=str)
            )
            if incident_context
            else NO_INCIDENT_SECTION
        )

        system = SYSTEM_PROMPT.format(
            architecture_yaml=architecture_yaml,
            incident_section=incident_section,
        )

        history = self._sessions.setdefault(session_id, [])
        history.append({"role": "user", "content": user_message})
        messages = list(history)

        result = await self._tool_loop(system, messages)

        history.append({"role": "assistant", "content": result})
        # Keep history bounded at 40 messages (~20 exchanges)
        if len(history) > 40:
            self._sessions[session_id] = history[-40:]

        return result

    def clear_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def _tool_loop(self, system: str, messages: list[dict]) -> str:
        for _ in range(MAX_TURNS):
            response = self._client.messages.create(
                model=MODEL_ID,
                max_tokens=4096,
                system=system,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        return block.text
                return ""

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.debug("Chat tool call: %s", block.name)
                        result = await _dispatch_tool(block.name, block.input)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result, default=str),
                            }
                        )
                messages.append({"role": "user", "content": tool_results})

        return "I reached the maximum reasoning steps. Please ask a more specific question."
