"""Exercise real subprocesses, two MCP clients, and one shared durable watch."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from pr_watch.store import Store

try:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:
    ClientSession = None

ROOT = Path(__file__).resolve().parent.parent
FAKE_GH = '''#!{python}
import json, os, sys
state = json.load(open(os.environ["WATCH_TEST_FIXTURE"]))
endpoint = sys.argv[4]
if endpoint == "graphql":
    print(json.dumps({{"data":{{"repository":{{"pullRequest":state["pr"]}}}}}}))
elif "/check-runs?" in endpoint:
    print(json.dumps([{{"check_runs":[{{"id":1,"name":"test", "head_sha":"a"*40,
        "app":{{"id":1}},"status":"completed" if state["pass"] else "in_progress",
        "conclusion":"success" if state["pass"] else None}}]}}]))
elif "/statuses?" in endpoint:
    print("[[]]")
else:
    print("unexpected endpoint " + endpoint, file=sys.stderr)
    sys.exit(1)
'''


@unittest.skipIf(ClientSession is None, "Install the mcp extra to test the MCP adapter")
class Interfaces(unittest.IsolatedAsyncioTestCase):
    async def test_watch_survives_client_disconnect_and_is_shared_with_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "gh"
            executable.write_text(FAKE_GH.format(python=sys.executable))
            executable.chmod(0o700)
            fixture = root / "fixture.json"
            fixture_data = dict(pr=dict(state="OPEN", headRefOid="a"*40, baseRefOid="b"*40, baseRefName="main",
                                       url="https://github.com/owner/repo/pull/1", potentialMergeCommit=None,
                                       baseRef={"branchProtectionRule": None}))
            fixture.write_text(json.dumps({**fixture_data, "pass": False}))
            state_dir = root / "state"
            env = {"PATH": str(root) + os.pathsep + os.environ.get("PATH", ""),
                   "PYTHONPATH": str(ROOT), "WATCH_TEST_FIXTURE": str(fixture)}
            params = StdioServerParameters(command=sys.executable, args=["-m", "pr_watch", "--state-dir", str(state_dir), "mcp"], env=env)
            watch_id = None
            try:
                async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
                    await client.initialize()
                    names = {tool.name for tool in (await client.list_tools()).tools}
                    self.assertEqual(names, {"watch_pr", "get_pr_watch", "wait_for_pr_watch", "cancel_pr_watch", "list_pr_watches"})
                    result = await client.call_tool("watch_pr", dict(repository="owner/repo", pr_number=1, checks="named", check_names=["test"], interval=5, timeout=30))
                    result = result.model_dump(by_alias=True)
                    self.assertFalse(result.get("isError", False), result)
                    watch_id = result["structuredContent"]["id"]
                    wait = await client.call_tool("wait_for_pr_watch", dict(watch_id=watch_id, timeout=0.5))
                    self.assertEqual(wait.model_dump(by_alias=True)["structuredContent"]["state"], "waiting")
                proc = subprocess.run([sys.executable, "-m", "pr_watch", "--state-dir", str(state_dir), "status", watch_id],
                                      text=True, capture_output=True, check=True, cwd=ROOT)
                saved = json.loads(proc.stdout)
                self.assertTrue(saved["worker_active"])
                self.assertFalse(saved["terminal"])
                # A different provider/client reconnects after the first client exited.
                fixture.write_text(json.dumps({**fixture_data, "pass": True}))
                async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
                    await client.initialize()
                    result = await client.call_tool("wait_for_pr_watch", dict(watch_id=watch_id, timeout=15))
                    saved = result.model_dump(by_alias=True)["structuredContent"]
                    self.assertEqual(saved["state"], "ready")
                    self.assertEqual(saved["id"], watch_id)
                    result = await client.call_tool("cancel_pr_watch", dict(watch_id=watch_id))
                    self.assertEqual(result.model_dump(by_alias=True)["structuredContent"]["state"], "ready")
            finally:
                if watch_id:
                    Store(state_dir).update(watch_id, {"state": "cancelled"})
                    # Let any worker leave its lock before removing this test's private state directory.
                    for _ in range(30):
                        if not Store(state_dir).status(watch_id)["worker_active"]:
                            break
                        await asyncio.sleep(0.1)
