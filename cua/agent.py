"""Discovery: the AI decides, the recorder remembers.

Before recording, the AI reads the goal and decides the inputs. While recording, Gemini decides every action from
screenshots, and the recorder grounds each one to verified locators (pixels in, locators out). After recording,
the AI decides the checkpoints, the success condition and which steps are risky. Code applies each AI answer as
given; replay then runs the result, and consults the AI again only for the unexpected.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import get_args
from urllib.parse import urlsplit

from google import genai

from . import artifact as A
from . import assist
from .artifact import Artifact, Condition, Step, Target, fill, templatize
from .evidence import RunLog
from .handoff import Controller
from .policy import default_domain, domain_allowed
from .surface import WebSurface, canonical, path_query

MODEL = "gemini-3.8-flash"
ALLOWED_ACTIONS = ["navigate", "click", "double_click", "hover", "type", "select", "press_key", "go_back", "extract"]
# Predefined computer-use actions we don't offer: no stable replay meaning yet (drag paths, raw mouse/key state).
EXCLUDED = ["triple_click", "middle_click", "right_click", "mouse_down", "mouse_up", "drag_and_drop",
            "key_down", "key_up", "go_forward"]
POINTER = {"click": "click", "double_click": "double_click", "move": "hover"}  # model action -> recorded action
OUTPUT_TYPES = get_args(A.Output.model_fields["type"].annotation)


def _fn(fname: str, description: str, /, **props: str) -> dict:
    types = {"x": "integer", "y": "integer"}
    return {"type": "function", "name": fname, "description": description,
            "parameters": {"type": "object", "required": list(props),
                           "properties": {k: {"type": types.get(k, "string"), "description": d} for k, d in props.items()}}}


XY = dict(x="Horizontal position on the 0-999 grid", y="Vertical position on the 0-999 grid")
TOOLS = [
    {"type": "computer_use", "environment": "browser", "excluded_predefined_functions": EXCLUDED},
    _fn("extract_output", "Record a value the task asks you to read. Point at the element that contains just that value.",
        name="snake_case output name, e.g. balance", description="What the value is",
        type="One of: string, number, money, date", sensitive="yes if it is personal or confidential data, else no", **XY),
    _fn("task_complete", "Call once the task is fully achieved.", summary="One sentence on what was achieved",
        capability_name="Short kebab-case name for this reusable task, e.g. get-account-balance"),
    _fn("select_option", "Choose an option in a dropdown (<select>). Use this instead of clicking or pressing keys on "
        "dropdowns: their option lists are not visible in screenshots.",
        option="The visible text of the option to choose", **XY),
    _fn("request_human", "Call when you cannot proceed safely: CAPTCHA, missing information, an error you cannot resolve.",
        reason="Why a human is needed"),
]

PROMPT = """You are operating a web browser to complete a task for a user.
Task: {goal}

Rules:
- Stay on these sites: {domains}. Do not open other sites.
- Secrets appear as placeholders like {{{{secret:password}}}}. Type the placeholder exactly as written; it is replaced
  with the real value after you type it. Never ask for or guess secret values.
- Click a field before typing into it. For dropdowns use select_option.
- Set every field the task mentions explicitly, even if it already shows the right value.
- When a value the task asks you to read is displayed, call extract_output pointing at the element containing just that value.
- When the task is complete, call task_complete.
- If you are stuck, or something needs a human decision, call request_human.
- Do not submit anything that moves money or changes data unless the task explicitly asks for it.
"""

_SECRET = re.compile(r"\{(\w+):secret\}")
KEYS = {"enter": "Enter", "return": "Enter", "tab": "Tab", "escape": "Escape", "esc": "Escape", "backspace": "Backspace",
        "ctrl": "Control", "control": "Control", "cmd": "Meta", "meta": "Meta", "shift": "Shift", "alt": "Alt",
        "space": "Space", "delete": "Delete", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight"}


def parse_goal(goal: str) -> tuple[str, list[str]]:
    """'Log in with {password:secret}' -> ('Log in with {{secret:password}}', ['password']).
    Secrets are the only thing a user marks, because the model must never see their values.
    Everything else in the goal is read by the AI (assist.plan_goal)."""
    return _SECRET.sub(lambda m: "{{secret:" + m[1] + "}}", goal), _SECRET.findall(goal)


def site_name(url: str) -> str:
    host = [p for p in (urlsplit(url).hostname or "site").split(".") if p not in ("www", "com", "org", "net")]
    return host[0] if host else "site"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50]


class Stopped(Exception):
    """Ends a recording cleanly: denied risky action, no human to answer, out of turns or out of time."""


class Recorder:
    """Owns one discovery run: executes the model's actions via locators and records them as Steps."""

    def __init__(self, url: str, goal: str, model: str, human: bool, headless: bool, secrets: dict[str, str],
                 name: str | None = None, allow: list[str] = ()):
        self.url, self.model, self.user_name = url, model, name
        self.goal, self.secret_names = parse_goal(goal)  # what the model sees: secrets as placeholders
        self.template = re.sub(r"\{\{secret:(\w+)\}\}", r"{\1}", self.goal)  # the capability's description
        self.inputs = {n: A.Input(type="secret", description="Secret: read from the environment, never stored")
                       for n in self.secret_names}
        self.examples: dict[str, str] = {}  # input name -> the value used while recording (filled by apply_plan)
        self.secrets = secrets  # name -> real value (memory only)
        self.domains = [default_domain(url), *allow]
        self.log = RunLog(site_name(url), "record", list(secrets.values()))
        self.surface = WebSurface(self.domains, headless=headless)
        self.surface.examples = self.examples
        self.ctl = Controller(self.surface, self.log, human_available=human)
        self.surface.on_human = self.on_human
        self.steps: list[Step] = []
        self.outputs: dict[str, A.Output] = {}
        self.human_actions: list[str] = []
        self.done, self.name, self.summary, self.stop_reason = False, None, "", ""
        self.after: dict[str, dict] = {}  # step id -> {url, new_texts} seen after it: what the AI review chooses from
        self.final: dict = {"url": "", "texts": []}
        self.review_notes: list[str] = []

    # ---- helpers --------------------------------------------------------------------------------
    def _t(self, text: str | None) -> str | None:
        return templatize(text, self.examples)

    def _target(self, target: Target) -> Target:
        """Replace example input values inside locators with {{placeholders}}."""
        t = target.model_copy(deep=True)
        t.description = self._t(t.description)
        for loc in t.locators:
            loc.value = self._t(loc.value)
        if A.uses_param(t):
            # The element's identity depends on the input (e.g. the link for account {{account_id}}).
            # Structural rungs without the input would silently pick another record, so drop them.
            t.locators = [loc for loc in t.locators if loc.value and "{{" in loc.value]
        return t

    def _add(self, **kw) -> Step:
        step = Step(id=f"s{len(self.steps) + 1}", **kw)
        self.steps.append(step)
        self.log.log("step", step=step.id, **step.model_dump(exclude_none=True, exclude={"id"}))
        return step

    async def _shot(self, label: str) -> str:
        return self.log.image(await self.surface.screenshot(evidence=True), label)

    async def _snapshot(self, step: Step, before: set[str]) -> None:
        """What the page shows after a step (templated): the material the AI review picks checkpoints from."""
        if self.surface.doc_status >= 500:  # an error page is not a state to check for
            self.after[step.id] = {"url": "", "new_texts": []}
            return
        texts = [self._t(t) for t in await self.surface.texts() if t not in before]
        self.after[step.id] = {"url": self._t(path_query(self.surface.page.url)), "new_texts": texts[:40]}

    def apply_plan(self, plan: assist.Plan) -> None:
        """The AI decided which values in the goal are inputs; apply its answer as given."""
        for p in plan.inputs:
            name = slug(p.name).replace("-", "_") or "input"
            self.examples[name] = p.example
            self.inputs.setdefault(name, A.Input(type=p.type, description=p.description))  # a secret keeps its name
        self.template = re.sub(r"\{\{(?:secret:)?(\w+)\}\}", r"{\1}", templatize(self.goal, self.examples))

    # ---- the loop -------------------------------------------------------------------------------
    async def run(self, max_steps: int = 40, timeout: float = 600) -> Artifact | None:
        deadline = time.monotonic() + timeout  # checked before each model turn: never mid-action or while a person works
        try:
            await self.surface.open(self.url)
            plan = await assist.plan_goal(self.goal)
            if plan is None:
                raise Stopped("the AI could not read the goal (check GEMINI_API_KEY and the network)")
            self.apply_plan(plan)
            self.log.log("plan", inputs={k: v.type for k, v in self.inputs.items()}, template=self.template)
            await self._snapshot(self._add(intent="Open the start page", action="navigate", value=canonical(self.url)), set())
            await self.ctl.show("Recording: " + self.template)
            client = genai.Client()
            prompt = PROMPT.format(goal=self.goal, domains=", ".join(self.domains))
            self.log.log("start", goal=self.goal, url=self.url, model=self.model, allowed_domains=self.domains)
            inp: list = [{"type": "text", "text": prompt + f"\nCurrent URL: {self.surface.page.url}"}, await self._image()]
            prev, nudges, last_hash, same = None, 0, None, 0
            for turn in range(max_steps):
                if time.monotonic() > deadline:
                    raise Stopped(f"time limit of {timeout:g}s reached before the goal was met")
                kw = {"previous_interaction_id": prev} if prev else {}
                it = await client.aio.interactions.create(model=self.model, input=inp, tools=TOOLS, **kw)
                prev = it.id
                calls = [s for s in it.steps if s.type == "function_call"]
                text = " ".join(getattr(part, "text", "") or "" for s in it.steps for part in getattr(s, "content", None) or [])
                self.log.log("model", turn=turn, text=text, calls=[{"name": c.name, "args": c.arguments} for c in calls])
                if not calls:
                    inp = [{"type": "text", "text": await self.answer(text)}]
                    nudges += 1
                    if nudges > 3:
                        raise Stopped("the model kept replying in text instead of acting")
                    continue
                nudges = 0
                results = []
                for c in calls:
                    out = await self.handle(c.name, dict(c.arguments or {}))
                    results.append(await self._result(c, out))
                    if self.done:
                        break
                if self.done:
                    break
                h = hashlib.md5(await self.surface.screenshot()).hexdigest()
                same = same + 1 if h == last_hash else 0
                last_hash = h
                if same >= 3:  # no visible progress for 3 turns: stuck
                    d = await self.ctl.escalate("The agent is making no progress.", goal=self.template, url=self.surface.page.url,
                                                screenshot=await self._shot("stuck"))
                    note, last = self._human_note(d), results[-1]["result"]
                    if isinstance(last, dict):
                        last["note"] = note
                    else:
                        last[0]["text"] = json.dumps({"url": self.surface.page.url, "note": note})
                    same = 0
                inp = results
            if not self.done:
                raise Stopped(f"no task_complete within {max_steps} model turns")
            return await self.finish()
        except Stopped as e:
            self.stop_reason = str(e)
            self.log.log("stopped", reason=self.stop_reason)
            return None
        finally:
            await self.surface.close()

    async def _image(self) -> dict:
        return {"type": "image", "data": base64.b64encode(await self.surface.screenshot()).decode(), "mime_type": "image/png"}

    async def _result(self, call, out: dict) -> dict:
        body = {"url": self.surface.page.url, **out}
        if body.get("safety_acknowledgement"):
            # The API only accepts the acknowledgement when the result is a plain object (a text+image result is
            # rejected with 400, despite the docs), and an object result cannot carry the screenshot.
            body["note"] = (body.get("note", "") + " Take a screenshot to see the result.").strip()
            return {"type": "function_result", "name": call.name, "call_id": call.id, "result": body}
        return {"type": "function_result", "name": call.name, "call_id": call.id,
                "result": [{"type": "text", "text": json.dumps(body)}, await self._image()]}

    async def answer(self, text: str) -> str:
        """The model replied in words instead of acting, usually asking for confirmation. A human answers, never us."""
        d = await self.ctl.escalate(f"The agent asks: {text[:300] or '(no message)'}", ask="approval",
                                    goal=self.template, screenshot=await self._shot("question"))
        if d["decision"] == "approve":
            return "The operator confirms: yes, proceed with the task."
        if d["decision"] == "deny":
            raise Stopped("the operator said no to the agent's question")
        if d["decision"] == "hand_back":
            return self._human_note(d) + " Continue the task."
        return ("No human is available to answer. Continue only if the task explicitly asks for this; "
                "otherwise call request_human.")

    def _human_note(self, decision: dict) -> str:
        done = "; ".join(self.human_actions[-10:]) or "nothing"
        self.human_actions.clear()
        return f"Operator decision: {decision['decision']}. The human operator did: {done}. Control is back with you."

    # ---- one model action -----------------------------------------------------------------------
    async def handle(self, name: str, args: dict) -> dict:
        intent = self._t(args.pop("intent", "")) or ""  # custom tools have no intent; their branches describe themselves
        acked = False
        if sd := args.pop("safety_decision", None):
            d = await self.ctl.escalate(f"Confirm risky action: {sd.get('explanation', intent)}", ask="approval",
                                        goal=self.template, step=intent, screenshot=await self._shot("confirm"))
            if d["decision"] == "hand_back":  # the operator took over and handled this step themselves
                return {"status": "handled by the operator", "note": self._human_note(d), "safety_acknowledgement": True}
            if d["decision"] != "approve":  # the API does not allow continuing past an unacknowledged safety decision
                raise Stopped(f"the operator did not approve: {intent or name}")
            acked = True
        await self.ctl.checkpoint()  # waits here if the operator took over on their own
        await self.ctl.show(f"Step {len(self.steps) + 1}: {intent or name.replace('_', ' ')}")
        before = len(self.steps)
        out = await self._do(name, args, intent, acked)
        if sd:  # the model flagged this action as risky: so is the recorded step
            for st in self.steps[before:]:
                st.risk, st.risk_reason = "risky", f"model safety flag: {sd.get('explanation', '')}"[:200]
        if self.human_actions:  # the operator stepped in unasked: tell the model what changed
            out["operator_note"] = self._human_note({"decision": "hand_back"})
        if acked:
            out["safety_acknowledgement"] = True
        return out

    async def _do(self, name: str, args: dict, intent: str, acked: bool) -> dict:
        s = self.surface
        if name in POINTER:
            action = POINTER[name]
            found = await s.ground(args["x"], args["y"], "act")
            if not found:
                return {"error": "Nothing clickable at that point."}
            target, info = found
            # A click that only focuses a text field is not recorded: the following "type" step targets the field itself.
            focus_only = (action == "click" and info.get("tag") in ("input", "textarea")
                          and info.get("role") not in ("button", "checkbox", "radio"))
            return await self._act(action, target, None, intent, record=not focus_only)
        if name == "type":
            found = await s.focused()
            if not found:
                return {"error": "No field is focused. Click the field first."}
            target, info = found
            text = args.get("text", "")
            out = await self._act("type", target, text, intent)
            if args.get("press_enter"):
                out = await self._act("press_key", target, "Enter", intent + " (Enter)")
            return out
        if name in ("press_key", "hotkey"):
            keys = args.get("keys") or [args.get("key", "")]
            key = "+".join(KEYS.get(k.lower(), k) for k in keys)
            found = await s.focused()
            return await self._act("press_key", found[0] if found else None, key, intent)
        if name == "navigate":
            url = args["url"]
            if not domain_allowed(url, self.domains):
                self.log.log("policy_block", url=url)
                return {"error": f"Blocked by policy: only {', '.join(self.domains)} is allowed."}
            return await self._act("navigate", None, url, intent)
        if name == "go_back":
            return await self._act("go_back", None, None, intent)
        if name == "scroll":  # not recorded: replay locators scroll elements into view themselves
            await s.page.mouse.move(args.get("x", 500) / 1000 * 1280, args.get("y", 500) / 1000 * 800)
            dy = args.get("magnitude_in_pixels", 300) * (-1 if args.get("direction") in ("up", "left") else 1)
            await s.page.mouse.wheel(0, dy)
            return {"status": "ok"}
        if name in ("wait", "take_screenshot"):  # not recorded: replay waits on checkpoints instead
            await s.settle(min(float(args.get("seconds", 1)), 5))
            return {"status": "ok"}
        if name == "select_option":
            found = await s.ground(args["x"], args["y"], "act")
            if not found or found[1].get("tag") != "select":
                return {"error": "That is not a dropdown. Point at the dropdown itself."}
            option = args.get("option", "")
            intent = intent or self._t(f"Choose {option} in {found[0].description}")
            out = await self._act("select", found[0], option, intent)
            if "error" in out:
                handle = (await s.resolve(found[0], {}, 2))[0]
                options = await handle.evaluate("e => [...e.options].map(o => o.text.trim())")
                out["error"] = f"No option '{args.get('option')}'. Options: {options[:30]}"
            return out
        if name == "extract_output":
            found = await s.ground(args["x"], args["y"], "read")
            if not found:
                return {"error": "No text at that point."}
            target, _ = found
            handle = await s.resolve(target, {}, 5)
            value = await s.perform("extract", handle[0]) if handle else ""
            out = slug(args["name"]).replace("-", "_")
            desc = self._t(args.get("description", ""))
            kind = args.get("type") if args.get("type") in OUTPUT_TYPES else "string"
            sensitive = str(args.get("sensitive", "")).strip().lower() in ("yes", "true")
            self.outputs[out] = A.Output(type=kind, description=desc, sensitive=sensitive)
            self._add(intent=f"Read {out}" + (f": {desc}" if desc else ""), action="extract",
                      target=self._target(target), output=out)
            return {"value": value}
        if name == "task_complete":
            self.name, self.summary, self.done = args.get("capability_name"), args.get("summary", ""), True
            self.final = {"url": self._t(path_query(s.page.url)), "texts": [self._t(t) for t in await s.texts()]}
            await self._shot("done")
            return {"status": "recorded"}
        if name == "request_human":
            d = await self.ctl.escalate(args.get("reason", "The agent asked for help."), goal=self.template,
                                        url=s.page.url, screenshot=await self._shot("help"))
            return {"note": self._human_note(d)}
        return {"error": f"Action '{name}' is not allowed by policy."}

    async def _act(self, action: str, target: Target | None, value: str | None, intent: str, record: bool = True) -> dict:
        """Execute through the verified locator (not the coordinates), so what we record is what ran."""
        s = self.surface
        pre_texts = set(await s.texts()) if action not in ("type", "select") else set()
        reopen = action == "navigate" and self.steps[-1].action == "navigate" and self.steps[-1].value == self._t(canonical(value))
        record = record and not reopen  # re-opening the page we are on is a retry (e.g. after a 502), not a step
        handle = None
        if target:
            found = await s.resolve(target, {}, 5)
            if not found:
                return {"error": "The element could not be located again."}
            handle = found[0]
        real = fill(value, self.secrets) if action == "type" else value
        try:
            await s.perform(action, handle, real)
        except Exception as e:  # report to the model; it can choose another way
            self.log.log("action_error", action=action, error=str(e)[:300])
            return {"error": f"{action} failed: {str(e)[:200]}"}
        await s.settle(0.8)  # record-time settle so the checkpoint reflects the resulting page
        if s.blocked:
            return {"error": f"Blocked by policy: navigation to {s.blocked.pop()} is not allowed."}
        if reopen:
            await self._snapshot(self.steps[-1], set())  # what that page really shows, now that it loaded
        if not record:
            return {"status": "ok"}
        recorded = self._t(canonical(value)) if action == "navigate" else self._t(value)
        step = self._add(intent=intent, action=action, target=self._target(target) if target else None, value=recorded)
        if action not in ("type", "select"):
            await self._snapshot(step, pre_texts)
        await self._shot(step.id)
        return {"status": "ok"}

    async def on_human(self, kind: str, target: Target, info: dict, value: str | None) -> None:
        """While a human holds control, their actions are grounded and recorded like the model's."""
        if self.ctl.owner != "human":
            return
        if kind == "type":
            if info.get("password"):
                if not self.secret_names:
                    return
                value = "{{secret:" + self.secret_names[0] + "}}"
            self._add(intent=f"(human) type into {target.description}", action="type", target=self._target(target),
                      value=self._t(value), actor="human")
            self.human_actions.append(f"typed into {target.description}")
        elif kind == "select":
            self._add(intent=f"(human) choose {value} in {target.description}", action="select", target=self._target(target),
                      value=self._t(value), actor="human")
            self.human_actions.append(f"chose {value} in {target.description}")
        else:
            self._add(intent=f"(human) click {target.description}", action="click", target=self._target(target),
                      actor="human")
            self.human_actions.append(f"clicked {target.description}")

    # ---- the artifact ---------------------------------------------------------------------------
    async def finish(self) -> Artifact:
        name = self.user_name or f"{site_name(self.url)}-{slug(self.name or self.template)}"
        version = A.latest_version(name) + 1
        art = Artifact(
            schema_version=A.SCHEMA_VERSION,
            capability=A.Capability(id=name, version=version, status="draft", description=self.template,
                                    start_url=canonical(self.url), surface="web"),
            inputs=self.inputs, outputs=self.outputs,
            policy=A.Policy(allowed_domains=self.domains, allowed_actions=ALLOWED_ACTIONS),
            steps=self.steps,
            provenance=A.Provenance(recorded_at=A.now(), model=self.model, discovery_run=self.log.run_id,
                                    history=[A.Change(version=version, change=f"recorded from goal (run {self.log.run_id})",
                                                      by=f"model:{self.model}", at=A.now())]),
        )
        art = await self.review(art)
        path = art.save()
        self.log.log("artifact", path=str(path), model_summary=self.summary)  # summaries can quote data: log only
        return art

    async def review(self, art: Artifact) -> Artifact:
        """The AI decides each step's checkpoint, the success condition and which steps are risky. Applied as given."""
        brief = [{"id": s.id, "action": s.action, "intent": s.intent, "target": s.target.description if s.target else None,
                  "value": s.value, **self.after.get(s.id, {})} for s in art.steps]
        r = await assist.review(self.template, brief, self.final["url"], self.final["texts"][:120])
        if not r:
            self.review_notes = ["⚠ AI review unavailable: no checkpoints or risk labels; record again to add them"]
            return art

        def condition(url: str, text: str) -> Condition | None:
            url, text = self._t(url.strip()) or None, self._t(text.strip()) or None
            return Condition(url_contains=url, text_visible=text) if url or text else None

        steps = {s.id: s for s in art.steps}
        for c in r.checkpoints:
            if c.step in steps:
                steps[c.step].expect = condition(c.url_contains, c.text)
        success = condition(r.success_url_contains, r.success_text)
        art.success = [success] if success else []
        notes = [f"checkpoints on {sum(1 for s in art.steps if s.expect)} steps",
                 "success: " + (" and ".join(f"{k} {v!r}" for k, v in success.model_dump(exclude_none=True).items()) if success else "none")]
        for x in r.risky:  # added to any step Gemini already flagged while acting
            if x.step in steps:
                steps[x.step].risk, steps[x.step].risk_reason = "risky", f"AI review: {x.reason}"[:200]
                notes.append(f"{x.step} is risky: {x.reason}")
        self.log.log("review", decisions=r.model_dump(), notes=notes)
        art.provenance.history.append(A.Change(version=art.capability.version, change="AI review: " + "; ".join(notes),
                                               by=f"model:{assist.MODEL}", at=A.now()))
        self.review_notes = notes
        return art
