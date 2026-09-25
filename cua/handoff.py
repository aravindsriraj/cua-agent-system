"""Control transfer: exactly one owner of the live session at a time.

    agent/running --escalate--> agent/awaiting_human --Take over--> human/human_active --Hand back / Label--> agent/running
                  \\------------------------------ Take over (any time) ----------------------------------/
Automation calls `checkpoint()` before every action, so it can never act while a human holds control.
Nobody answering a request within `patience` counts as no human available; a person who took over is never cut off.
"""
from __future__ import annotations

import asyncio

from .evidence import RunLog


class Controller:
    patience = 300.0  # seconds to wait for someone to answer a request

    def __init__(self, surface, log: RunLog, human_available: bool, can_label: bool = False):
        self.surface, self.log = surface, log
        self.human_available = human_available
        self.can_label = can_label  # replay: a human may label an unknown screen as a new outcome
        self.owner, self.state, self.ask, self.message = "agent", "running", None, ""
        self._decision: asyncio.Future | None = None
        surface.on_ui = self.on_ui

    async def show(self, message: str) -> None:
        self.message = message
        await self._render()

    async def _render(self) -> None:
        await self.surface.render({"owner": self.owner, "state": self.state, "ask": self.ask,
                                   "message": self.message, "can_label": self.can_label})

    async def _set(self, owner: str, state: str, why: str) -> None:
        self.owner, self.state = owner, state
        self.log.log("control", owner=owner, state=state, why=why)
        await self._render()

    async def on_ui(self, action: str, data: dict) -> None:
        """Operator clicked a button in the control bar."""
        self.log.log("operator", action=action, **data)
        if action == "take_over":
            self.message = "Do what's needed in this window, then click Hand back."
            await self._set("human", "human_active", "operator took over")
            return
        if action in ("approve", "deny") and self.ask != "approval":
            return
        self.message = ""
        await self._set("agent", "running", f"operator: {action}")
        if self._decision and not self._decision.done():
            self._decision.set_result({"decision": action, **data})

    async def checkpoint(self) -> None:
        """Call before every automated action: waits while a human holds control."""
        if self.owner == "human":
            await self._wait()

    async def escalate(self, reason: str, ask: str = "stuck", **context) -> dict:
        """Raise an intervention request and wait for the operator's decision.
        Returns {"decision": approve|deny|hand_back|label|unavailable, ...}."""
        path = self.log.write("intervention.json", {"reason": reason, "ask": ask, "run_id": self.log.run_id, **context})
        self.log.log("escalation", reason=reason, ask=ask, request=path)
        if not self.human_available:
            return {"decision": "unavailable"}
        print(f"\a⚠️  Needs you: {reason}\n   Use the bar at the bottom of the browser window.", flush=True)
        self.ask, self.message = ask, reason
        await self._set("agent", "awaiting_human", reason)
        try:
            return await self._wait(self.patience)
        finally:
            self.ask = None

    async def _wait(self, patience: float | None = None) -> dict:
        self._decision = asyncio.get_running_loop().create_future()
        try:
            return await asyncio.wait_for(asyncio.shield(self._decision), patience)
        except TimeoutError:
            if self.owner == "human":  # someone took over: wait for them to hand back
                return await self._decision
            self.ask, self.message = None, ""
            await self._set("agent", "running", f"nobody answered within {patience:g}s")
            return {"decision": "unavailable", "note": f" (nobody answered within {patience:g}s)"}
