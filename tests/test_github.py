import subprocess
import unittest
from unittest.mock import patch

from pr_watch.github import GitHub
from pr_watch.model import WatchError
from test_model import BASE, SHA, request


def pr():
    return dict(state="OPEN", headRefOid=SHA, baseRefOid=BASE, baseRefName="main", url="https://github.com/owner/repo/pull/1",
                potentialMergeCommit=None, baseRef={"branchProtectionRule": None})


def run(id=1, state="success", app=15368):
    return dict(id=id, name="test (api)", head_sha=SHA, app={"id": app}, status="completed", conclusion=state, html_url="url")


class Transport(unittest.TestCase):
    def test_pagination_combines_every_page(self):
        g = GitHub()
        with patch.object(g, "api", return_value=[[1, 2], [3]]) as api:
            self.assertEqual(g.pages("reviews"), [1, 2, 3])
            api.assert_called_once_with("reviews", paginate=True)

    def test_check_pages_and_latest_rerun(self):
        g = GitHub()
        with patch.object(g, "pages", side_effect=[[run(2, "failure"), run(3, "pending"), run(1)], []]):
            result = g.checks("owner/repo", SHA)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["state"], "pending")

    def test_legacy_status_and_check_are_not_collapsed(self):
        g = GitHub()
        with patch.object(g, "pages", side_effect=[[run()], [dict(id=2, context="test (api)", state="failure")]]):
            self.assertEqual(len(g.checks("owner/repo", SHA)), 2)

    def test_rule_sources_union_app_identity_and_branch_quoting(self):
        p = pr()
        p.update(baseRefName="release/2026")
        p["baseRef"]["branchProtectionRule"] = {"requiredStatusChecks": [{"context": "test", "app": {"databaseId": 9}}]}
        rule = dict(type="required_status_checks", parameters={"required_status_checks": [{"context": "integration", "integration_id": 9}]})
        g = GitHub()
        with patch.object(g, "pages", return_value=[rule]) as pages:
            self.assertEqual(len(g.requirements({**request(), "checks": "required"}, p)), 2)
            self.assertIn("release%2F2026", pages.call_args.args[0])

    def test_test_merge_checks_take_precedence(self):
        g, p = GitHub(), pr()
        p["potentialMergeCommit"] = {"oid": "c" * 40, "parents": {"nodes": [{"oid": SHA}, {"oid": BASE}]}}
        with patch.object(g, "pr", return_value=p), patch.object(g, "requirements", return_value=[]), \
             patch.object(g, "checks", return_value=[{"name": "test", "state": "pending"}]) as checks:
            result = g.snapshot(request())
            self.assertEqual(result["checks_sha"], "c" * 40)
            self.assertEqual(checks.call_count, 1)

    def test_manual_actions_run_does_not_satisfy_required_pr_checks(self):
        g = GitHub()
        r = run()
        r["app"]["slug"] = "github-actions"
        r["check_suite"] = {"id": 99}
        action = dict(check_suite_id=99, event="workflow_dispatch", head_sha=SHA)
        with patch.object(g, "pages", side_effect=[[r], [], [action]]):
            self.assertEqual(g.checks("owner/repo", SHA, required=True), [])
        action["event"] = "pull_request"
        with patch.object(g, "pages", side_effect=[[r], [], [action]]):
            self.assertEqual(len(g.checks("owner/repo", SHA, required=True)), 1)

    def test_head_change_during_observation_is_exposed(self):
        g, p = GitHub(), pr()
        changed = dict(p, headRefOid="d" * 40)
        with patch.object(g, "pr", side_effect=[p, changed]), patch.object(g, "requirements", return_value=[]), \
             patch.object(g, "checks", return_value=[]):
            self.assertEqual(g.snapshot(request())["pr"]["headRefOid"], "d" * 40)

    def test_stale_test_merge_parents_are_retried(self):
        g, p = GitHub(), pr()
        p["potentialMergeCommit"] = {"oid": "c" * 40, "parents": {"nodes": [{"oid": "d" * 40}, {"oid": BASE}]}}
        with patch.object(g, "pr", return_value=p), patch.object(g, "requirements", return_value=[]):
            with self.assertRaisesRegex(WatchError, "recalculating"):
                g.snapshot(request())

    def test_expired_deadline_stops_before_next_api_call(self):
        with patch("pr_watch.github.subprocess.run") as run_command, self.assertRaisesRegex(WatchError, "deadline"):
            GitHub(deadline=0).api("repos/owner/repo")
        run_command.assert_not_called()

    def test_api_error_is_not_an_empty_list(self):
        for code in (401, 403, 429, 503):
            proc = subprocess.CompletedProcess([], 1, stdout="", stderr=f"gh: error (HTTP {code})")
            with patch("pr_watch.github.subprocess.run", return_value=proc), self.assertRaises(WatchError) as raised:
                GitHub().api("repos/owner/repo")
            self.assertEqual(raised.exception.retryable, code in (429, 503))

    def test_invalid_json_and_graphql_partial_errors_fail_closed(self):
        for data in ("", '{"data":{},"errors":[{"message":"denied"}]}'):
            proc = subprocess.CompletedProcess([], 0, stdout=data, stderr="")
            with patch("pr_watch.github.subprocess.run", return_value=proc), self.assertRaises(WatchError):
                GitHub().api("graphql")
