import argparse
import json
import sys

from . import engine
from .model import WatchError
from .store import Store


def parser():
    p = argparse.ArgumentParser(description="Durable GitHub PR watches. Every result is JSON; GitHub access is read-only.")
    p.add_argument("--state-dir", help="Shared state directory (or PR_WATCH_STATE_DIR)")
    commands = p.add_subparsers(dest="command", required=True)
    watch = commands.add_parser("watch", help="Create and start a background watch")
    watch.add_argument("--repo", dest="repository", required=True)
    watch.add_argument("--pr", dest="pr_number", type=int, required=True)
    watch.add_argument("--host", default="github.com")
    watch.add_argument("--head", dest="head_sha")
    watch.add_argument("--checks", choices=["required", "named", "none"], default="required")
    watch.add_argument("--check", dest="check_names", action="append", default=[])
    watch.add_argument("--reviewer", dest="reviewers", action="append", default=[])
    watch.add_argument("--review-after", help="Required with --reviewer: timestamp recorded before requesting this review cycle")
    watch.add_argument("--timeout", type=float, default=3600, help="Watch deadline in seconds (default 3600)")
    watch.add_argument("--interval", type=float, default=30, help="GitHub polling interval in seconds (default 30)")
    watch.add_argument("--allow-skipped", action="store_true", help="Accept GitHub's skipped/neutral conclusions; default requires success")
    watch.add_argument("--wait", action="store_true", help="Print only the terminal result; worker survives interruption")
    for name in ("status", "cancel", "resume", "wait", "_worker"):
        command = commands.add_parser(name)
        command.add_argument("id")
        if name == "wait":
            command.add_argument("--timeout", type=float, help="Return the current state after this many seconds; does not cancel the watch")
    commands.add_parser("list").add_argument("--active", action="store_true")
    commands.add_parser("serve", help="Supervise pending watches, including after reboot")
    commands.add_parser("mcp", help="Serve the same engine over MCP stdio (requires the mcp extra)")
    return p


def main():
    args = vars(parser().parse_args())
    command = args.pop("command")
    try:
        store = Store(args.pop("state_dir"))
        if command == "watch":
            blocking = args.pop("wait")
            result = engine.start(store, **args)
            if blocking:
                result = engine.wait(store, result["id"])
        elif command == "status":
            result = store.status(args["id"])
        elif command == "cancel":
            result = store.update(args["id"], {"state": "cancelled"})
        elif command == "resume":
            engine.spawn(store, args["id"])
            result = store.get(args["id"])
        elif command == "wait":
            result = engine.wait(store, args["id"], args["timeout"])
        elif command == "list":
            result = store.list(args["active"])
        elif command == "_worker":
            engine.run_worker(store, args["id"])
            return
        elif command == "serve":
            engine.serve(store)
            return
        elif command == "mcp":
            from .mcp_server import run
            run(store)
            return
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except WatchError as exc:
        print(json.dumps({"state": "error", "terminal": True, "error": str(exc), "retryable": exc.retryable}))
        sys.exit(1)
    except KeyboardInterrupt:
        # A foreground wait has no ownership of its durable subscription.
        sys.exit(130)


if __name__ == "__main__":
    main()
