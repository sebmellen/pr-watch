import copy
import unittest

from pr_watch.model import WatchError, evaluate, validate

SHA = "a" * 40
BASE = "b" * 40


def request(**updates):
    return dict(repository="owner/repo", pr_number=1, host="github.com", checks="named", check_names=["test (api)"],
                reviewers=[], review_after=None, head_sha=SHA, base_ref="main", base_sha=BASE,
                timeout=3600, interval=30, allow_skipped=False, **updates)


def snapshot():
    return dict(pr=dict(state="OPEN", headRefOid=SHA, baseRefOid=BASE, baseRefName="main", url="https://github.com/owner/repo/pull/1"),
                requirements=[dict(name="test (api)", app_id=None)], checks_sha=SHA, reviews=[], checks=[])


def check(state="success", **updates):
    c = dict(name="test (api)", app_id=15368, kind="check", state=state, id=1, sha=SHA, url="https://example.invalid/check")
    c.update(updates)
    return c


def review(**updates):
    r = dict(id=1, author="copilot-pull-request-reviewer[bot]", commit_id=SHA,
             state="COMMENTED", submitted_at="2026-10-07T10:01:00Z", url="https://example.invalid/review")
    r.update(updates)
    return r


class Predicates(unittest.TestCase):
    def setUp(self):
        self.r, self.s = request(), snapshot()

    def outcome(self):
        return evaluate(self.r, self.s)

    def test_empty_checks_are_missing(self):
        result = self.outcome()
        self.assertEqual(result["state"], "waiting")
        self.assertEqual(result["checks"][0]["state"], "missing")

    def test_no_configured_requirements_is_explicit_error(self):
        self.s["requirements"] = []
        with self.assertRaisesRegex(WatchError, "No required"):
            self.outcome()

    def test_job_name_with_spaces_is_not_parsed_as_status(self):
        self.s["requirements"][0]["name"] = "integration testing (shard 1/3)"
        self.s["checks"] = [check("pending", name="integration testing (shard 1/3)")]
        self.assertEqual(self.outcome()["state"], "waiting")

    def test_missing_second_required_check_does_not_pass(self):
        self.s["requirements"].append(dict(name="integration", app_id=None))
        self.s["checks"] = [check()]
        self.assertEqual(self.outcome()["state"], "waiting")

    def test_unknown_conclusion_is_not_success(self):
        self.s["checks"] = [check("new_future_conclusion")]
        self.assertEqual(self.outcome()["state"], "waiting")

    def test_skipped_and_neutral_require_opt_in(self):
        for value in ("skipped", "neutral"):
            self.s["checks"] = [check(value)]
            self.r["allow_skipped"] = False
            self.assertEqual(self.outcome()["state"], "checks_failed")
            self.r["allow_skipped"] = True
            self.assertEqual(self.outcome()["state"], "ready")

    def test_failure_wakes_even_if_other_check_is_missing(self):
        self.s["requirements"].append(dict(name="integration", app_id=None))
        self.s["checks"] = [check("failure")]
        self.assertEqual(self.outcome()["state"], "checks_failed")

    def test_wrong_app_does_not_satisfy_required_source(self):
        self.s["requirements"][0]["app_id"] = 9
        self.s["checks"] = [check()]
        self.assertEqual(self.outcome()["state"], "waiting")

    def test_status_and_check_with_same_name_both_must_pass(self):
        self.s["checks"] = [check(), check("pending", kind="status", app_id=None)]
        self.assertEqual(self.outcome()["state"], "waiting")

    def test_app_scoped_check_cannot_hide_failing_same_named_status(self):
        self.s["requirements"][0]["app_id"] = 15368
        self.s["checks"] = [check(), check("failure", kind="status", app_id=None)]
        self.assertEqual(self.outcome()["state"], "checks_failed")

    def test_unverified_status_source_does_not_satisfy_app_requirement(self):
        self.s["requirements"][0]["app_id"] = 15368
        self.s["checks"] = [check(kind="status", app_id=None)]
        self.assertEqual(self.outcome()["state"], "waiting")

    def test_all_required_success_is_ready(self):
        self.s["checks"] = [check()]
        self.assertEqual(self.outcome()["state"], "ready")

    def test_new_head_and_base_terminate_without_reusing_old_results(self):
        self.s["checks"] = [check()]
        for key, value, state in (("headRefOid", "c" * 40, "head_changed"), ("baseRefOid", "d" * 40, "base_changed"),
                                  ("state", "CLOSED", "closed"), ("state", "MERGED", "merged")):
            with self.subTest(key=key, value=value):
                changed = copy.deepcopy(self.s)
                changed["pr"][key] = value
                self.assertEqual(evaluate(self.r, changed)["state"], state)

    def setup_review(self):
        self.r.update(checks="none", check_names=[], reviewers=["copilot"], review_after="2026-10-07T10:00:00Z")

    def test_old_review_other_author_other_head_and_pending_are_excluded(self):
        self.setup_review()
        for changes in (dict(submitted_at="2026-10-07T09:59:59Z"), dict(author="someone-else"),
                        dict(commit_id="c" * 40), dict(state="PENDING", submitted_at=None)):
            with self.subTest(changes=changes):
                self.s["reviews"] = [review(**changes)]
                self.assertEqual(self.outcome()["state"], "waiting")

    def test_review_after_page_one_is_found(self):
        self.setup_review()
        self.s["reviews"] = [review(id=i, commit_id="c" * 40) for i in range(150)] + [review(id=200)]
        self.assertEqual(self.outcome()["reviews"][0]["id"], 200)
        self.assertEqual(self.outcome()["state"], "review_received")

    def test_latest_dismissed_review_does_not_revive_old_approval(self):
        self.setup_review()
        self.s["reviews"] = [review(state="APPROVED"), review(id=2, state="DISMISSED", submitted_at="2026-10-07T10:02:00Z")]
        self.assertEqual(self.outcome()["state"], "waiting")

    def test_requires_every_selected_reviewer(self):
        self.setup_review()
        self.r["reviewers"].append("alice")
        self.s["reviews"] = [review()]
        self.assertEqual(self.outcome()["missing_reviewers"], ["alice"])

    def test_combined_watch_requires_checks_and_reviews(self):
        self.setup_review()
        self.r.update(checks="named", check_names=["test (api)"])
        self.s["reviews"] = [review()]
        self.assertEqual(self.outcome()["state"], "waiting")
        self.s["checks"] = [check()]
        self.assertEqual(self.outcome()["state"], "ready")

    def test_invalid_request_fails_before_any_network_access(self):
        for update in (dict(pr_number=0), dict(repository="https://evil/owner/repo"), dict(host="--help"),
                       dict(interval=float("nan")), dict(timeout=float("inf")), dict(head_sha="short"),
                       dict(reviewers=["copilot"], review_after=None), dict(reviewers=["copilot"], review_after="2026-10-07T10:00:00")):
            r = request()
            r.update(update)
            with self.subTest(update=update), self.assertRaises(WatchError):
                validate(r)
