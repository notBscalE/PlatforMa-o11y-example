"""RemediationAgent: applies fixes identified by DiagnosticAgent."""

import json
import re
from datetime import datetime
from typing import Any

import yaml

from agents.diagnostic import DiagnosisResult, MODEL_ID
from tools.github import GitHubTools

CODE_FIX_SYSTEM = """You are a senior platform engineer implementing a fix for a production incident.
Given the diagnosis, produce the exact file changes needed to fix the issue.

ARCHITECTURE:
{architecture_yaml}

Return a JSON object with this structure:
{{
  "files": {{
    "path/to/file": "full new file content here"
  }},
  "pr_title": "fix: <concise description>",
  "pr_body": "## What\\n<explanation>\\n\\n## Why\\n<root cause>\\n\\n## Testing\\n<how to verify>"
}}

Be precise — include the FULL content of each file, not just the diff.
Only change files that directly fix the root cause."""

INFRA_FIX_SYSTEM = """You are a senior platform/infrastructure engineer implementing an infrastructure fix.
Given the diagnosis, produce the exact Terraform or Helm changes needed.

ARCHITECTURE:
{architecture_yaml}

Return a JSON object with this structure:
{{
  "files": {{
    "path/to/file": "full new file content here"
  }},
  "pr_title": "infra: <concise description>",
  "pr_body": "## What\\n<explanation>\\n\\n## Why\\n<root cause>\\n\\n## Risk\\n<blast radius>\\n\\n## Rollback\\n<how to revert>"
}}

Include FULL file contents. For Terraform changes, include the complete .tf file.
For Helm changes, include the complete values.yaml or template file."""


class RemediationAgent:
    def __init__(
        self,
        anthropic_client: Any,
        github_tools: GitHubTools,
        architecture: dict,
    ):
        self._client = anthropic_client
        self._github = github_tools
        self._architecture = architecture

    # ------------------------------------------------------------------
    # Immediate kubectl actions
    # ------------------------------------------------------------------

    async def apply_immediate_actions(
        self, actions: list[str], kubectl_tools: Any
    ) -> list[str]:
        """Execute immediate kubectl actions described in natural language.

        This interprets the action strings and calls the appropriate kubectl
        tool functions.

        Args:
            actions: List of action description strings from DiagnosisResult.
            kubectl_tools: The kubectl module (imported from tools.kubectl).

        Returns:
            List of result strings.
        """
        results = []
        for action in actions:
            try:
                result = await self._execute_action(action, kubectl_tools)
                results.append(f"✓ {action}: {json.dumps(result, default=str)}")
            except Exception as e:
                results.append(f"✗ {action}: {str(e)}")
        return results

    async def _execute_action(self, action: str, kubectl_tools: Any) -> dict:
        """Parse an action string and dispatch to the correct kubectl function."""
        action_lower = action.lower()

        # Scale: "scale deployment X to N replicas"
        scale_match = re.search(
            r"scale\s+(?:deployment\s+)?(\S+)\s+(?:in\s+namespace\s+(\S+)\s+)?to\s+(\d+)\s+replica",
            action_lower,
        )
        if scale_match:
            name = scale_match.group(1)
            ns = scale_match.group(2) or "platformma"
            replicas = int(scale_match.group(3))
            return kubectl_tools.scale_deployment(ns, name, replicas)

        # Rollback: "rollback deployment X"
        rollback_match = re.search(
            r"rollback\s+(?:deployment\s+)?(\S+)(?:\s+in\s+namespace\s+(\S+))?",
            action_lower,
        )
        if rollback_match:
            name = rollback_match.group(1)
            ns = rollback_match.group(2) or "platformma"
            return kubectl_tools.rollback_deployment(ns, name)

        # Restart: "restart deployment X"
        restart_match = re.search(
            r"restart\s+(?:deployment\s+)?(\S+)(?:\s+in\s+namespace\s+(\S+))?",
            action_lower,
        )
        if restart_match:
            name = restart_match.group(1)
            ns = restart_match.group(2) or "platformma"
            return kubectl_tools.rolling_restart_deployment(ns, name)

        # Patch resources: "patch deployment X resources cpu=200m memory=256Mi"
        patch_match = re.search(
            r"patch\s+(?:deployment\s+)?(\S+)(?:\s+in\s+namespace\s+(\S+))?",
            action_lower,
        )
        if patch_match:
            name = patch_match.group(1)
            ns = patch_match.group(2) or "platformma"
            cpu_match = re.search(r"cpu[=:\s]+(\S+)", action_lower)
            mem_match = re.search(r"memory[=:\s]+(\S+)", action_lower)
            return kubectl_tools.patch_deployment_resources(
                ns,
                name,
                requests_cpu=cpu_match.group(1) if cpu_match else None,
                requests_memory=mem_match.group(1) if mem_match else None,
            )

        return {"skipped": True, "action": action, "reason": "Could not parse action — manual execution needed"}

    # ------------------------------------------------------------------
    # Action classification
    # ------------------------------------------------------------------

    @staticmethod
    def classify_actions(actions: list[str]) -> tuple[list[str], list[str]]:
        """Split actions into (safe, downtime_risk).

        Safe: rollback, restart, patch-resources — all now use rolling updates
              (maxSurge=1, maxUnavailable=0) so pods come up before going down.
        Downtime risk: scale operations (direction unknown without live state),
                       delete/drain operations.

        Returns:
            (safe_actions, risky_actions)
        """
        safe, risky = [], []
        for action in actions:
            lower = action.lower()
            if re.search(r"\b(scale|delete|drain)\b", lower):
                risky.append(action)
            else:
                safe.append(action)
        return safe, risky

    # ------------------------------------------------------------------
    # Code fix PR
    # ------------------------------------------------------------------

    async def create_code_fix_pr(
        self, diagnosis: DiagnosisResult, incident_issue_number: int
    ) -> dict:
        """Generate a code fix using Claude and open a pull request.

        Args:
            diagnosis: DiagnosisResult from DiagnosticAgent.
            incident_issue_number: GitHub issue number to link in the PR.

        Returns:
            {branch, pr_number, pr_url}.
        """
        architecture_yaml = yaml.dump(self._architecture, default_flow_style=False)
        fix_details = json.dumps(diagnosis.proposed_fix_details, indent=2)

        response = self._client.messages.create(
            model=MODEL_ID,
            max_tokens=8096,
            system=CODE_FIX_SYSTEM.format(architecture_yaml=architecture_yaml),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Root cause: {diagnosis.root_cause}\n\n"
                        f"Proposed fix: {diagnosis.proposed_fix}\n\n"
                        f"Fix details:\n{fix_details}\n\n"
                        f"Evidence:\n" + "\n".join(f"- {e}" for e in diagnosis.evidence)
                        + f"\n\nRelated incident issue: #{incident_issue_number}"
                        + "\n\nProvide the exact file changes as JSON."
                    ),
                }
            ],
        )

        fix_data = self._extract_json(response.content)
        if not fix_data or "files" not in fix_data:
            raise RuntimeError("Claude did not return valid file changes for code fix")

        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        branch_name = f"fix/incident-code-{timestamp}"

        self._github.create_branch(branch_name)
        self._github.commit_files(
            branch=branch_name,
            files=fix_data["files"],
            message=f"fix: {diagnosis.root_cause[:72]}",
        )

        default_body = (
            f"Automated fix for incident #{incident_issue_number}.\n\n"
            f"Root cause: {diagnosis.root_cause}\n\n"
            f"Addresses #{incident_issue_number}"
        )
        pr_body = fix_data.get("pr_body", default_body)
        # Use a reference that links without auto-closing — the issue closes
        # only when the health check confirms the system is healthy.
        if f"#{incident_issue_number}" not in pr_body:
            pr_body += f"\n\nAddresses #{incident_issue_number}"

        pr = self._github.create_pr(
            title=fix_data.get("pr_title", f"fix: {diagnosis.root_cause[:60]}"),
            body=pr_body,
            head_branch=branch_name,
        )

        return {
            "branch": branch_name,
            "pr_number": pr["number"],
            "pr_url": pr["url"],
            "pr_title": fix_data.get("pr_title", ""),
            "files_changed": list(fix_data.get("files", {}).keys()),
        }

    # ------------------------------------------------------------------
    # Infrastructure fix PR
    # ------------------------------------------------------------------

    async def create_infra_fix_pr(
        self, diagnosis: DiagnosisResult, incident_issue_number: int
    ) -> dict:
        """Generate an infra (Terraform/Helm) fix using Claude and open a PR.

        Args:
            diagnosis: DiagnosisResult from DiagnosticAgent.
            incident_issue_number: GitHub issue number for cross-reference.

        Returns:
            {branch, pr_number, pr_url}.
        """
        architecture_yaml = yaml.dump(self._architecture, default_flow_style=False)
        fix_details = json.dumps(diagnosis.proposed_fix_details, indent=2)

        response = self._client.messages.create(
            model=MODEL_ID,
            max_tokens=8096,
            system=INFRA_FIX_SYSTEM.format(architecture_yaml=architecture_yaml),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Root cause: {diagnosis.root_cause}\n\n"
                        f"Category: {diagnosis.category}\n\n"
                        f"Proposed fix: {diagnosis.proposed_fix}\n\n"
                        f"Fix details:\n{fix_details}\n\n"
                        f"Evidence:\n" + "\n".join(f"- {e}" for e in diagnosis.evidence)
                        + f"\n\nRelated incident issue: #{incident_issue_number}"
                        + "\n\nProvide the exact Terraform/Helm file changes as JSON."
                    ),
                }
            ],
        )

        fix_data = self._extract_json(response.content)
        if not fix_data or "files" not in fix_data:
            raise RuntimeError("Claude did not return valid file changes for infra fix")

        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        branch_name = f"fix/incident-infra-{timestamp}"

        self._github.create_branch(branch_name)
        self._github.commit_files(
            branch=branch_name,
            files=fix_data["files"],
            message=f"infra: {diagnosis.root_cause[:72]}",
        )

        default_body = (
            f"Infrastructure fix for incident #{incident_issue_number}.\n\n"
            f"Root cause: {diagnosis.root_cause}\n\n"
            f"Addresses #{incident_issue_number}"
        )
        pr_body = fix_data.get("pr_body", default_body)
        if f"#{incident_issue_number}" not in pr_body:
            pr_body += f"\n\nAddresses #{incident_issue_number}"

        pr = self._github.create_pr(
            title=fix_data.get("pr_title", f"infra: {diagnosis.root_cause[:60]}"),
            body=pr_body,
            head_branch=branch_name,
        )

        return {
            "branch": branch_name,
            "pr_number": pr["number"],
            "pr_url": pr["url"],
            "pr_title": fix_data.get("pr_title", ""),
            "files_changed": list(fix_data.get("files", {}).keys()),
        }

    # ------------------------------------------------------------------
    # Incident documentation
    # ------------------------------------------------------------------

    def generate_incident_doc(
        self,
        incident: dict,
        diagnosis: DiagnosisResult,
        fix_result: str,
        fix_pr_url: str = "",
        fix_pr_title: str = "",
        diff_files: list | None = None,
    ) -> str:
        """Generate a markdown incident report.

        Args:
            incident: Dict with keys: title, started_at, diagnosed_at, fixed_at.
            diagnosis: DiagnosisResult from DiagnosticAgent.
            fix_result: Description of what fix was applied.

        Returns:
            Markdown string.
        """
        title = incident.get("title", "Untitled Incident")
        started_at = incident.get("started_at", datetime.utcnow().isoformat())
        diagnosed_at = incident.get("diagnosed_at", datetime.utcnow().isoformat())
        fixed_at = incident.get("fixed_at", datetime.utcnow().isoformat())
        resolved_at = incident.get("resolved_at", datetime.utcnow().isoformat())
        date = datetime.utcnow().strftime("%Y-%m-%d")

        evidence_lines = "\n".join(f"- {e}" for e in diagnosis.evidence)
        immediate_actions = "\n".join(f"- {a}" for a in diagnosis.immediate_actions)

        files_changed = ""
        for fc in diagnosis.proposed_fix_details.get("files_to_change", []):
            files_changed += f"- `{fc.get('path', 'unknown')}`: {fc.get('change_description', '')}\n"

        prevention = (
            "Review resource limits and add automated scaling policies. "
            "Add more granular alerting for the affected components. "
            "Consider adding circuit breakers and retry logic to reduce blast radius."
        )

        # Build fix PR section with diff
        pr_section = ""
        if fix_pr_url:
            pr_title_display = fix_pr_title or "Fix PR"
            pr_section = f"\n## Fix Pull Request\n\n**[{pr_title_display}]({fix_pr_url})**\n"
            if diff_files:
                pr_section += "\n### Changes\n"
                for f in diff_files:
                    pr_section += f"\n#### `{f['filename']}` (+{f['additions']} -{f['deletions']})\n"
                    patch = (f.get("patch") or "").strip()
                    if patch:
                        pr_section += f"```diff\n{patch}\n```\n"

        return f"""# Incident: {title}
**Date:** {date}
**Severity:** {diagnosis.severity}
**Status:** Resolved

## Summary
{diagnosis.root_cause}

## Timeline
- {started_at}: Alert fired / health check failed
- {diagnosed_at}: Diagnosis complete
- {fixed_at}: Fix applied
- {resolved_at}: System healthy

## Root Cause
{diagnosis.root_cause}

**Category:** {diagnosis.category}
**Affected components:** {", ".join(diagnosis.affected_components)}

## Immediate Actions Taken
{immediate_actions if immediate_actions else "No immediate actions were required."}

## Fix Applied
{fix_result}

### Files Changed
{files_changed if files_changed else "No file changes (see fix PR for diff)."}
{pr_section}
## Prevention
{prevention}

## Metrics at Time of Incident
{evidence_lines if evidence_lines else "No metrics evidence recorded."}
"""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_json(content: list) -> dict | None:
        """Extract the first JSON object from Claude's response content blocks."""
        for block in content:
            if hasattr(block, "text"):
                text = block.text.strip()
                if text.startswith("```"):
                    lines = text.splitlines()
                    text = "\n".join(line for line in lines if not line.startswith("```"))
                start = text.find("{")
                if start == -1:
                    continue
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
