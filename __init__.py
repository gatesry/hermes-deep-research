"""Hermes Deep Research plugin: registers the ``deep_research`` tool.

Hermes loads this directory as a package and calls ``register(ctx)`` once per profile.
"""

from __future__ import annotations

import threading

from .deep_research.adapters import AUDIT_TASK, HermesIO
from .deep_research.tool import SCHEMA, handle

PLUGIN_NAME = "deep-research"


def _int_setting(ctx, key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(ctx.get_config(key, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def register(ctx) -> None:
    lock = threading.Lock()
    state: dict = {}

    def manager():
        # Opened lazily inside a tool call, so the database lands in the calling profile's home.
        with lock:
            if "manager" not in state:
                from plugins.plugin_storage import plugin_db

                from .deep_research.jobs import JobManager
                from .deep_research.store import Store

                io = HermesIO(ctx, _int_setting(ctx, "max_output_tokens", 4096, 512, 32768))
                state["manager"] = JobManager(
                    Store(lambda: plugin_db(PLUGIN_NAME, "jobs.db")),
                    io_factory=lambda: io,
                    foreground_wait=_int_setting(ctx, "foreground_wait_seconds", 300, 0, 360),
                    max_active=_int_setting(ctx, "max_active_jobs", 4, 1, 16),
                )
            return state["manager"]

    def deep_research(args, conversation_id=None, session_id=None):
        return handle(manager, args, conversation_id=conversation_id, session_id=session_id)

    ctx.register_auxiliary_task(
        AUDIT_TASK,
        display_name="Deep research audit",
        description="Independent check that each research finding is supported by its quoted sources.",
    )
    ctx.register_tool(
        name="deep_research",
        toolset="deep_research",
        schema=SCHEMA,
        handler=deep_research,
        description=SCHEMA["description"],
        emoji="🔎",
    )
