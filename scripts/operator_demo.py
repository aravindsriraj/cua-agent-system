"""Reproducible operator for the /evidence runs.

A person normally does this by clicking the control bar in the browser window. Here the same bar actions
(`take_over`, `approve`, `label`) are sent through the same binding, so the evidence is reproducible.

  uv run python scripts/operator_demo.py handoff    # no AI: an unknown pop-up -> operator takes over, closes it, labels it
  uv run python scripts/operator_demo.py transfer   # record a risky transfer; operator approves the Transfer click
"""
import asyncio
import os
import sys

from cua import artifact as A
from cua.agent import MODEL, Recorder
from cua.cli import load_dotenv
from cua.replay import Replayer

load_dotenv()
PASSWORD = os.environ["PASSWORD"]


async def bar(page, action: str, data: str = "{}") -> None:
    """Like a real click on the bar: fire and forget."""
    await page.evaluate(f"() => {{ setTimeout(() => window.__cua_ui('{action}', {data}), 0); }}")


async def handoff() -> None:
    art = A.load("parabank-account-balance", version=1)  # v1 does not know the pop-up yet
    user, account = [k for k, v in art.inputs.items() if v.type != "secret"]  # the names the AI chose, in goal order
    params = {user: "cua_demo_6955", account: "31437", "password": PASSWORD}
    rp = Replayer(art, params, inject=["modal@s5"], ai=False)  # no AI, so the human decides

    async def operator():
        while rp.ctl.state != "awaiting_human":
            await asyncio.sleep(0.2)
        await asyncio.sleep(1.5)
        await bar(rp.surface.page, "take_over")
        await asyncio.sleep(1)
        await rp.surface.page.click("#cua-fault-modal >> text=OK")  # the manual fix, in the same live session
        await asyncio.sleep(1)
        await bar(rp.surface.page, "label", "{kind: 'dismiss', code: 'SYSTEM_NOTICE', text: 'System notice'}")

    result, _ = await asyncio.gather(rp.run(), operator())
    print(result.model_dump_json(indent=2))


async def transfer() -> None:
    goal = ("Log in as cua_demo_6955 with {password:secret}, then use Transfer Funds to transfer 1 "
            "from account 31437 to account 31770 and reach the transfer confirmation")
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
    asyncio.run({"handoff": handoff, "transfer": transfer}[sys.argv[1]]())
