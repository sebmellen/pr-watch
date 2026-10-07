"""Pure predicates: missing data is never evidence of success."""
from datetime import datetime, timezone
import math
import re


class WatchError(Exception):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("timezone required")
        return dt.timestamp()
    except (ValueError, TypeError, AttributeError) as exc:
        raise WatchError("review_after must be an ISO-8601 timestamp with a timezone") from exc


def reviewer_key(login):
    key = login.lower().removesuffix("[bot]")
    return "copilot-pull-request-reviewer" if key == "copilot" else key


def validate(request):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", request["repository"], flags=re.ASCII) or any(p in (".", "..") for p in request["repository"].split("/")):
        raise WatchError("repository must be OWNER/REPO")
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", request["host"]):
        raise WatchError("host must be a hostname, not a URL")
    if not isinstance(request["pr_number"], int) or isinstance(request["pr_number"], bool) or request["pr_number"] < 1:
        raise WatchError("pr_number must be a positive integer")
    if request["checks"] not in ("required", "named", "none"):
        raise WatchError("checks must be required, named, or none")
    if request["checks"] == "named" and not request["check_names"]:
        raise WatchError("named checks need at least one check name")
    if request["checks"] != "named" and request["check_names"]:
        raise WatchError("check_names may only be used with checks=named")
    if any(not isinstance(n, str) or not n.strip() for n in request["check_names"] + request["reviewers"]):
        raise WatchError("check names and reviewers must be nonempty strings")
    if request["checks"] == "none" and not request["reviewers"]:
        raise WatchError("select checks, reviewers, or both")
    if request["reviewers"]:
        parse_time(request["review_after"])
    for key, low, high in (("timeout", 1, 604800), ("interval", 5, 3600)):
        value = request[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise WatchError(f"{key} must be between {low} and {high} seconds")
    sha = request.get("head_sha")
    if sha is not None and not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
        raise WatchError("head_sha must be a full commit SHA")


def identity_state(request, pr):
    if pr["state"] in ("CLOSED", "MERGED"):
        return "merged" if pr["state"] == "MERGED" else "closed"
    if pr["headRefOid"] != request["head_sha"]:
        return "head_changed"
    if (pr["baseRefName"], pr["baseRefOid"]) != (request["base_ref"], request["base_sha"]):
        return "base_changed"
    return None


def evaluate(request, snapshot):
    pr = snapshot["pr"]
    result = {"state": "waiting", "head_sha": pr["headRefOid"], "base_sha": pr["baseRefOid"],
              "url": pr["url"], "checks": [], "reviews": [], "missing_reviewers": []}
    changed = identity_state(request, pr)
    if changed:
        return dict(result, state=changed)
    if request["checks"] != "none":
        requirements = snapshot["requirements"]
        if not requirements:
            raise WatchError("No required status checks are configured. Select explicit named checks or a review-only watch.")
        for required in requirements:
            matches = [c for c in snapshot["checks"] if c["name"] == required["name"]
                       and (required["app_id"] is None or c["app_id"] == required["app_id"] or c["kind"] == "status")]
            source_verified = required["app_id"] is None or any(c["app_id"] == required["app_id"] for c in matches)
            result["checks"].append({**required, "runs": matches, "state": "reported" if matches and source_verified else "missing"})
        runs = [c for group in result["checks"] for c in group["runs"]]
        bad = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale", "error"}
        if not request["allow_skipped"]:
            bad |= {"skipped", "neutral"}
        if any(c["state"] in bad for c in runs):
            return dict(result, state="checks_failed", checks_sha=snapshot["checks_sha"])
        allowed = {"success", "skipped", "neutral"} if request["allow_skipped"] else {"success"}
        checks_done = all(g["state"] == "reported" and all(c["state"] in allowed for c in g["runs"]) for g in result["checks"])
        result["checks_sha"] = snapshot["checks_sha"]
    else:
        checks_done = True
    for reviewer in request["reviewers"]:
        eligible = [r for r in snapshot["reviews"] if reviewer_key(r["author"]) == reviewer_key(reviewer)
                    and r["commit_id"] == request["head_sha"] and r["submitted_at"]
                    and parse_time(r["submitted_at"]) >= parse_time(request["review_after"])]
        latest = max(eligible, key=lambda r: (parse_time(r["submitted_at"]), r["id"]), default=None)
        if latest and latest["state"] in ("APPROVED", "COMMENTED", "CHANGES_REQUESTED"):
            result["reviews"].append(latest)
        else:
            result["missing_reviewers"].append(reviewer)
    if checks_done and not result["missing_reviewers"]:
        result["state"] = "review_received" if request["checks"] == "none" else "ready"
    return result
