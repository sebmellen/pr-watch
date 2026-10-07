"""Thin provider-independent MCP adapter; all semantics live in the shared engine."""
import asyncio
import threading
from typing import Any, Literal

from . import engine
from .model import WatchError


def run(store):
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise WatchError('Install the MCP adapter with: pip install "pr-watch[mcp]"') from exc
    server = MCPServer("pr-watch")

    @server.tool(annotations=ToolAnnotations(destructive_hint=False))
    async def watch_pr(repository: str, pr_number: int, checks: Literal["required", "named", "none"] = "required",
                       check_names: list[str] | None = None, reviewers: list[str] | None = None,
                       review_after: str | None = None, head_sha: str | None = None,
                       timeout: float = 3600, interval: float = 30, allow_skipped: bool = False,
                       host: str = "github.com") -> dict[str, Any]:
        """Start a durable read-only PR watch. Reviewers require a timezone-qualified review_after cycle timestamp.

        Returns an ID immediately. Use wait_for_pr_watch once instead of model-driven polling.
        Named checks require check_names. A new head/base ends this watch instead of silently following it.
        """
        return await asyncio.to_thread(engine.start, store, repository=repository, pr_number=pr_number,
                                       checks=checks, check_names=check_names, reviewers=reviewers,
                                       review_after=review_after, head_sha=head_sha, timeout=timeout,
                                       interval=interval, allow_skipped=allow_skipped, host=host)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    def get_pr_watch(watch_id: str) -> dict[str, Any]:
        """Inspect a saved watch and whether its worker is alive; does not query GitHub."""
        return store.status(watch_id)

    @server.tool(annotations=ToolAnnotations(destructive_hint=False))
    async def wait_for_pr_watch(watch_id: str, timeout: float = 3600) -> dict[str, Any]:
        """Wait quietly for a terminal event; the worker polls outside the model.

        Set the host's tool timeout longer than this call. Host cancellation does not cancel the watch.
        A waiting result means this call's timeout elapsed, not that the PR completed.
        """
        stop = threading.Event()
        try:
            return await asyncio.to_thread(engine.wait, store, watch_id, timeout, stop_event=stop)
        finally:
            stop.set()

    @server.tool(annotations=ToolAnnotations(destructive_hint=False))
    def cancel_pr_watch(watch_id: str) -> dict[str, Any]:
        """Cancel local watching only. Never cancels CI, modifies a PR, or posts comments."""
        return store.update(watch_id, {"state": "cancelled"})

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    def list_pr_watches(active_only: bool = True) -> dict[str, Any]:
        """List persisted watches shared across CLI and MCP clients."""
        return {"watches": store.list(active_only)}

    server.run()
