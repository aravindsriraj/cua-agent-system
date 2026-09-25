"""Reproducible operator for the /evidence runs.

A person normally does this by clicking the control bar in the browser window. Here the same bar actions
(`take_over`, `approve`, `label`) are sent through the same binding, so the evidence is reproducible.

  uv run python scripts/operator_demo.py handoff    # no AI: an unknown pop-up -> operator takes over, closes it, labels it
  uv run python scripts/operator_demo.py wrong_password   # no AI: the login is rejected -> operator labels it INVALID_LOGIN
  uv run python scripts/operator_demo.py transfer   # record a risky transfer; operator approves the Transfer click
"""
import asyncio
import os
import sys

from cua import artifact as A
from cua.agent import MODEL, Recorder
from cua.cli import load_dotenv
from cua.replay import Replayer
from parabank_accounts import accounts  # john's current accounts (they change when ParaBank resets)

load_dotenv()
PASSWORD = os.environ["PASSWORD"]


async def bar(page, action: str, data: str = "{}") -> None:
    """Like a real click on the bar: fire and forget."""
    await page.evaluate(f"() => {{ setTimeout(() => window.__cua_ui('{action}', {data}), 0); }}")


async def replay_with_operator(version: int | None, password: str, label: str, inject=(), fix=None) -> None:
    """Default replay (no AI). When it asks for a person, the operator takes over, does `fix` if any, labels the screen."""
    art = A.load("parabank-account-balance", version=version)
    user, account = [k for k, v in art.inputs.items() if v.type != "secret"]  # the names the AI chose, in goal order
    rp = Replayer(art, {user: "john", account: accounts()[0], "password": password}, inject=inject, ai=False)

    async def operator():
        while rp.ctl.state != "awaiting_human":
            await asyncio.sleep(0.2)
        await asyncio.sleep(1.5)
        await bar(rp.surface.page, "take_over")
        await asyncio.sleep(1)
        if fix:
            await fix(rp.surface.page)  # the manual work, in the same live session
            await asyncio.sleep(1)
        await bar(rp.surface.page, "label", label)

    result, _ = await asyncio.gather(rp.run(), operator())
    print(result.model_dump_json(indent=2))


async def handoff() -> None:
    """v1 does not know the pop-up yet: the operator closes it and labels it."""
    await replay_with_operator(1, PASSWORD, "{kind: 'dismiss', code: 'SYSTEM_NOTICE', text: 'System notice'}",
                               inject=["modal@s5"], fix=lambda page: page.click("#cua-fault-modal >> text=OK"))


async def wrong_password() -> None:
    """A validation error: the app rejects the login. Nothing to fix; the operator labels it a normal answer."""
    await replay_with_operator(None, "not-the-password",
                               "{kind: 'business_outcome', code: 'INVALID_LOGIN', text: 'could not be verified'}")


async def transfer() -> None:
    a1, a2 = accounts()[:2]
    goal = ("Log in as john with {password:secret}, then use Transfer Funds to transfer 1 "
            f"from account {a1} to account {a2} and reach the transfer confirmation")
    rec = Recorder("https://parabank.parasoft.com/parabank/index.htm", goal, MODEL, human=True, headless=False,
                   secrets={"password": PASSWORD}, name="parabank-transfer")

    async def operator():
        while True:
            await asyncio.sleep(0.3)
            if rec.ctl.state == "awaiting_human" and rec.ctl.ask == "approval":
                print("operator approves:", rec.ctl.message, flush=True)
                await asyncio.sleep(1)
                await bar(rec.surface.page, "approve")

    op = asyncio.create_task(operator())
    art = await rec.run()
    op.cancel()
    print(f"learned {art.name} v{art.capability.version}" if art else "recording failed")


if __name__ == "__main__":
    asyncio.run({"handoff": handoff, "wrong_password": wrong_password, "transfer": transfer}[sys.argv[1]]())
