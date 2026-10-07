"""Waiting is local I/O; no model, provider SDK, or GitHub write is involved."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .github import GitHub
from .model import WatchError, evaluate, utc, validate
from .store import Store


def start(store, *, repository, pr_number, checks="required", check_names=None, reviewers=None,
          review_after=None, head_sha=None, timeout=3600, interval=30, allow_skipped=False,
          host="github.com", detached=True, github=None):
    request = dict(repository=repository, pr_number=pr_number, checks=checks,
                   check_names=check_names or [], reviewers=reviewers or [], review_after=review_after,
                   head_sha=head_sha, timeout=timeout, interval=interval, allow_skipped=allow_skipped, host=host)
    validate(request)
    pr = (github or GitHub(host)).pr(repository, pr_number)
    request.update(head_sha=(head_sha or pr["headRefOid"]).lower(), base_sha=pr["baseRefOid"], base_ref=pr["baseRefName"],
                   deadline=time.time() + timeout)
    result = store.create(request)
    if detached:
        spawn(store, result["id"])
    return result


def spawn(store, watch_id):
    if store.get(watch_id)["terminal"]:
        return
    # File locks, not process-name matching or reusable PIDs, decide ownership.
    with store.lock(watch_id) as acquired:
        if not acquired:
            return
    env = dict(os.environ)
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
    with (store.directory / "workers.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, "-m", "pr_watch", "--state-dir", str(store.directory), "_worker", watch_id],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                         close_fds=True, env=env, cwd=store.directory)
    threading.Thread(target=process.wait, daemon=True).start()


def run_worker(store, watch_id, *, github=None):
    with store.lock(watch_id) as acquired:
        if not acquired:
            return
        failures = 0
        while True:
            current = store.get(watch_id)
            if current["terminal"]:
                return
            request = current["request"]
            if time.time() >= request["deadline"]:
                store.update(watch_id, {"state": "timed_out"})
                return
            delay = request["interval"]
            try:
                snapshot = (github or GitHub(request["host"], deadline=request["deadline"])).snapshot(request)
                observation = evaluate(request, snapshot)
                observation.update(observed_at=utc(time.time()), last_error=None)
                failures = 0
            except WatchError as exc:
                failures += 1
                observation = dict(state="waiting" if exc.retryable and failures < 5 else "error",
                                   last_error={"message": str(exc), "retryable": exc.retryable,
                                               "consecutive_failures": failures, "at": utc(time.time())})
                delay = min(300, delay * 2 ** (failures - 1))
            except Exception as exc:
                observation = dict(state="error", last_error={"message": f"{type(exc).__name__}: {exc}", "retryable": False})
            # An observation completed after the deadline cannot silently extend the watch.
            if time.time() >= request["deadline"]:
                observation["state"] = "timed_out"
            result = store.update(watch_id, observation)
            if result["terminal"]:
                return
            end = min(time.time() + delay, request["deadline"])
            while time.time() < end:
                if store.get(watch_id)["terminal"]:
                    return
                time.sleep(min(1, max(0, end - time.time())))


def wait(store, watch_id, timeout=None, *, stop_event=None):
    if timeout is not None and (not isinstance(timeout, (int, float)) or not 0 < timeout <= 604800):
        raise WatchError("wait timeout must be between 0 and 604800 seconds")
    end = time.monotonic() + timeout if timeout is not None else None
    last_resume = 0
    while True:
        result = store.get(watch_id)
        if result["terminal"] or (stop_event is not None and stop_event.is_set()) or (end is not None and time.monotonic() >= end):
            return result
        # Also recovers a worker lost to a process crash or a machine restart.
        if time.monotonic() - last_resume >= 5:
            spawn(store, watch_id)
            last_resume = time.monotonic()
        time.sleep(min(0.25, max(0, end - time.monotonic())) if end else 0.25)


def serve(store):
    """Optional supervisor: recover pending watches after login/reboot or worker loss."""
    while True:
        for watch in store.list(active=True):
            spawn(store, watch["id"])
        time.sleep(5)
