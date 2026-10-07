"""Read-only GitHub transport. Authentication stays with the installed gh CLI."""
import json
import os
import re
import subprocess
import time
from urllib.parse import quote

from .model import WatchError, identity_state

PR_QUERY = """query($owner:String!,$name:String!,$number:Int!){
 repository(owner:$owner,name:$name){pullRequest(number:$number){
  headRefOid baseRefOid baseRefName state url
  potentialMergeCommit{oid parents(first:2){nodes{oid}}}
  baseRef{branchProtectionRule{requiresStatusChecks requiredStatusChecks{context app{databaseId}}}}
 }}}
"""


class GitHub:
    def __init__(self, host="github.com", executable=None, *, deadline=None):
        self.host, self.executable = host, executable or os.environ.get("PR_WATCH_GH", "gh")
        self.deadline = deadline

    def api(self, endpoint, *, paginate=False, fields=None):
        args = [self.executable, "api", "--hostname", self.host, endpoint]
        if paginate:
            args += ["--paginate", "--slurp"]
        for key, value in (fields or {}).items():
            args += ["-F", f"{key}={value}"]
        env = {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_PAGER": "cat"}
        remaining = 20 if self.deadline is None else min(20, self.deadline - time.time())
        if remaining <= 0:
            raise WatchError("Watch deadline elapsed during GitHub observation")
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=remaining, env=env)
        except subprocess.TimeoutExpired as exc:
            raise WatchError("GitHub request timed out", retryable=True) from exc
        except OSError as exc:
            raise WatchError(f"Cannot execute gh: {exc}") from exc
        if proc.returncode:
            message = proc.stderr.strip()[:1200] or "GitHub request failed"
            code = re.search(r"HTTP (\d{3})", message)
            retryable = not code or int(code[1]) in (408, 429) or int(code[1]) >= 500 or "rate limit" in message.lower()
            raise WatchError(message, retryable=retryable)
        try:
            data = json.loads(proc.stdout)
        except ValueError as exc:
            raise WatchError("GitHub returned invalid JSON", retryable=True) from exc
        if isinstance(data, dict) and data.get("errors"):
            raise WatchError("GitHub GraphQL returned errors: " + str(data["errors"])[:1000])
        return data

    def pr(self, repository, number):
        owner, name = repository.split("/")
        data = self.api("graphql", fields={"query": PR_QUERY, "owner": owner, "name": name, "number": number})
        try:
            pr = data["data"]["repository"]["pullRequest"]
            if not pr:
                raise KeyError("pullRequest")
            return pr
        except (KeyError, TypeError) as exc:
            raise WatchError("GitHub did not return the requested PR") from exc

    def pages(self, endpoint, key=None):
        pages = self.api(endpoint, paginate=True)
        if not isinstance(pages, list):
            raise WatchError("Expected paginated GitHub response", retryable=True)
        result = []
        for page in pages:
            items = page[key] if key else page
            if not isinstance(items, list):
                raise WatchError("Malformed GitHub page", retryable=True)
            result.extend(items)
        return result

    def requirements(self, request, pr):
        if request["checks"] == "named":
            return [{"name": name, "app_id": None} for name in dict.fromkeys(request["check_names"])]
        branch = pr.get("baseRef")
        if branch is None:
            raise WatchError("PR base branch is unavailable; cannot establish required checks", retryable=True)
        rule = branch.get("branchProtectionRule") or {}
        checks = [{"name": c["context"], "app_id": (c.get("app") or {}).get("databaseId")}
                  for c in rule.get("requiredStatusChecks", []) if rule.get("requiresStatusChecks", True)]
        rules = self.pages(f"repos/{request['repository']}/rules/branches/{quote(pr['baseRefName'], safe='')}?per_page=100")
        for rule in rules:
            if rule["type"] == "required_status_checks":
                checks += [{"name": c["context"], "app_id": c.get("integration_id") or None}
                           for c in rule["parameters"]["required_status_checks"]]
        return [dict(name=name, app_id=app) for name, app in dict.fromkeys((c["name"], c["app_id"]) for c in checks)]

    def checks(self, repository, sha, *, required=False):
        root = f"repos/{repository}/commits/{sha}"
        runs = self.pages(root + "/check-runs?filter=latest&per_page=100", "check_runs")
        statuses = self.pages(root + "/statuses?per_page=100")
        eligible_suites = None
        if required and any(r["app"].get("slug") == "github-actions" for r in runs):
            actions = self.pages(f"repos/{repository}/actions/runs?head_sha={sha}&per_page=100", "workflow_runs")
            events = {"push", "pull_request", "pull_request_review", "pull_request_target", "deployment", "deployment_status"}
            eligible_suites = {a["check_suite_id"] for a in actions if a["event"] in events and a["head_sha"] == sha}
        candidates = []
        for r in runs:
            if eligible_suites is not None and r["app"].get("slug") == "github-actions" and r["check_suite"]["id"] not in eligible_suites:
                continue
            if r["head_sha"] != sha:
                raise WatchError("GitHub returned a check for an unexpected commit", retryable=True)
            candidates.append(dict(name=r["name"], app_id=r["app"]["id"], kind="check",
                                   id=r["id"], state=(r["conclusion"] or "pending") if r["status"] == "completed" else "pending",
                                   url=r.get("html_url"), sha=sha))
        for s in statuses:
            candidates.append(dict(name=s["context"], app_id=None, kind="status", id=s["id"],
                                   state=s["state"], url=s.get("target_url"), sha=sha))
        # Newest run per name/app/type; a status and a check with the same name BOTH count.
        latest = {}
        for c in candidates:
            key = (c["name"], c["app_id"], c["kind"])
            if key not in latest or c["id"] > latest[key]["id"]:
                latest[key] = c
        return list(latest.values())

    def snapshot(self, request):
        pr = self.pr(request["repository"], request["pr_number"])
        snapshot = dict(pr=pr, requirements=[], checks=[], reviews=[], checks_sha=request["head_sha"])
        if identity_state(request, pr):
            return snapshot
        if request["checks"] != "none":
            snapshot["requirements"] = self.requirements(request, pr)
            merge = pr.get("potentialMergeCommit")
            merge_checks = []
            if merge:
                parents = {p["oid"] for p in merge["parents"]["nodes"]}
                if not {pr["headRefOid"], pr["baseRefOid"]}.issubset(parents):
                    raise WatchError("GitHub is recalculating the test merge commit", retryable=True)
                merge_checks = self.checks(request["repository"], merge["oid"], required=request["checks"] == "required")
            if merge_checks:
                snapshot.update(checks=merge_checks, checks_sha=merge["oid"])
            else:
                snapshot["checks"] = self.checks(request["repository"], request["head_sha"], required=request["checks"] == "required")
        if request["reviewers"]:
            reviews = self.pages(f"repos/{request['repository']}/pulls/{request['pr_number']}/reviews?per_page=100")
            snapshot["reviews"] = [dict(id=r["id"], author=(r.get("user") or {}).get("login", ""),
                                       state=r["state"], commit_id=r.get("commit_id"),
                                       submitted_at=r.get("submitted_at"), url=r.get("html_url")) for r in reviews]
        final = self.pr(request["repository"], request["pr_number"])
        # Never label results from an old head/base as current after an in-flight push.
        snapshot["pr"] = final
        if not identity_state(request, final) and final.get("potentialMergeCommit") != pr.get("potentialMergeCommit"):
            raise WatchError("Test merge commit changed during observation", retryable=True)
        return snapshot
