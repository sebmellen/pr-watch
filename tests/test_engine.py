import tempfile
import time
import unittest
from unittest.mock import patch

from pr_watch import engine
from pr_watch.model import WatchError
from pr_watch.store import Store
from test_model import request, snapshot, check


class Engine(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        r = request()
        r["deadline"] = time.time() + 3600
        self.watch = self.store.create(r)
        self.id = self.watch["id"]

    def test_state_survives_a_new_client(self):
        self.assertEqual(Store(self.temp.name).get(self.id), self.watch)

    def test_cancelled_watch_cannot_be_overwritten_by_late_worker(self):
        self.store.update(self.id, {"state": "cancelled"})
        self.store.update(self.id, {"state": "ready"})
        self.assertEqual(self.store.get(self.id)["state"], "cancelled")

    def test_duplicate_worker_cannot_query_github(self):
        with self.store.lock(self.id) as acquired, patch("pr_watch.engine.GitHub") as gh:
            self.assertTrue(acquired)
            engine.run_worker(self.store, self.id)
            gh.assert_not_called()
            self.assertTrue(self.store.status(self.id)["worker_active"])

    def test_deadline_without_checks_is_timed_out(self):
        self.store.get(self.id)
        with patch("pr_watch.engine.time.time", return_value=time.time() + 7200), patch("pr_watch.engine.GitHub") as gh:
            engine.run_worker(self.store, self.id)
            self.assertEqual(self.store.get(self.id)["state"], "timed_out")
            gh.assert_not_called()

    def test_success_is_stored_as_terminal(self):
        data = snapshot()
        data["checks"] = [check()]
        with patch("pr_watch.engine.GitHub") as gh:
            gh.return_value.snapshot.return_value = data
            engine.run_worker(self.store, self.id)
        self.assertEqual(self.store.get(self.id)["state"], "ready")

    def test_permanent_api_error_wakes_with_error(self):
        with patch("pr_watch.engine.GitHub") as gh:
            gh.return_value.snapshot.side_effect = WatchError("HTTP 401")
            engine.run_worker(self.store, self.id)
        result = self.store.get(self.id)
        self.assertEqual(result["state"], "error")
        self.assertIn("401", result["last_error"]["message"])

    def test_transient_failure_does_not_become_completion(self):
        with patch("pr_watch.engine.GitHub") as gh, patch("pr_watch.engine.time.sleep") as sleep:
            gh.return_value.snapshot.side_effect = WatchError("HTTP 503", retryable=True)
            def cancel(_):
                self.assertEqual(self.store.get(self.id)["state"], "waiting")
                self.assertEqual(self.store.get(self.id)["last_error"]["consecutive_failures"], 1)
                self.store.update(self.id, {"state": "cancelled"})
            sleep.side_effect = cancel
            engine.run_worker(self.store, self.id)
        self.assertEqual(self.store.get(self.id)["state"], "cancelled")

    def test_wait_timeout_does_not_cancel_subscription(self):
        with patch("pr_watch.engine.spawn"):
            result = engine.wait(self.store, self.id, 0.01)
        self.assertFalse(result["terminal"])
        self.assertEqual(result["state"], "waiting")

    def test_wait_recovers_a_missing_worker(self):
        def complete(store, watch_id):
            store.update(watch_id, {"state": "ready"})
        with patch("pr_watch.engine.spawn", side_effect=complete) as spawn:
            self.assertEqual(engine.wait(self.store, self.id, 1)["state"], "ready")
            spawn.assert_called_once()
