"""GitHub API tools using PyGithub."""

import os
from datetime import datetime

from github import Github, GithubException
from github.InputGitTreeElement import InputGitTreeElement

# ---------------------------------------------------------------------------
# Standalone read-only tool functions used by DiagnosticAgent
# ---------------------------------------------------------------------------

_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
_GITHUB_REPO = os.environ.get("GITHUB_REPO", "EXAMPLE_REPO")


def read_file(path: str, branch: str = "main") -> dict:
    """Read a file from the repository.

    Args:
        path: File path relative to repo root, e.g. "app/main.go".
        branch: Branch to read from (default: main).

    Returns:
        {path, content, size} or {error}.
    """
    try:
        gh = Github(_GITHUB_TOKEN)
        repo = gh.get_repo(_GITHUB_REPO)
        contents = repo.get_contents(path, ref=branch)
        if isinstance(contents, list):
            return {"error": f"{path} is a directory — use list_directory instead"}
        content = contents.decoded_content.decode("utf-8")
        return {"path": path, "content": content, "size": contents.size}
    except GithubException as e:
        return {"error": f"GitHub {e.status}: {e.data.get('message', str(e))}"}
    except Exception as e:
        return {"error": str(e)}


def list_directory(path: str = "", branch: str = "main") -> dict:
    """List files and directories at a path in the repository.

    Args:
        path: Directory path relative to repo root (empty string for root).
        branch: Branch to read from (default: main).

    Returns:
        {path, entries: [{name, type, path}]} or {error}.
    """
    try:
        gh = Github(_GITHUB_TOKEN)
        repo = gh.get_repo(_GITHUB_REPO)
        contents = repo.get_contents(path or "/", ref=branch)
        if not isinstance(contents, list):
            return {"error": f"{path} is a file — use read_file instead"}
        entries = [
            {"name": c.name, "type": c.type, "path": c.path}
            for c in sorted(contents, key=lambda c: (c.type != "dir", c.name))
        ]
        return {"path": path or "/", "entries": entries}
    except GithubException as e:
        return {"error": f"GitHub {e.status}: {e.data.get('message', str(e))}"}
    except Exception as e:
        return {"error": str(e)}


# Label definitions: name -> color
LABEL_DEFINITIONS = {
    "incident": "d73a4a",
    "awaiting-approval": "e4e669",
    "approved": "0075ca",
    "applying": "cfd3d7",
    "resolved": "0e8a16",
    "failed": "b60205",
    "code-fix": "bfd4f2",
    "infra-fix": "d4c5f9",
    "incident-report": "1d76db",
}


class GitHubTools:
    """Wraps PyGithub for incident and PR management."""

    def __init__(self, token: str, repo_full_name: str):
        self._gh = Github(token)
        self._repo = self._gh.get_repo(repo_full_name)

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------

    def ensure_labels_exist(self) -> None:
        """Create any missing labels needed by the incident workflow."""
        existing = {label.name for label in self._repo.get_labels()}
        for name, color in LABEL_DEFINITIONS.items():
            if name not in existing:
                try:
                    self._repo.create_label(name=name, color=color)
                except GithubException:
                    pass  # race condition — another instance created it

    def add_issue_label(self, issue_number: int, label: str) -> None:
        """Add a single label to an issue.

        Args:
            issue_number: GitHub issue number.
            label: Label name to add.
        """
        issue = self._repo.get_issue(issue_number)
        issue.add_to_labels(label)

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    def create_incident_issue(self, title: str, body: str) -> dict:
        """Create a new GitHub issue tagged as an incident.

        Args:
            title: Issue title.
            body: Markdown body.

        Returns:
            {number, url}.
        """
        self.ensure_labels_exist()
        issue = self._repo.create_issue(title=title, body=body, labels=["incident"])
        return {"number": issue.number, "url": issue.html_url}

    def check_issue_approved(self, issue_number: int) -> bool:
        """Return True if the issue has the 'approved' label.

        Args:
            issue_number: GitHub issue number.
        """
        issue = self._repo.get_issue(issue_number)
        return any(label.name == "approved" for label in issue.get_labels())

    def close_issue(self, issue_number: int, comment: str) -> None:
        """Add a comment and close the issue.

        Args:
            issue_number: GitHub issue number.
            comment: Closing comment text.
        """
        issue = self._repo.get_issue(issue_number)
        issue.create_comment(comment)
        issue.edit(state="closed")

    def get_open_incidents(self) -> list:
        """Return open issues with the 'incident' label."""
        return self._get_open_issues(label="incident")

    def get_all_open_issues(self) -> list:
        """Return all open issues regardless of label."""
        return self._get_open_issues()

    def _get_open_issues(self, label: str | None = None) -> list:
        kwargs = {"state": "open"}
        if label:
            kwargs["labels"] = [label]
        issues = self._repo.get_issues(**kwargs)
        return [
            {
                "number": i.number,
                "title": i.title,
                "url": i.html_url,
                "body": i.body or "",
                "author": i.user.login,
                "labels": [lb.name for lb in i.get_labels()],
                "created_at": str(i.created_at),
            }
            for i in issues
        ]

    # ------------------------------------------------------------------
    # Branches and commits
    # ------------------------------------------------------------------

    def create_branch(self, branch_name: str, base: str = "main") -> str:
        """Create a new branch from the specified base.

        Args:
            branch_name: New branch name.
            base: Base branch name (default: main).

        Returns:
            The branch name created.
        """
        source = self._repo.get_branch(base)
        self._repo.create_git_ref(
            ref=f"refs/heads/{branch_name}",
            sha=source.commit.sha,
        )
        return branch_name

    def commit_files(self, branch: str, files: dict, message: str) -> str:
        """Commit one or more files to an existing branch.

        Args:
            branch: Target branch name.
            files: Mapping of {file_path: file_content_str}.
            message: Commit message.

        Returns:
            The commit SHA.
        """
        ref = self._repo.get_git_ref(f"heads/{branch}")
        base_tree = self._repo.get_git_tree(ref.object.sha, recursive=True)

        elements = []
        for path, content in files.items():
            blob = self._repo.create_git_blob(content, "utf-8")
            elements.append(
                InputGitTreeElement(
                    path=path,
                    mode="100644",
                    type="blob",
                    sha=blob.sha,
                )
            )

        new_tree = self._repo.create_git_tree(elements, base_tree)
        parent_commit = self._repo.get_git_commit(ref.object.sha)
        new_commit = self._repo.create_git_commit(
            message=message,
            tree=new_tree,
            parents=[parent_commit],
        )
        ref.edit(new_commit.sha)
        return new_commit.sha

    # ------------------------------------------------------------------
    # Pull requests
    # ------------------------------------------------------------------

    def create_pr(
        self,
        title: str,
        body: str,
        head_branch: str,
        base: str = "main",
    ) -> dict:
        """Open a pull request.

        Args:
            title: PR title.
            body: PR description (markdown).
            head_branch: Source branch.
            base: Target branch (default: main).

        Returns:
            {number, url}.
        """
        pr = self._repo.create_pull(
            title=title,
            body=body,
            head=head_branch,
            base=base,
        )
        return {"number": pr.number, "url": pr.html_url}

    # ------------------------------------------------------------------
    # Pull request state and diff
    # ------------------------------------------------------------------

    def get_pr_state(self, pr_number: int) -> dict:
        """Return merge state and status of a pull request.

        Returns:
            {merged, state, merge_commit_sha}
        """
        pr = self._repo.get_pull(pr_number)
        return {
            "merged": pr.merged,
            "state": pr.state,
            "merge_commit_sha": pr.merge_commit_sha,
        }

    def get_pr_diff_files(self, pr_number: int) -> list[dict]:
        """Return changed files with diffs for a pull request.

        Returns:
            List of {filename, additions, deletions, patch}.
        """
        pr = self._repo.get_pull(pr_number)
        return [
            {
                "filename": f.filename,
                "additions": f.additions,
                "deletions": f.deletions,
                "patch": f.patch or "",
            }
            for f in pr.get_files()
        ]

    # ------------------------------------------------------------------
    # Closed incidents
    # ------------------------------------------------------------------

    def get_recently_closed_incidents(self, since_hours: int = 2) -> list[dict]:
        """Return closed issues with the 'incident' label updated in the last N hours.

        Args:
            since_hours: Look back window in hours.

        Returns:
            List of issue dicts including state_reason where available.
        """
        from datetime import timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        issues = self._repo.get_issues(state="closed", labels=["incident"], since=cutoff)
        results = []
        for i in issues:
            if i.state != "closed":
                continue
            results.append({
                "number": i.number,
                "title": i.title,
                "url": i.html_url,
                "body": i.body or "",
                "labels": [lb.name for lb in i.get_labels()],
                "created_at": str(i.created_at),
                "closed_at": str(i.closed_at),
                "state_reason": getattr(i, "state_reason", None),
            })
        return results

    # ------------------------------------------------------------------
    # File access
    # ------------------------------------------------------------------

    def get_file_content(self, path: str, branch: str = "main") -> str:
        """Read a file's content from the repository.

        Args:
            path: File path relative to the repository root.
            branch: Branch to read from.

        Returns:
            File content as a string.

        Raises:
            GithubException if the file does not exist.
        """
        file_contents = self._repo.get_contents(path, ref=branch)
        if isinstance(file_contents, list):
            raise ValueError(f"{path} is a directory, not a file")
        return file_contents.decoded_content.decode("utf-8")
