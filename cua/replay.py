"""Replay: deterministic. Code runs the recorded flow with no model; with --ai, the AI handles only the unexpected.

Every step: (inject fault) -> close known pop-ups -> approval gate -> resolve ladder -> act -> verify checkpoint.
A miss is handled, in order:
  1. navigation outside the allowlist               -> failed POLICY_BLOCKED               code
  2. a known outcome in the artifact                 -> as remembered                       code
  3. server error (HTTP 5xx)                         -> back off, reload, retry             code
  4. an input's element is missing on the right page -> business_outcome NOT_FOUND          code
  5. every recorded locator failed                   -> the AI says where the element went  AI (--ai only)
  6. anything else                                   -> the AI decides what it means, acts  AI (--ai only)
  7. no AI (the default), or it is unsure            -> a human decides, or failed UNKNOWN_STATE
What the AI or a human decided is remembered in a new draft version, so next time step 2 handles it without them.
Guarantees, whoever acts: the allowlist, approval before risky steps, a risky step is never retried or repeated
by a restart, bounded attempts.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from typing import Literal

from playwright.async_api import Error as PWError
from pydantic import BaseModel, Field

from . import assist
from .artifact import Artifact, Condition, Outcome, Recover, Step, Target, fill, uses_param
from .evidence import RunLog
from .handoff import Controller
from .surface import WebSurface, path_query

MAX_ATTEMPTS = 2  # retries and dismissals per cause; a "start over" happens at most once


class Result(BaseModel):
    """What the caller (an AI agent) gets back."""
    status: Literal["success", "business_outcome", "needs_confirmation", "failed"]
    capability: str
    version: int
    run_id: str
    code: str | None = None  # business outcome / failure code, e.g. NOT_FOUND, UNKNOWN_STATE
    message: str = ""
    outputs: dict = Field(default_factory=dict)
    failed_step: str | None = None
    expected: dict | None = None
    observed: dict | None = None
    recoveries: list[str] = Field(default_factory=list)  # what went wrong and was handled
    locator_fallbacks: list[str] = Field(default_factory=list)  # drift signal: a lower rung or the AI was needed
    ai_decisions: list[str] = Field(default_factory=list)  # every decision the AI made in this run
    human_actions: list[str] = Field(default_factory=list)
    learned: str | None = None  # the new artifact version this run wrote (what the AI or a human taught it)
    evidence: dict = Field(default_factory=dict)
    duration_s: float = 0.0


class Stop(Exception):
    def __init__(self, status: str, **fields):
        super().__init__(status)
        self.fields = {"status": status, **fields}


class InputError(ValueError):
    pass


def validate_params(art: Artifact, params: dict[str, str]) -> dict[str, str]:
    """Check inputs against the artifact's contract before any browser opens."""
    errors = [f"unknown input '{k}'" for k in params if k not in art.inputs]
    for name, spec in art.inputs.items():
        v = params.get(name)
        if v in (None, ""):
            errors.append(f"missing input '{name}'")
        elif spec.type == "number" and not re.fullmatch(r"-?\d+(\.\d+)?", v):
            errors.append(f"input '{name}' must be a number")
    if errors:
        raise InputError("; ".join(errors))
    return params


def coerce(value: str, kind: str):
    if kind in ("money", "number"):
        num = re.sub(r"[^\d.\-]", "", value)
        try:
            return float(num)
        except ValueError:
            return value
    return value


class Replayer:
    def __init__(self, art: Artifact, params: dict[str, str], headless: bool = False, human: bool = True,
                 inject: list[str] = (), ai: bool = False):
        self.art, self.params, self.ai = art, params, ai
        secrets = [params[k] for k, v in art.inputs.items() if v.type == "secret" and k in params]
        self.log = RunLog(art.name, "run", secrets)
        self.surface = WebSurface(art.policy.allowed_domains, headless=headless)
        self.ctl = Controller(self.surface, self.log, human_available=human, can_label=True)
        self.surface.on_human = self.on_human
        self.faults: dict[str, list[str]] = {}
        for spec in inject:  # e.g. "http500@s3"
            kind, _, step = spec.partition("@")
            self.faults.setdefault(step, []).append(kind)
        self.outputs: dict = {}
        self.recoveries: list[str] = []
        self.fallbacks: list[str] = []
        self.ai_decisions: list[str] = []
        self.ai_note = ""  # why the AI left a screen to a human
        self.human: list[str] = []
        self.last_human_target = None
        self.attempts: Counter = Counter()
        self.risky_done = False  # once a risky step ran, nothing may repeat it
        self.repaired: dict[str, Target] = {}  # step id -> its recorded target, while the AI's replacement is in use
        self.changes: list[tuple[str, str]] = []  # (what was learned, by whom): written as one new version at the end
        self.learned: str | None = None

    # ---- entry point ------------------------------------------------------------------------------
    async def run(self) -> Result:
        t0 = time.monotonic()
        self.log.log("start", capability=self.art.name, version=self.art.capability.version, ai=self.ai,
                     inputs={k: ("[secret]" if self.art.inputs[k].type == "secret" else v) for k, v in self.params.items()})
        try:
            await self.surface.open("about:blank")
            i = 0
            while i < len(self.art.steps):
                i = await self.run_step(i)
            await self.verify_success()
            result = await self.finish({"status": "success"}, t0)
        except Stop as s:
            result = await self.finish(s.fields, t0)
        finally:
            await self.surface.close()
        return result

    async def run_step(self, i: int) -> int:
        """Run step i; return the index of the next step to run."""
        step = self.art.steps[i]
        await self.ctl.checkpoint()
        await self.ctl.show(f"Step {i + 1}/{len(self.art.steps)}: {self.said(step)}")
        self.log.log("step_start", step=step.id, intent=step.intent)
        if step.action not in self.art.policy.allowed_actions:
            raise Stop("failed", code="POLICY_BLOCKED", failed_step=step.id,
                       message=f"Action '{step.action}' is not in this capability's allowed_actions.")
        await self.inject(step.id)
        self.surface.doc_status = 200  # only a server error caused by this step counts, not one left from an earlier page
        await self.dismiss_known_interstitials()
        if step.risk == "risky":
            await self.gate(step)
        handle = None
        if step.target:
            # Input-bound targets (the row for {{account_id}}) get a short grace: the page itself was already
            # verified by the previous checkpoint, so absence is an answer, not slowness.
            found = await self.surface.resolve(step.target, self.params, timeout=3 if uses_param(step.target) else 10)
            if not found:
                return await self.on_miss(i, step, f"could not find {fill(step.target.description, self.params)}", missing=True)
            handle, rung = found
            if rung:
                self.fallbacks.append(f"{step.id}: used rung {rung + 1} ({step.target.locators[rung].by})")
        try:
            await self.ctl.checkpoint()
            out = await self.surface.perform(step.action, handle, fill(step.value, self.params))
        except PWError as e:
            err = str(e).splitlines()[0][:200]
            if "Timeout" in err:  # Playwright waited for the element to become clickable/editable
                err = "it did not respond (something may be covering it, or it is disabled)"
            return await self.on_miss(i, step, f"could not {step.action}: {err}")
        self.risky_done = self.risky_done or step.risk == "risky"
        self.faults_off()
        if self.surface.blocked:
            raise Stop("failed", code="POLICY_BLOCKED", failed_step=step.id,
                       message=f"Navigation to {self.surface.blocked[-1]} is outside the allowlist.")
        if step.action == "extract":
            if not out:
                return await self.on_miss(i, step, f"no value found for output '{step.output}'")
            self.outputs[step.output] = coerce(out, self.art.outputs[step.output].type)
        if step.expect and not await self.surface.check(step.expect, self.params, timeout=10):
            return await self.on_miss(i, step, "checkpoint not reached")
        self.log.log("step_ok", step=step.id)
        return i + 1

    # ---- a miss: code handles what it knows, the AI decides the rest -----------------------------------
    async def on_miss(self, i: int, step: Step, why: str, missing: bool = False) -> int:
        self.log.log("step_miss", step=step.id, why=why)
        if self.surface.blocked:
            raise Stop("failed", code="POLICY_BLOCKED", failed_step=step.id,
                       message=f"Navigation to {self.surface.blocked[-1]} is outside the allowlist.")
        for o in self.art.outcomes:
            if await self.surface.check(o.when, self.params):
                return await self.apply(o, i, step)
        if self.surface.doc_status >= 500:
            return await self.retry(i, step, f"HTTP_{self.surface.doc_status}", reload=True)
        if missing and uses_param(step.target) and await self.on_expected_page(i):
            used = sorted(set(re.findall(r"\{\{(\w+)\}\}", " ".join(loc.value or "" for loc in step.target.locators))))
            raise Stop("business_outcome", code="NOT_FOUND", failed_step=step.id,
                       message=f"No match for {', '.join(f'{k}={self.params[k]}' for k in used)} on this page.")
        if missing and await self.repair(step):
            return i  # run the step again with the AI's locator
        if (nxt := await self.decide(i, step, why)) is not None:
            return nxt
        return await self.escalate(i, step, why)

    async def repair(self, step: Step) -> bool:
        """Every recorded locator failed: the AI says where the element is now, and the step runs again with it.
        Not for risky steps (only a human-approved locator may perform them) or input-bound ones (a fixed locator
        would pick the same record for every input). Written back only if the run succeeds."""
        if not self.ai or step.risk == "risky" or uses_param(step.target) or self.attempts[f"ai-repair:{step.id}"]:
            return False
        self.attempts[f"ai-repair:{step.id}"] += 1
        p = await assist.locate(fill(step.target.description, self.params), self.said(step), await self.surface.screenshot())
        found = await self.surface.ground(p.x, p.y, "read" if step.action == "extract" else "act") if p and p.found else None
        self.log.log("ai_repair", step=step.id, found=found[0].description if found else None)
        if not found:
            return False
        self.repaired.setdefault(step.id, step.target)
        step.target = Target(description=step.target.description, frames=found[0].frames,
                             locators=found[0].locators + step.target.locators)
        self.fallbacks.append(f"{step.id}: AI re-found {step.target.description}")
        self.ai_decisions.append(f"{step.id}: re-found {step.target.description} as {found[0].description}")
        return True

    async def decide(self, i: int, step: Step, why: str) -> int | None:
        """The AI decides what an unexpected screen means and acts on it, and the decision is remembered.
        Returns the next step, or None when the AI is off, unsure or unreachable (then a human decides)."""
        if not self.ai or self.attempts[f"ai-decide:{step.id}"]:
            return None
        self.attempts[f"ai-decide:{step.id}"] += 1
        d = await assist.decide(self.art.capability.description, self.said(step), why, self.expected(step),
                                await self.surface.texts(), await self.surface.screenshot())
        self.log.log("ai_decision", step=step.id, decision=d.model_dump() if d else None)
        if not d or d.kind == "unsure":
            self.ai_note = d.reason if d else "AI unavailable"
            return None
        code = re.sub(r"\W+", "_", d.code.upper()).strip("_")[:40] or d.kind.upper()
        self.ai_decisions.append(f"{step.id}: {d.kind} {code}: {d.reason}"[:240])
        target = None
        if d.kind == "dismiss":  # the AI also said what closes it
            found = await self.surface.ground(d.x, d.y, "act")
            target = found[0] if found else None
            handle = await self.surface.resolve(target, self.params, timeout=2) if target else None
            if handle:
                await self.surface.perform("click", handle[0])
        o = self.remember(step, d.kind, code, d.text.strip(), "ai", target)
        if d.kind == "dismiss":
            self.recoveries.append(f"{step.id}: AI closed '{o.id}'")
            return i
        return await self.apply(o, i, step)

    def remember(self, step: Step, kind: str, code: str | None, text: str, source: str, target: Target | None) -> Outcome:
        """What a screen means becomes an outcome of the artifact, so next time code handles it alone."""
        when = Condition(text_visible=text) if text else Condition(url_contains=path_query(self.surface.page.url))
        oid = re.sub(r"[^a-z0-9]+", "_", (code or text or kind).lower()).strip("_")[:40]
        if kind in ("business_outcome", "failure"):
            o = Outcome(id=oid, kind=kind, when=when, code=code or kind.upper(), message=text, source=source)
        else:
            o = Outcome(id=oid, kind="recoverable", when=when, recover=Recover(action=kind, target=target),
                        message=text, source=source)
        self.art.outcomes.append(o)
        by = f"model:{assist.MODEL}" if source == "ai" else "operator"
        self.changes.append((f"learned outcome '{o.id}' ({kind}) at {step.id}", by))
        self.log.log("learned", outcome=o.model_dump(exclude_none=True), by=by)
        return o

    def said(self, step: Step) -> str:
        """A step's intent with this run's (non-secret) inputs filled in, for people reading the screen."""
        return fill(step.intent, {k: v for k, v in self.params.items() if self.art.inputs[k].type != "secret"})

    def expected(self, step: Step) -> dict | None:
        return {k: fill(v, self.params) for k, v in step.expect.model_dump(exclude_none=True).items()} if step.expect else None

    async def on_expected_page(self, i: int) -> bool:
        """Is the page still the one the previous checkpoint promised? (So a missing target is data, not drift.)"""
        prev = next((s.expect for s in reversed(self.art.steps[:i]) if s.expect), None)
        return prev is None or await self.surface.check(prev, self.params)

    async def apply(self, o: Outcome, i: int, step: Step) -> int:
        self.log.log("outcome", id=o.id, kind=o.kind, source=o.source, step=step.id)
        if o.kind == "business_outcome":
            raise Stop("business_outcome", code=o.code, message=o.message, failed_step=step.id)
        if o.kind == "failure":
            raise Stop("failed", code=o.code, message=o.message or f"Known error screen '{o.id}'", failed_step=step.id)
        r = o.recover
        if r.action == "retry":
            return await self.retry(i, step, o.id)
        if r.action == "restart":
            return self.restart(o.id, step)
        self.count(o.id, MAX_ATTEMPTS, step)  # dismiss
        found = await self.surface.resolve(r.target, self.params, timeout=2) if r.target else None
        if found:
            await self.surface.perform("click", found[0])
        self.recoveries.append(f"{step.id}: dismissed '{o.id}'")
        return i  # re-run the interrupted step

    def count(self, key: str, max_attempts: int, step: Step) -> None:
        self.attempts[key] += 1
        if self.attempts[key] > max_attempts:
            raise Stop("failed", code="RECOVERY_EXHAUSTED", failed_step=step.id,
                       message=f"'{key}' kept happening after {max_attempts} recoveries.")

    async def retry(self, i: int, step: Step, why: str, reload: bool = False) -> int:
        if step.risk == "risky":  # never repeat something that may have moved money
            raise Stop("failed", code=why, failed_step=step.id,
                       message="A risky step hit an error; not retried automatically because it may have taken effect.")
        self.count(why, MAX_ATTEMPTS, step)
        self.recoveries.append(f"{step.id}: {why} -> retry")
        await asyncio.sleep(2 * self.attempts[why])  # back off: transient outages usually pass in seconds
        if reload:
            try:
                await self.surface.page.reload(wait_until="load")
            except PWError:
                pass
            if step.expect and await self.surface.check(step.expect, self.params, timeout=5):
                return i + 1
        return i

    def restart(self, why: str, step: Step) -> int:
        if self.risky_done:  # starting over would run the risky step a second time
            raise Stop("failed", code=why, failed_step=step.id,
                       message="The run needs to start over, but a risky step already ran; not repeated automatically.")
        self.count(why, 1, step)
        self.recoveries.append(f"{why} -> start over")
        self.outputs.clear()
        return 0

    # ---- risky steps & humans ---------------------------------------------------------------------
    async def gate(self, step: Step) -> None:
        if self.art.capability.status == "approved":
            self.log.log("risk_gate", step=step.id, decision="allowed: capability approved")
            return
        d = await self.ctl.escalate(f"Approve risky step: {self.said(step)}", ask="approval", capability=self.art.name,
                                    step=step.id, screenshot=await self.shot(f"{step.id}-approve"))
        self.log.log("risk_gate", step=step.id, decision=d["decision"])
        if d["decision"] != "approve":
            raise Stop("needs_confirmation", code="RISKY_STEP_NOT_APPROVED", failed_step=step.id,
                       message=f"'{self.said(step)}' is risky and this capability is still a draft. "
                               f"Approve it with `cua approve {self.art.name}` or confirm when asked.")

    async def escalate(self, i: int, step: Step, why: str) -> int:
        obs = await self.surface.observe()
        self.count(f"escalate:{step.id}", 3, step)
        expected = self.expected(step)
        note, self.ai_note = (f" (AI: {self.ai_note})" if self.ai_note else ""), ""
        d = await self.ctl.escalate(f"Step {i + 1} '{self.said(step)}': {why}{note}", capability=self.art.name, step=step.id,
                                    expected=expected, observed={k: obs[k] for k in ("url", "heading", "text")},
                                    screenshot=await self.shot(f"{step.id}-stuck"))
        if d["decision"] == "unavailable":
            raise Stop("failed", code="UNKNOWN_STATE", failed_step=step.id, message=why + note, expected=expected,
                       observed={k: obs[k] for k in ("url", "heading", "text", "dialogs", "http_status")})
        if d["decision"] == "label":
            return await self.learn(d, i, step)
        if step.expect and await self.surface.check(step.expect, self.params, timeout=2):
            return i + 1  # the human completed the step
        return i

    async def learn(self, d: dict, i: int, step: Step) -> int:
        """An operator labelled this screen ("This screen means…"): remember it, then act on it."""
        kind = d.get("kind")
        target = self.last_human_target if kind == "dismiss" else None
        o = self.remember(step, kind, d.get("code") or None, d.get("text") or "", "human", target)
        if kind == "dismiss":  # the human already closed it
            self.recoveries.append(f"{step.id}: '{o.id}' closed by operator")
            return i
        return await self.apply(o, i, step)

    async def on_human(self, kind: str, target, info: dict, value: str | None) -> None:
        if self.ctl.owner != "human":
            return
        self.human.append(f"{kind} {target.description}")
        if kind == "click":
            self.last_human_target = target
        self.log.log("human_action", kind=kind, target=target.description, value=value)

    # ---- faults, success, result ------------------------------------------------------------------
    async def inject(self, step_id: str) -> None:
        for kind in self.faults.pop(step_id, []):
            self.log.log("fault_injected", kind=kind, step=step_id)
            if kind == "slow":
                self.surface.faults["delay"] = 2.0
            elif kind == "http500":
                self.surface.faults["fail_next_doc"] = True
            elif kind == "expire_session":
                await self.surface.ctx.clear_cookies()
            elif kind == "modal":
                await self.surface.page.evaluate(MODAL_JS)
            else:
                raise ValueError(f"unknown fault '{kind}' (use slow, http500, expire_session, modal)")

    def faults_off(self) -> None:
        self.surface.faults["delay"] = 0.0

    async def dismiss_known_interstitials(self) -> None:
        for o in self.art.outcomes:
            if o.recover and o.recover.action == "dismiss" and await self.surface.check(o.when, self.params):
                found = await self.surface.resolve(o.recover.target, self.params, timeout=1) if o.recover.target else None
                if found:
                    await self.surface.perform("click", found[0])
                    self.recoveries.append(f"dismissed '{o.id}'")
                    self.log.log("recovered", outcome=o.id)

    async def verify_success(self) -> None:
        for cond in self.art.success:
            if not await self.surface.check(cond, self.params, timeout=10):
                for o in self.art.outcomes:
                    if o.kind != "recoverable" and await self.surface.check(o.when, self.params):
                        await self.apply(o, len(self.art.steps) - 1, self.art.steps[-1])
                raise Stop("failed", code="SUCCESS_NOT_VERIFIED", expected=cond.model_dump(exclude_none=True),
                           message="All steps ran but the success condition does not hold.")
        missing = [k for k in self.art.outputs if k not in self.outputs]
        if missing:
            raise Stop("failed", code="MISSING_OUTPUT", message=f"Outputs not extracted: {', '.join(missing)}")

    async def shot(self, label: str) -> str:
        return self.log.image(await self.surface.screenshot(evidence=True), label)

    def write_back(self, completed: bool) -> None:
        """Everything this run learned becomes one new draft version. AI repairs count only if the run completed."""
        if not completed:
            for step_id, recorded in self.repaired.items():
                next(s for s in self.art.steps if s.id == step_id).target = recorded
            self.repaired.clear()
        if self.repaired:
            self.changes.append((f"AI re-found the elements of {', '.join(self.repaired)}", f"model:{assist.MODEL}"))
        if self.changes:
            by = " + ".join(sorted({b for _, b in self.changes}))
            self.art.bump("; ".join(c for c, _ in self.changes), by=by)
            self.learned = str(self.art.save())

    async def finish(self, fields: dict, t0: float) -> Result:
        self.write_back(completed=fields["status"] in ("success", "business_outcome"))
        evidence = {"run_dir": str(self.log.dir), "log": str(self.log.dir / "log.jsonl")}
        if fields["status"] != "success":
            try:
                evidence["screenshot"] = await self.shot("final")
                evidence["a11y_snapshot"] = self.log.write("a11y.txt", (await self.surface.observe())["aria"])
            except PWError:
                pass
        r = Result(capability=self.art.name, version=self.art.capability.version, run_id=self.log.run_id,
                   outputs=self.outputs if fields["status"] == "success" else {}, recoveries=self.recoveries,
                   locator_fallbacks=self.fallbacks, ai_decisions=self.ai_decisions, human_actions=self.human,
                   learned=self.learned, evidence=evidence, duration_s=round(time.monotonic() - t0, 1), **fields)
        on_disk = r.model_dump()
        for k, spec in self.art.outputs.items():
            if spec.sensitive and k in on_disk["outputs"]:
                on_disk["outputs"][k] = "[sensitive]"
        self.log.write("result.json", on_disk)
        self.log.log("result", status=r.status, code=r.code)
        return r


MODAL_JS = """() => {
  const d = document.createElement('div');
  d.id = 'cua-fault-modal';
  d.style.cssText = 'position:fixed;inset:0;background:#0008;display:flex;align-items:center;justify-content:center;z-index:99999';
  d.innerHTML = '<div role="dialog" style="background:#fff;padding:24px;border-radius:8px;font:16px sans-serif">' +
    '<h2>System notice</h2><p>Scheduled maintenance tonight at 11 PM.</p><button>OK</button></div>';
  d.querySelector('button').onclick = () => d.remove();
  document.body.appendChild(d);
}"""
