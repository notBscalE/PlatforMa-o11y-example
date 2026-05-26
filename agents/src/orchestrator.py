"""Orchestrator: drives the full incident detection → diagnosis → remediation → resolution workflow."""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import yaml

from agents.chat_agent import ChatAgent
from agents.diagnostic import DiagnosticAgent
from agents.health_checker import HealthChecker
from agents.remediation import RemediationAgent
from tools import kubectl as kubectl_tools
from tools.github import GitHubTools

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

ARCHITECTURE_PATH = Path(__file__).parent / "architecture.yaml"

# Labels that indicate a non-closed incident is in progress
ACTIVE_INCIDENT_LABELS = {"incident", "awaiting-approval", "applying", "code-fix", "infra-fix"}

# Approval poll interval and timeout
APPROVAL_POLL_SECONDS = 60
APPROVAL_TIMEOUT_HOURS = 24

HEALTH_TIMEOUT_MINUTES = 10

# How often to poll GitHub Issues for new operator comments
COMMENT_POLL_SECONDS = 30

# Login of the GitHub account that posts agent comments (set during init)
_BOT_LOGIN: str | None = None


class Orchestrator:
    """Orchestrates the full incident lifecycle."""

    def __init__(self):
        self._anthropic_api_key = os.environ["ANTHROPIC_API_KEY"]
        self._github_token = os.environ["GITHUB_TOKEN"]
        self._github_repo = os.environ.get("GITHUB_REPO", "EXAMPLE_REPO")

        self._architecture = self._load_architecture()

        self._client = anthropic.Anthropic(api_key=self._anthropic_api_key)
        self._github = GitHubTools(
            token=self._github_token,
            repo_full_name=self._github_repo,
        )
        self._diagnostic = DiagnosticAgent(
            anthropic_client=self._client,
            architecture=self._architecture,
        )
        self._remediation = RemediationAgent(
            anthropic_client=self._client,
            github_tools=self._github,
            architecture=self._architecture,
        )
        self._health_checker = HealthChecker()
        self._chat = ChatAgent(
            anthropic_client=self._client,
            architecture=self._architecture,
        )

        # Track the last-seen comment ID per issue so we don't re-process comments
        self._last_seen_comment: dict[int, int] = {}
        # Issues the agent has already acknowledged (so we don't greet them twice)
        self._greeted_issues: set[int] = set()
        # Issues for which an incident-report PR has already been created this session
        self._incident_reports_created: set[int] = set()

        # Ensure GitHub labels are present and record bot login
        global _BOT_LOGIN
        try:
            self._github.ensure_labels_exist()
            _BOT_LOGIN = self._github._gh.get_user().login
            logger.info("Bot GitHub login: %s", _BOT_LOGIN)
        except Exception as e:
            logger.warning("Could not initialise GitHub labels/login: %s", e)

    # ------------------------------------------------------------------
    # Architecture
    # ------------------------------------------------------------------

    def _load_architecture(self) -> dict:
        with open(ARCHITECTURE_PATH) as f:
            return yaml.safe_load(f)

    # ------------------------------------------------------------------
    # Active incident guard
    # ------------------------------------------------------------------

    def _is_active_incident(self) -> bool:
        """Return True if there is already an open incident being worked on."""
        try:
            open_incidents = self._github.get_open_incidents()
            for incident in open_incidents:
                labels = set(incident.get("labels", []))
                if labels & ACTIVE_INCIDENT_LABELS:
                    return True
            return False
        except Exception as e:
            logger.warning("Could not check active incidents: %s", e)
            return False

    # ------------------------------------------------------------------
    # Main workflow
    # ------------------------------------------------------------------

    async def handle_trigger(self, trigger: dict) -> None:
        """Full incident workflow from trigger to resolution.

        Args:
            trigger: Dict describing the triggering event.
        """
        now = datetime.now(timezone.utc).isoformat()
        logger.info("handle_trigger called: source=%s", trigger.get("source"))

        # 1. Guard: don't double-trigger
        if self._is_active_incident():
            logger.info("Active incident already in progress — ignoring new trigger")
            return

        # 2. Create GitHub issue
        issue_title = self._format_issue_title(trigger)
        issue_body = self._format_issue_body(trigger, now)
        try:
            issue = self._github.create_incident_issue(title=issue_title, body=issue_body)
            issue_number = issue["number"]
            issue_url = issue["url"]
            logger.info("Created incident issue #%d: %s", issue_number, issue_url)
        except Exception as e:
            logger.error("Failed to create GitHub issue: %s", e)
            return

        # 3. Diagnose
        try:
            self._github.add_issue_label(issue_number, "applying")
            logger.info("Running DiagnosticAgent for issue #%d", issue_number)
            diagnosis = await self._diagnostic.investigate(trigger)
            diagnosed_at = datetime.now(timezone.utc).isoformat()
            logger.info(
                "Diagnosis complete: category=%s severity=%s", diagnosis.category, diagnosis.severity
            )
        except Exception as e:
            logger.error("DiagnosticAgent failed: %s", e)
            self._safe_comment(issue_number, f"Diagnostic agent failed with error: {e}\n\nManual investigation required.")
            self._safe_add_label(issue_number, "failed")
            return

        # 4. Update issue with diagnosis
        diagnosis_comment = self._format_diagnosis_comment(diagnosis)
        self._safe_comment(issue_number, diagnosis_comment)

        # 5. Handle by category
        if diagnosis.category == "transient":
            await self._handle_transient(issue_number, diagnosis)

        elif diagnosis.category == "code":
            await self._handle_code_fix(
                issue_number=issue_number,
                diagnosis=diagnosis,
                trigger=trigger,
                issue_title=issue_title,
                started_at=now,
                diagnosed_at=diagnosed_at,
            )

        elif diagnosis.category == "infrastructure":
            await self._handle_infra_fix(
                issue_number=issue_number,
                diagnosis=diagnosis,
                trigger=trigger,
                issue_title=issue_title,
                started_at=now,
                diagnosed_at=diagnosed_at,
            )

        else:  # unknown
            self._safe_add_label(issue_number, "failed")
            self._safe_comment(
                issue_number,
                "**Category: unknown**\n\nThe automated agent could not determine the root cause with confidence. "
                "Manual investigation is required.\n\n"
                "**Evidence gathered:**\n" + "\n".join(f"- {e}" for e in diagnosis.evidence),
            )

    # ------------------------------------------------------------------
    # Category handlers
    # ------------------------------------------------------------------

    async def _handle_transient(self, issue_number: int, diagnosis) -> None:
        """Close the issue immediately — transient issues self-resolve."""
        logger.info("Transient issue detected — closing issue #%d", issue_number)
        self._safe_comment(
            issue_number,
            f"**Category: transient**\n\n"
            f"This issue appears to be transient and is already resolving.\n\n"
            f"**Root cause:** {diagnosis.root_cause}\n\n"
            "No fix required. Closing this issue.",
        )
        try:
            self._github.close_issue(issue_number, "Closing — transient issue, self-resolved.")
        except Exception as e:
            logger.error("Failed to close transient issue #%d: %s", issue_number, e)

    async def _handle_code_fix(
        self,
        issue_number: int,
        diagnosis,
        trigger: dict,
        issue_title: str,
        started_at: str,
        diagnosed_at: str,
    ) -> None:
        """Apply immediate actions, create code fix PR, wait for health."""
        logger.info("Code fix workflow for issue #%d", issue_number)

        # a. Immediate actions — safe ones run immediately, risky ones need approval
        safe_actions, risky_actions = self._remediation.classify_actions(diagnosis.immediate_actions)

        if safe_actions:
            results = await self._remediation.apply_immediate_actions(safe_actions, kubectl_tools)
            self._safe_comment(issue_number, "**Immediate actions applied:**\n" + "\n".join(results))

        if risky_actions:
            risky_list = "\n".join(f"- {a}" for a in risky_actions)
            self._safe_comment(
                issue_number,
                f"**⚠️ The following actions may cause downtime and require your approval:**\n\n"
                f"{risky_list}\n\n"
                "Add the **`approved`** label to this issue to authorize them.\n"
                "_The agent will wait up to 24 hours._",
            )
            self._safe_add_label(issue_number, "awaiting-approval")
            approved = await self._poll_for_approval(issue_number)
            if not approved:
                self._safe_comment(
                    issue_number,
                    "Approval timeout — risky actions were not applied. Manual intervention required.",
                )
                self._safe_add_label(issue_number, "failed")
                return
            results = await self._remediation.apply_immediate_actions(risky_actions, kubectl_tools)
            self._safe_comment(issue_number, "**Approved actions applied:**\n" + "\n".join(results))

        # b. Code fix PR
        self._safe_add_label(issue_number, "code-fix")
        pr_info = None
        try:
            pr_info = await self._remediation.create_code_fix_pr(diagnosis, issue_number)
            files_list = "\n".join(f"- `{f}`" for f in pr_info.get("files_changed", []))
            self._safe_comment(
                issue_number,
                f"**Code fix PR opened:** [{pr_info['pr_title'] or 'fix PR'}]({pr_info['pr_url']})\n\n"
                f"**Why:** {diagnosis.root_cause}\n\n"
                f"**What changes:**\n{files_list if files_list else '_see PR for details_'}\n\n"
                f"**Proposed fix:** {diagnosis.proposed_fix}\n\n"
                "The issue will close automatically once the PR is merged and the system is confirmed healthy.",
            )
            logger.info("Code fix PR: %s", pr_info["pr_url"])
        except Exception as e:
            logger.error("Failed to create code fix PR: %s", e)
            self._safe_comment(issue_number, f"Failed to create code fix PR: {e}\n\nManual fix required.")
            self._safe_add_label(issue_number, "failed")
            return

        # c. Wait for PR to be merged before health-checking
        merged = await self._poll_for_pr_merge(pr_info["pr_number"], issue_number)
        if not merged:
            self._safe_add_label(issue_number, "failed")
            return

        # d. Wait for health (deployment needs time to roll out after merge)
        self._safe_comment(issue_number, "PR merged — waiting for the deployment to become healthy...")
        fixed_at = datetime.now(timezone.utc).isoformat()
        healthy = await self._health_checker.wait_for_healthy(timeout_minutes=HEALTH_TIMEOUT_MINUTES)
        resolved_at = datetime.now(timezone.utc).isoformat()

        if healthy:
            self._safe_add_label(issue_number, "resolved")
            self._github.close_issue(
                issue_number,
                f"System is healthy. Incident resolved.\n\n"
                f"**Fix applied:** [{pr_info['pr_title'] or 'fix PR'}]({pr_info['pr_url']})",
            )
            await self._create_incident_report_pr(
                issue_number=issue_number,
                issue_title=issue_title,
                diagnosis=diagnosis,
                fix_pr_info=pr_info,
                started_at=started_at,
                diagnosed_at=diagnosed_at,
                fixed_at=fixed_at,
                resolved_at=resolved_at,
            )
        else:
            self._safe_add_label(issue_number, "failed")
            self._safe_comment(
                issue_number,
                f"System did not recover within {HEALTH_TIMEOUT_MINUTES} minutes after the fix was merged. "
                "Manual intervention required.\n\n"
                f"**Fix PR:** {pr_info['pr_url']}",
            )

    async def _handle_infra_fix(
        self,
        issue_number: int,
        diagnosis,
        trigger: dict,
        issue_title: str,
        started_at: str,
        diagnosed_at: str,
    ) -> None:
        """Apply immediate actions, request approval, apply infra fix PR, wait for health."""
        logger.info("Infrastructure fix workflow for issue #%d", issue_number)

        # a. Safe immediate actions run without approval; risky ones are folded into the approval request
        safe_actions, risky_actions = self._remediation.classify_actions(diagnosis.immediate_actions)

        if safe_actions:
            results = await self._remediation.apply_immediate_actions(safe_actions, kubectl_tools)
            self._safe_comment(issue_number, "**Immediate actions applied:**\n" + "\n".join(results))

        # b. Request approval — one approval covers both risky kubectl actions and the infra PR
        self._safe_add_label(issue_number, "infra-fix")
        self._safe_add_label(issue_number, "awaiting-approval")

        risky_section = ""
        if risky_actions:
            risky_list = "\n".join(f"- {a}" for a in risky_actions)
            risky_section = (
                f"\n\n**⚠️ Actions that may cause downtime (require your approval):**\n{risky_list}"
            )

        approval_comment = (
            f"**Infrastructure fix required — awaiting DevOps approval**\n\n"
            f"**Root cause:** {diagnosis.root_cause}\n\n"
            f"**Proposed fix:** {diagnosis.proposed_fix}"
            f"{risky_section}\n\n"
            "Add the **`approved`** label to authorize all of the above.\n"
            "_The agent will poll for approval for up to 24 hours._"
        )
        self._safe_comment(issue_number, approval_comment)

        # c. Poll for approval
        approved = await self._poll_for_approval(issue_number)

        if not approved:
            self._safe_comment(
                issue_number,
                "Approval timeout reached (24 hours). No action taken. Manual intervention required.",
            )
            self._safe_add_label(issue_number, "failed")
            return

        logger.info("Issue #%d approved — applying risky actions then creating infra fix PR", issue_number)
        self._safe_comment(issue_number, "Approval received!")

        # d. Execute approved risky kubectl actions
        if risky_actions:
            results = await self._remediation.apply_immediate_actions(risky_actions, kubectl_tools)
            self._safe_comment(issue_number, "**Approved actions applied:**\n" + "\n".join(results))

        # e. Create infra fix PR
        pr_info = None
        try:
            pr_info = await self._remediation.create_infra_fix_pr(diagnosis, issue_number)
            files_list = "\n".join(f"- `{f}`" for f in pr_info.get("files_changed", []))
            self._safe_comment(
                issue_number,
                f"**Infrastructure fix PR opened:** [{pr_info['pr_title'] or 'infra fix PR'}]({pr_info['pr_url']})\n\n"
                f"**Why:** {diagnosis.root_cause}\n\n"
                f"**What changes:**\n{files_list if files_list else '_see PR for details_'}\n\n"
                f"**Proposed fix:** {diagnosis.proposed_fix}\n\n"
                "The issue will close automatically once the PR is merged and the system is confirmed healthy.",
            )
        except Exception as e:
            logger.error("Failed to create infra fix PR: %s", e)
            self._safe_comment(issue_number, f"Failed to create infra fix PR: {e}\n\nManual fix required.")
            self._safe_add_label(issue_number, "failed")
            return

        # f. Wait for PR to be merged
        merged = await self._poll_for_pr_merge(pr_info["pr_number"], issue_number)
        if not merged:
            self._safe_add_label(issue_number, "failed")
            return

        # g. Wait for health
        self._safe_comment(issue_number, "PR merged — waiting for the deployment to become healthy...")
        fixed_at = datetime.now(timezone.utc).isoformat()
        healthy = await self._health_checker.wait_for_healthy(timeout_minutes=HEALTH_TIMEOUT_MINUTES)
        resolved_at = datetime.now(timezone.utc).isoformat()

        if healthy:
            self._safe_add_label(issue_number, "resolved")
            self._github.close_issue(
                issue_number,
                f"System is healthy. Incident resolved.\n\n"
                f"**Fix applied:** [{pr_info['pr_title'] or 'infra fix PR'}]({pr_info['pr_url']})",
            )
            await self._create_incident_report_pr(
                issue_number=issue_number,
                issue_title=issue_title,
                diagnosis=diagnosis,
                fix_pr_info=pr_info,
                started_at=started_at,
                diagnosed_at=diagnosed_at,
                fixed_at=fixed_at,
                resolved_at=resolved_at,
            )
        else:
            self._safe_add_label(issue_number, "failed")
            self._safe_comment(
                issue_number,
                f"System did not recover within {HEALTH_TIMEOUT_MINUTES} minutes after the fix was merged. "
                "Manual intervention required.\n\n"
                f"**Fix PR:** {pr_info['pr_url']}",
            )

    # ------------------------------------------------------------------
    # GitHub Issue comment interaction
    # ------------------------------------------------------------------

    async def poll_issue_comments(self) -> None:
        """Background loop: check active incident issues for new operator comments
        and reply via the chat agent.  Runs continuously while the pod is alive.
        """
        logger.info("GitHub comment polling started (interval: %ds)", COMMENT_POLL_SECONDS)
        while True:
            await asyncio.sleep(COMMENT_POLL_SECONDS)
            try:
                await self._process_new_comments()
            except Exception as e:
                logger.warning("Comment poll error: %s", e)

    async def _process_new_comments(self) -> None:
        # Watch ALL open issues — both agent-created incidents and user-opened ones
        all_issues = self._github.get_all_open_issues()
        for issue_meta in all_issues:
            issue_number = issue_meta["number"]
            issue_url = issue_meta["url"]
            issue_author = issue_meta["author"]

            try:
                issue = self._github._repo.get_issue(issue_number)
                comments = list(issue.get_comments())
            except Exception as e:
                logger.warning("Could not fetch comments for issue #%d: %s", issue_number, e)
                continue

            issue_context = {
                "issue_number": issue_number,
                "issue_url": issue_url,
                "title": issue.title,
                "labels": [lb.name for lb in issue.get_labels()],
            }

            # Greet new user-created issues the agent hasn't seen before
            is_bot_issue = issue_author == _BOT_LOGIN
            if issue_number not in self._greeted_issues and not is_bot_issue:
                self._greeted_issues.add(issue_number)
                # Mark the comment watermark now so we don't double-process the body
                self._last_seen_comment[issue_number] = (
                    max((c.id for c in comments), default=0)
                )
                logger.info("Greeting new user issue #%d: %s", issue_number, issue.title)
                await self._greet_issue(issue_number, issue_meta, issue_context)
                continue  # let the greeting land before processing comments

            # Mark agent-created issues as greeted on first sight (no greeting needed)
            if issue_number not in self._greeted_issues:
                self._greeted_issues.add(issue_number)
                self._last_seen_comment[issue_number] = (
                    max((c.id for c in comments), default=0)
                )
                continue

            # Process new comments from non-bot users
            last_seen = self._last_seen_comment.get(issue_number, 0)
            new_comments = [
                c for c in comments
                if c.id > last_seen and c.user.login != _BOT_LOGIN
            ]

            if not new_comments:
                continue

            self._last_seen_comment[issue_number] = max(c.id for c in comments)

            for comment in new_comments:
                logger.info(
                    "New comment on issue #%d from %s: %s",
                    issue_number, comment.user.login, comment.body[:80],
                )
                try:
                    reply = await self._chat.chat(
                        session_id=f"issue-{issue_number}",
                        user_message=comment.body,
                        incident_context=issue_context,
                    )
                    self._safe_comment(issue_number, f"🤖 {reply}")
                except Exception as e:
                    logger.error(
                        "Chat agent failed for issue #%d comment %d: %s",
                        issue_number, comment.id, e,
                    )
                    self._safe_comment(
                        issue_number,
                        f"⚠️ Agent error while processing your comment: `{e}`",
                    )

    async def _greet_issue(self, issue_number: int, issue_meta: dict, issue_context: dict) -> None:
        """Post an initial response to a user-opened issue using the title + body as the prompt."""
        prompt = f"**Issue title:** {issue_meta['title']}\n\n{issue_meta.get('body', '')}".strip()
        if not prompt:
            return
        try:
            reply = await self._chat.chat(
                session_id=f"issue-{issue_number}",
                user_message=prompt,
                incident_context=issue_context,
            )
            self._safe_comment(issue_number, f"🤖 {reply}")
        except Exception as e:
            logger.error("Greeting failed for issue #%d: %s", issue_number, e)
            self._safe_comment(issue_number, f"⚠️ Could not process this issue: `{e}`")

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    async def startup_recovery(self) -> None:
        """Detect incidents stuck mid-workflow (due to pod restart) and resume them."""
        logger.info("startup_recovery: scanning for stuck incidents")
        try:
            open_incidents = self._github.get_open_incidents()
        except Exception as e:
            logger.warning("startup_recovery: cannot list incidents: %s", e)
            return

        for incident in open_incidents:
            labels = set(incident.get("labels", []))
            issue_number = incident["number"]

            stuck_code = "code-fix" in labels and "resolved" not in labels
            stuck_infra = "infra-fix" in labels and "resolved" not in labels

            if not (stuck_code or stuck_infra):
                continue

            # Check whether a PR comment was already posted by the bot
            try:
                issue_obj = self._github._repo.get_issue(issue_number)
                comments = list(issue_obj.get_comments())
                pr_already_created = any(
                    _BOT_LOGIN
                    and c.user.login == _BOT_LOGIN
                    and "PR opened:" in (c.body or "")
                    for c in comments
                )
            except Exception as e:
                logger.warning("startup_recovery: comment check failed for #%d: %s", issue_number, e)
                continue

            fix_type = "code-fix" if stuck_code else "infra-fix"

            # Clear any stale 'failed' label before retrying
            if "failed" in labels:
                try:
                    self._github._repo.get_issue(issue_number).remove_from_labels("failed")
                except Exception:
                    pass

            if pr_already_created:
                logger.info(
                    "startup_recovery: issue #%d has PR — resuming health monitoring",
                    issue_number,
                )
                asyncio.create_task(self._resume_health_monitoring(incident, fix_type))
            else:
                logger.info(
                    "startup_recovery: issue #%d stuck in %s without PR — re-diagnosing",
                    issue_number, fix_type,
                )
                asyncio.create_task(self._recover_stuck_incident(incident, fix_type))

    async def _recover_stuck_incident(self, incident: dict, fix_type: str) -> None:
        """Re-diagnose and continue the workflow for an interrupted incident."""
        issue_number = incident["number"]
        issue_title = incident["title"]
        started_at = incident.get("created_at", datetime.now(timezone.utc).isoformat())

        self._safe_comment(
            issue_number,
            "**Agent pod restarted** — the previous workflow was interrupted. Resuming investigation...",
        )

        trigger = self._reconstruct_trigger(incident)

        try:
            diagnosis = await self._diagnostic.investigate(trigger)
            diagnosed_at = datetime.now(timezone.utc).isoformat()
            logger.info(
                "startup_recovery: diagnosis for #%d: category=%s severity=%s",
                issue_number, diagnosis.category, diagnosis.severity,
            )
        except Exception as e:
            logger.error("startup_recovery: diagnosis failed for #%d: %s", issue_number, e)
            self._safe_comment(
                issue_number,
                f"Recovery diagnosis failed: `{e}`\n\nManual investigation required.",
            )
            self._safe_add_label(issue_number, "failed")
            return

        self._safe_comment(issue_number, self._format_diagnosis_comment(diagnosis))

        if fix_type == "code-fix":
            await self._handle_code_fix(
                issue_number=issue_number,
                diagnosis=diagnosis,
                trigger=trigger,
                issue_title=issue_title,
                started_at=started_at,
                diagnosed_at=diagnosed_at,
            )
        else:
            await self._handle_infra_fix(
                issue_number=issue_number,
                diagnosis=diagnosis,
                trigger=trigger,
                issue_title=issue_title,
                started_at=started_at,
                diagnosed_at=diagnosed_at,
            )

    async def _resume_health_monitoring(self, incident: dict, fix_type: str) -> None:
        """Resume after a pod restart when the fix PR already exists.

        Polls for PR merge if not yet merged, then waits for health and closes the issue.
        """
        from agents.diagnostic import DiagnosisResult

        issue_number = incident["number"]
        issue_title = incident["title"]
        started_at = incident.get("created_at", datetime.now(timezone.utc).isoformat())

        info = self._extract_incident_info_from_comments(issue_number)
        pr_number = info.get("fix_pr_number")
        if not pr_number:
            logger.warning("startup_recovery: no PR number in comments for #%d", issue_number)
            self._safe_comment(
                issue_number,
                "**Agent restarted** — could not find fix PR number in comments. Manual intervention required.",
            )
            self._safe_add_label(issue_number, "failed")
            return

        fix_pr_info = {
            "pr_number": pr_number,
            "pr_url": info["fix_pr_url"],
            "pr_title": info["fix_pr_title"],
        }

        # Check current PR state
        try:
            pr_state = self._github.get_pr_state(pr_number)
        except Exception as e:
            logger.warning("startup_recovery: could not get PR #%d state: %s", pr_number, e)
            pr_state = {"merged": False, "state": "open"}

        if not pr_state.get("merged"):
            if pr_state.get("state") == "closed":
                self._safe_comment(
                    issue_number,
                    f"**Agent restarted** — PR #{pr_number} was closed without merging. Manual fix required.",
                )
                self._safe_add_label(issue_number, "failed")
                return
            self._safe_comment(
                issue_number,
                "**Agent restarted** — still waiting for the fix PR to be merged...",
            )
            merged = await self._poll_for_pr_merge(pr_number, issue_number)
            if not merged:
                self._safe_add_label(issue_number, "failed")
                return

        self._safe_comment(
            issue_number,
            "**Agent restarted** — fix PR is merged. Verifying system health...",
        )

        diagnosis = DiagnosisResult(
            root_cause=info["root_cause"],
            category=info["category"] if info["category"] in ("infrastructure", "code", "transient", "unknown") else "code",
            severity=info["severity"] if info["severity"] in ("critical", "high", "medium", "low") else "high",
            affected_components=info["affected_components"] or ["unknown"],
            proposed_fix=info["proposed_fix"],
            proposed_fix_details={"files_to_change": []},
            immediate_actions=info["immediate_actions"],
            evidence=info["evidence"],
            can_auto_remediate=False,
        )

        fixed_at = datetime.now(timezone.utc).isoformat()
        healthy = await self._health_checker.wait_for_healthy(timeout_minutes=HEALTH_TIMEOUT_MINUTES)
        resolved_at = datetime.now(timezone.utc).isoformat()

        if healthy:
            self._safe_add_label(issue_number, "resolved")
            self._github.close_issue(
                issue_number,
                f"System is healthy. Incident resolved.\n\n"
                f"**Fix applied:** [{fix_pr_info['pr_title'] or 'fix PR'}]({fix_pr_info['pr_url']})",
            )
            await self._create_incident_report_pr(
                issue_number=issue_number,
                issue_title=issue_title,
                diagnosis=diagnosis,
                fix_pr_info=fix_pr_info,
                started_at=started_at,
                diagnosed_at=fixed_at,
                fixed_at=fixed_at,
                resolved_at=resolved_at,
            )
        else:
            self._safe_add_label(issue_number, "failed")
            self._safe_comment(
                issue_number,
                f"System did not recover within {HEALTH_TIMEOUT_MINUTES} minutes after the fix was merged. "
                f"Manual intervention required.\n\n**Fix PR:** {fix_pr_info['pr_url']}",
            )

    @staticmethod
    def _reconstruct_trigger(incident: dict) -> dict:
        """Parse the original trigger JSON embedded in the issue body."""
        body = incident.get("body", "")
        match = re.search(r"```json\s*(.*?)\s*```", body, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return {
            "source": "pod_restart_recovery",
            "issue_number": incident["number"],
            "title": incident["title"],
            "timestamp": incident.get("created_at", datetime.now(timezone.utc).isoformat()),
        }

    # ------------------------------------------------------------------
    # Periodic check
    # ------------------------------------------------------------------

    async def run_periodic_check(self) -> None:
        """Run a health check and trigger handle_trigger if unhealthy."""
        logger.info("Running periodic health check")
        try:
            status = await self._health_checker.check_all()
            if not status.healthy:
                logger.warning("Periodic check: unhealthy — %s", status.summary)
                trigger = {
                    "source": "periodic_check",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "health_status": status.dict(),
                }
                await self.handle_trigger(trigger)
            else:
                logger.info("Periodic check: all healthy")
        except Exception as e:
            logger.error("Periodic health check failed: %s", e)

        # Create incident report PRs for recently human-closed incidents
        await self._check_for_closed_incidents_needing_reports()

    # ------------------------------------------------------------------
    # PR merge polling
    # ------------------------------------------------------------------

    async def _poll_for_pr_merge(self, pr_number: int, issue_number: int) -> bool:
        """Poll until the PR is merged or closed without merging.

        Returns True when merged, False if closed unmerged or timeout.
        """
        self._safe_comment(
            issue_number,
            f"Waiting for [PR #{pr_number}](../../pull/{pr_number}) to be merged "
            "before verifying system health...",
        )
        max_polls = int((APPROVAL_TIMEOUT_HOURS * 3600) / APPROVAL_POLL_SECONDS)
        for _ in range(max_polls):
            try:
                state = self._github.get_pr_state(pr_number)
                if state["merged"]:
                    logger.info("PR #%d merged", pr_number)
                    return True
                if state["state"] == "closed":
                    self._safe_comment(
                        issue_number,
                        f"PR #{pr_number} was closed without merging. Manual fix required.",
                    )
                    return False
            except Exception as e:
                logger.warning("Error polling PR #%d: %s", pr_number, e)
            await asyncio.sleep(APPROVAL_POLL_SECONDS)
        self._safe_comment(
            issue_number,
            f"Timed out waiting for PR #{pr_number} to be merged after {APPROVAL_TIMEOUT_HOURS}h.",
        )
        return False

    # ------------------------------------------------------------------
    # Incident report PR
    # ------------------------------------------------------------------

    async def _create_incident_report_pr(
        self,
        issue_number: int,
        issue_title: str,
        diagnosis,
        fix_pr_info: dict,
        started_at: str,
        diagnosed_at: str,
        fixed_at: str,
        resolved_at: str,
    ) -> None:
        """Open a separate PR with a markdown incident report including the fix diff."""
        if issue_number in self._incident_reports_created:
            return
        self._incident_reports_created.add(issue_number)

        diff_files: list[dict] = []
        pr_number = fix_pr_info.get("pr_number")
        if pr_number:
            try:
                diff_files = self._github.get_pr_diff_files(pr_number)
            except Exception as e:
                logger.warning("Could not fetch diff for PR #%d: %s", pr_number, e)

        incident_doc = self._remediation.generate_incident_doc(
            incident={
                "title": issue_title,
                "started_at": started_at,
                "diagnosed_at": diagnosed_at,
                "fixed_at": fixed_at,
                "resolved_at": resolved_at,
            },
            diagnosis=diagnosis,
            fix_result=f"[{fix_pr_info.get('pr_title') or 'fix PR'}]({fix_pr_info.get('pr_url', '')})",
            fix_pr_url=fix_pr_info.get("pr_url", ""),
            fix_pr_title=fix_pr_info.get("pr_title", ""),
            diff_files=diff_files,
        )

        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        branch_name = f"docs/incident-{timestamp}"
        doc_path = f"incidents/{timestamp}-incident-{issue_number}.md"

        try:
            self._github.create_branch(branch_name)
            self._github.commit_files(
                branch=branch_name,
                files={doc_path: incident_doc},
                message=f"docs: incident report for issue #{issue_number}",
            )
            report_pr = self._github.create_pr(
                title=f"docs: incident report — {issue_title[:60]}",
                body=(
                    f"Automated incident postmortem for issue #{issue_number}.\n\n"
                    f"**Fix PR:** {fix_pr_info.get('pr_url', 'N/A')}\n\n"
                    f"Addresses #{issue_number}"
                ),
                head_branch=branch_name,
            )
            self._safe_add_label(issue_number, "incident-report")
            self._safe_comment(
                issue_number,
                f"**Incident report PR:** [{doc_path}]({report_pr['url']})\n\n"
                "The postmortem document includes the fix PR diff and full timeline.",
            )
            logger.info("Incident report PR: %s", report_pr["url"])
        except Exception as e:
            logger.error("Failed to create incident report PR for #%d: %s", issue_number, e)

    # ------------------------------------------------------------------
    # Human-close detection
    # ------------------------------------------------------------------

    async def _check_for_closed_incidents_needing_reports(self) -> None:
        """Find recently human-closed incidents and create report PRs for them."""
        try:
            closed = self._github.get_recently_closed_incidents(since_hours=2)
        except Exception as e:
            logger.warning("Could not check recently closed incidents: %s", e)
            return

        for incident in closed:
            issue_number = incident["number"]
            labels = set(incident.get("labels", []))

            if "incident-report" in labels:
                continue
            if issue_number in self._incident_reports_created:
                continue
            # Skip issues closed as "not_planned" (duplicates, won't-fix, etc.)
            if incident.get("state_reason") == "not_planned":
                continue

            logger.info("Detected human-closed incident #%d — creating report", issue_number)
            self._incident_reports_created.add(issue_number)
            asyncio.create_task(self._create_report_for_human_closed_incident(incident))

    async def _create_report_for_human_closed_incident(self, incident: dict) -> None:
        """Reconstruct incident context from issue comments and open a report PR."""
        from agents.diagnostic import DiagnosisResult

        issue_number = incident["number"]
        info = self._extract_incident_info_from_comments(issue_number)

        diagnosis = DiagnosisResult(
            root_cause=info["root_cause"],
            category=info["category"],
            severity=info["severity"],
            affected_components=info["affected_components"],
            proposed_fix=info["proposed_fix"],
            proposed_fix_details={"files_to_change": []},
            immediate_actions=info["immediate_actions"],
            evidence=info["evidence"],
            can_auto_remediate=False,
        )
        fix_pr_info = {
            "pr_number": info["fix_pr_number"],
            "pr_url": info["fix_pr_url"],
            "pr_title": info["fix_pr_title"],
        }
        closed_at = incident.get("closed_at", datetime.now(timezone.utc).isoformat())

        await self._create_incident_report_pr(
            issue_number=issue_number,
            issue_title=incident["title"],
            diagnosis=diagnosis,
            fix_pr_info=fix_pr_info,
            started_at=incident.get("created_at", closed_at),
            diagnosed_at=closed_at,
            fixed_at=closed_at,
            resolved_at=closed_at,
        )

    def _extract_incident_info_from_comments(self, issue_number: int) -> dict:
        """Parse issue comments to reconstruct incident metadata for the report."""
        info: dict = {
            "root_cause": "See issue comments for details",
            "category": "unknown",
            "severity": "unknown",
            "affected_components": [],
            "proposed_fix": "",
            "immediate_actions": [],
            "evidence": [],
            "fix_pr_number": None,
            "fix_pr_url": "",
            "fix_pr_title": "",
        }
        try:
            issue = self._github._repo.get_issue(issue_number)
            for comment in issue.get_comments():
                body = comment.body or ""

                if "## Diagnosis Complete" in body:
                    m = re.search(r"\*\*Root cause:\*\*\n(.+?)(?:\n\n|\Z)", body, re.DOTALL)
                    if m:
                        info["root_cause"] = m.group(1).strip()
                    m = re.search(r"\*\*Category:\*\* (\S+)", body)
                    if m:
                        info["category"] = m.group(1)
                    m = re.search(r"\*\*Severity:\*\* (\S+)", body)
                    if m:
                        info["severity"] = m.group(1)
                    m = re.search(r"\*\*Proposed fix:\*\*\n(.+?)(?:\n\n|\Z)", body, re.DOTALL)
                    if m:
                        info["proposed_fix"] = m.group(1).strip()
                    m = re.search(r"\*\*Affected components:\*\* (.+)", body)
                    if m:
                        info["affected_components"] = [c.strip() for c in m.group(1).split(",")]
                    m = re.search(r"\*\*Evidence:\*\*\n((?:- .+\n?)+)", body)
                    if m:
                        info["evidence"] = [
                            ln.lstrip("- ").strip()
                            for ln in m.group(1).strip().splitlines()
                            if ln.startswith("- ")
                        ]
                    m = re.search(r"\*\*Immediate actions:\*\*\n((?:- .+\n?)+)", body)
                    if m:
                        info["immediate_actions"] = [
                            ln.lstrip("- ").strip()
                            for ln in m.group(1).strip().splitlines()
                            if ln.startswith("- ")
                        ]

                for pattern in [
                    r"\*\*Code fix PR opened:\*\* \[(.+?)\]\((.+?)\)",
                    r"\*\*Infrastructure fix PR opened:\*\* \[(.+?)\]\((.+?)\)",
                ]:
                    m = re.search(pattern, body)
                    if m:
                        info["fix_pr_title"] = m.group(1)
                        info["fix_pr_url"] = m.group(2)
                        num_m = re.search(r"/pull/(\d+)", info["fix_pr_url"])
                        if num_m:
                            info["fix_pr_number"] = int(num_m.group(1))
        except Exception as e:
            logger.warning("Could not parse comments for issue #%d: %s", issue_number, e)
        return info

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _poll_for_approval(self, issue_number: int) -> bool:
        """Poll the GitHub issue for the 'approved' label.

        Returns True when approved, False on timeout.
        """
        max_polls = int((APPROVAL_TIMEOUT_HOURS * 3600) / APPROVAL_POLL_SECONDS)
        for _ in range(max_polls):
            try:
                if self._github.check_issue_approved(issue_number):
                    return True
            except Exception as e:
                logger.warning("Error checking approval for issue #%d: %s", issue_number, e)
            await asyncio.sleep(APPROVAL_POLL_SECONDS)
        return False

    def _safe_comment(self, issue_number: int, body: str) -> None:
        try:
            issue = self._github._repo.get_issue(issue_number)
            issue.create_comment(body)
        except Exception as e:
            logger.error("Failed to comment on issue #%d: %s", issue_number, e)

    def _safe_add_label(self, issue_number: int, label: str) -> None:
        try:
            self._github.add_issue_label(issue_number, label)
        except Exception as e:
            logger.error("Failed to add label '%s' to issue #%d: %s", label, issue_number, e)

    @staticmethod
    def _format_issue_title(trigger: dict) -> str:
        source = trigger.get("source", "unknown")
        if source == "alertmanager":
            alerts = trigger.get("alerts", [])
            if alerts:
                name = alerts[0].get("labels", {}).get("alertname", "Unknown alert")
                return f"Incident: {name}"
        if source == "argocd":
            app = trigger.get("app", {})
            app_name = app.get("metadata", {}).get("name", "unknown") if app else "unknown"
            return f"Incident: ArgoCD app {app_name} degraded"
        if source == "periodic_check":
            return "Incident: Periodic health check failure"
        return "Incident: Platform health issue detected"

    @staticmethod
    def _format_issue_body(trigger: dict, timestamp: str) -> str:
        return (
            f"**Automated incident detected at {timestamp}**\n\n"
            f"**Source:** {trigger.get('source', 'unknown')}\n\n"
            "---\n\n"
            "## Trigger Payload\n\n"
            f"```json\n{json.dumps(trigger, indent=2, default=str)}\n```\n\n"
            "---\n\n"
            "_This issue is being investigated by the automated observability agent._"
        )

    @staticmethod
    def _format_diagnosis_comment(diagnosis) -> str:
        evidence_lines = "\n".join(f"- {e}" for e in diagnosis.evidence)
        actions_lines = "\n".join(f"- {a}" for a in diagnosis.immediate_actions)
        return (
            f"## Diagnosis Complete\n\n"
            f"**Category:** {diagnosis.category}\n"
            f"**Severity:** {diagnosis.severity}\n"
            f"**Can auto-remediate:** {'Yes' if diagnosis.can_auto_remediate else 'No'}\n\n"
            f"**Root cause:**\n{diagnosis.root_cause}\n\n"
            f"**Affected components:** {', '.join(diagnosis.affected_components)}\n\n"
            f"**Proposed fix:**\n{diagnosis.proposed_fix}\n\n"
            f"**Evidence:**\n{evidence_lines}\n\n"
            f"**Immediate actions:**\n{actions_lines if actions_lines else 'None'}"
        )
