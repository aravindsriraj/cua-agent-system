"""The decisions the AI owns (besides driving the recording itself).

  plan_goal  before recording: which values in the goal are inputs
  review     after recording: checkpoints, success condition, risky steps
  decide     during replay: what an unknown screen means, and what to do about it
  locate     during replay: where an element went when every recorded locator fails

The AI's judgment is final: callers apply its answers as given. Code keeps only the guarantees the AI never touches
(allowlist, approval gate, secrets, redaction, attempt limits). A failed call returns None after a few attempts;
the caller then continues without AI (a human decides, or the run fails with evidence).
"""
from __future__ import annotations

import asyncio
import json
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel

MODEL = "gemini-3.8-flash"


async def ask(prompt: str, schema: type[BaseModel], image: bytes | None = None, attempts: int = 3):
    contents = [prompt] + ([types.Part.from_bytes(data=image, mime_type="image/png")] if image else [])
    for n in range(attempts):
        try:
            r = await genai.Client().aio.models.generate_content(
                model=MODEL, contents=contents,
                config={"response_mime_type": "application/json", "response_schema": schema,
                        "automatic_function_calling": {"disable": True}})
            if r.parsed is not None:
                return r.parsed
        except Exception:  # network, quota, no key: try again, then give up quietly
            pass
        await asyncio.sleep(n + 1)
    return None


# ---- before recording: read the goal ------------------------------------------------------------------
class InputPlan(BaseModel):
    name: str
    example: str
    type: Literal["string", "number"]
    description: str


class Plan(BaseModel):
    inputs: list[InputPlan]


PLAN = """A user wants to automate this task in a web app, and to run it again later with different values:
"{goal}"

List the values in the task that a caller would change from one run to the next (an account number, an amount, a name,
a date, a search term...). For each: a snake_case name, the example exactly as written in the task, a type, and a
one-line description. Do not list fixed parts of the procedure, and do not list {{{{secret:...}}}} placeholders."""


async def plan_goal(goal: str) -> Plan | None:
    return await ask(PLAN.format(goal=goal), Plan)


# ---- after recording: checkpoints, success, risk --------------------------------------------------------
class Check(BaseModel):
    step: str
    url_contains: str
    text: str


class Risky(BaseModel):
    step: str
    reason: str


class Review(BaseModel):
    checkpoints: list[Check]
    success_url_contains: str
    success_text: str
    risky: list[Risky]


REVIEW = """A UI automation was just recorded for this task: "{goal}". It will be replayed without you, so it needs
checks that prove each step worked, and it must know which steps are risky.

Steps (with the URL and the new texts that appeared after each one): {steps}
Final page URL: {final_url}
Final page texts: {final_texts}

Return:
- checkpoints: for each step that changes the page, a url_contains (part of that step's url_after) and/or a text (one of
  that step's new_texts) that proves the step worked. Use "" for either when it would not help. Prefer stable headings and
  labels over data that changes between runs (balances, dates, counts, ids).
- success_url_contains and success_text: the same, proving the whole task succeeded (from the final URL and texts).
- risky: steps that submit, pay, transfer, apply, book, delete, or change data in a way that is hard to undo, with a
  short reason. They will require human approval before running unattended."""


async def review(goal: str, steps: list[dict], final_url: str, final_texts: list[str]) -> Review | None:
    return await ask(REVIEW.format(goal=goal, steps=json.dumps(steps), final_url=final_url,
                                   final_texts=json.dumps(final_texts)), Review)


# ---- during replay: an unknown screen --------------------------------------------------------------------
class Decision(BaseModel):
    kind: Literal["business_outcome", "dismiss", "retry", "restart", "failure", "unsure"]
    code: str
    text: str
    reason: str
    x: int
    y: int


DECIDE = """An automation replaying the task "{goal}" hit an unexpected screen at step "{step}": {why}.
It expected: {expected}. The screenshot shows the page now. Short texts on it: {texts}

Decide what this screen means. Your decision is acted on immediately and remembered for future runs:
- business_outcome: a legitimate answer the caller should receive (not found, insufficient funds, loan denied...)
- dismiss: a pop-up, notice or interstitial to close; x,y (0-999 grid) is the control that closes it without
  committing anything
- retry: a temporary problem (loading, busy, maintenance, rate limit)
- restart: logged out or session expired
- failure: an error that should stop the run
- unsure: you cannot tell; a human will look
code: SHORT_UPPER_SNAKE_CASE. text: a short phrase copied exactly from the page that identifies this screen.
reason: one sentence. x, y: 0 unless kind is dismiss."""


async def decide(goal: str, step: str, why: str, expected, texts: list[str], screenshot: bytes) -> Decision | None:
    return await ask(DECIDE.format(goal=goal, step=step, why=why, expected=json.dumps(expected),
                                   texts=json.dumps(texts[:80])), Decision, screenshot)


# ---- during replay: a moved element ----------------------------------------------------------------------
class Point(BaseModel):
    found: bool
    x: int
    y: int


LOCATE = """Find this element on the screenshot: {target}. It is used to: {intent}.
If it is present, return found=true and its center as x,y on a 0-999 grid over the whole screenshot
(x from the left edge, y from the top edge). If it is not clearly present, return found=false, x=0, y=0."""


async def locate(target: str, intent: str, screenshot: bytes) -> Point | None:
    return await ask(LOCATE.format(target=target, intent=intent), Point, screenshot)
