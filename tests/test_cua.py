"""Offline end-to-end tests: no network, no API key.

Scripted stand-ins play every AI part (the computer-use model, and each assist decision), so the tests exercise the
real recorder and replayer against a local "legacy bank" (table layouts, no ids, late-loading values, cookie session).
The AI's decisions are applied as given; the tests check what code owns: execution, guarantees, memory.
"""
import asyncio
import functools
import http.server
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from cua import agent, assist
from cua import artifact as A
from cua.policy import default_domain, domain_allowed, redact
from cua.replay import Replayer

SITE = Path(__file__).parent / "site"
GOAL = "Log in as jdoe with {pw:secret} and read the balance of account 11111"
PLAN = assist.Plan(inputs=[assist.InputPlan(name="username", example="jdoe", type="string", description="Login name"),
                           assist.InputPlan(name="account_id", example="11111", type="number", description="Account")])
REVIEW = assist.Review(  # what the AI decides for the fixture flow
    checkpoints=[assist.Check(step="s1", url_contains="", text="Customer Login"),
                 assist.Check(step="s4", url_contains="/accounts.html", text="Accounts Overview"),
                 assist.Check(step="s5", url_contains="/account.html?id={{account_id}}", text="Account Details")],
    success_url_contains="/account.html?id={{account_id}}", success_text="Account Details", risky=[])
SECRET = "hunter2-secret"
PARAMS = {"username": "jdoe", "pw": SECRET, "account_id": "11111"}


@pytest.fixture(scope="module", autouse=True)
def workdir(tmp_path_factory):
    """Every test writes artifacts/ and runs/ into a temp folder, never into the repo. No test calls the real model:
    the goal plan and the review are scripted; every other AI call answers None (as if the AI were unreachable)."""
    old, saved = os.getcwd(), (assist.ask, assist.plan_goal, assist.review)
    os.chdir(tmp_path_factory.mktemp("work"))

    async def offline(*a, **k):
        return None

    async def plan(*a, **k):
        return PLAN

    async def review(*a, **k):
        return REVIEW
    assist.ask, assist.plan_goal, assist.review = offline, plan, review
    yield
    os.chdir(old)
    assist.ask, assist.plan_goal, assist.review = saved


@pytest.fixture(scope="module")
def server():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = functools.partial(Quiet, directory=str(SITE))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


def act(action, selector=None, /, **args):
    """One scripted model turn: an action at the centre of `selector`, on the model's 0-999 grid."""
    async def turn(surface):
        if selector:
            box = await surface.page.locator(selector).bounding_box()
            args.update(x=int((box["x"] + box["width"] / 2) / 1280 * 1000), y=int((box["y"] + box["height"] / 2) / 800 * 1000))
        return action, {"intent": action, **args}
    return turn


SCRIPT = [
    act("click", "input[name=u]", intent="Click the username field"),
    act("type", text="jdoe", intent="Type the username"),
    act("click", "input[name=p]", intent="Click the password field"),
    act("type", text="{{secret:pw}}", intent="Type the password"),
    act("click", "input[type=submit]", intent="Click Log In"),
    act("click", "tr:has-text('11111') >> text=View", intent="Open account 11111"),
    act("select_option", "select[name=to]", option="22222", intent="Choose the destination account"),
    act("extract_output", "td.v >> nth=1", name="balance", description="Current balance", type="money", sensitive="no"),
    act("task_complete", summary="Read the balance", capability_name="get-balance"),
]


class FakeModel:
    def __init__(self, rec, script=SCRIPT):
        self.rec, self.turns, self.n, self.inputs = rec, iter(script), 0, []
        self.aio = SimpleNamespace(interactions=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        self.inputs.append(kw["input"])
        name, args = await next(self.turns)(self.rec.surface)
        self.n += 1
        call = SimpleNamespace(type="function_call", name=name, arguments=args, id=f"c{self.n}")
        return SimpleNamespace(id=f"i{self.n}", steps=[call])


@pytest.fixture(scope="module")
def recorded(server):
    rec = agent.Recorder(server, GOAL, "fake", human=False, headless=True, secrets={"pw": SECRET}, name="bank-balance")
    agent.genai.Client = lambda: FakeModel(rec)
    art = asyncio.run(rec.run())
    assert art, "recording failed"
    return art


async def until(check, what="the run to ask for a human", seconds=30):
    """Fake-operator wait: fail loudly instead of hanging when the expected state never comes."""
    for _ in range(seconds * 20):
        if check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def replay(art, inject=(), holder=None, ai=False, **params):
    """Replay headless with no human. `holder` receives the Replayer, for fakes that look at its live page."""
    rp = Replayer(art, {**PARAMS, **params}, headless=True, human=False, inject=list(inject), ai=ai)
    if holder is not None:
        holder["rp"] = rp
    return asyncio.run(rp.run())


def fresh(art):
    return art.model_copy(deep=True)


# ---- recording ------------------------------------------------------------------------------------
def test_recording_produces_parameterized_verified_steps(recorded):
    art = recorded
    assert [s.action for s in art.steps] == ["navigate", "type", "type", "click", "click", "select", "extract"]
    assert art.steps[5].value == "22222" and art.steps[5].target.locators[0].by == "near_text"
    assert art.inputs["account_id"].type == "number" and art.inputs["pw"].type == "secret"
    assert art.outputs["balance"].type == "money"
    username, password, login, account = art.steps[1:5]
    assert username.value == "{{username}}" and password.value == "{{secret:pw}}"
    assert username.target.locators[0].by == "near_text" and username.target.locators[0].value == "Username:"
    row = account.target.locators[0]  # "the View button in the row for {{account_id}}"
    assert (row.by, row.value, row.inner.by, row.inner.value) == ("row", "{{account_id}}", "role", "View")
    assert all("{{account_id}}" in (loc.value or "") for loc in account.target.locators)  # no rung can pick another row
    assert account.expect.url_contains == "/account.html?id={{account_id}}"  # the AI review's checkpoints
    assert login.expect.text_visible == "Accounts Overview"
    assert art.capability.description == "Log in as {username} with {pw} and read the balance of account {account_id}"


def test_secrets_never_reach_disk(recorded):
    replay(recorded)
    leaked = [p for p in Path(".").rglob("*") if p.is_file() and SECRET.encode() in p.read_bytes()]
    assert leaked == []


# ---- replay outcomes ------------------------------------------------------------------------------
def test_success_returns_typed_output(recorded):
    r = replay(recorded)
    assert r.status == "success", r
    assert r.outputs == {"balance": 515.5}


def test_other_account_same_artifact(recorded):
    assert replay(recorded, account_id="22222").outputs == {"balance": 20.0}


def test_unknown_account_is_a_business_outcome(recorded):
    r = replay(recorded, account_id="99999")
    assert (r.status, r.code) == ("business_outcome", "NOT_FOUND")


def test_server_error_is_recovered(recorded):
    r = replay(recorded, inject=["http500@s5"])
    assert r.status == "success" and any("HTTP_503" in x for x in r.recoveries)


def test_slow_load_is_waited_out(recorded):
    assert replay(recorded, inject=["slow@s5"]).status == "success"


def decides(monkeypatch, kind, code, text, at=None, holder=None):
    """Script the AI's decision about an unexpected screen (at: the selector it points at to close a pop-up)."""
    async def fake(*a, **k):
        x = y = 0
        if at:
            box = await holder["rp"].surface.page.locator(at).bounding_box()
            x, y = int((box["x"] + box["width"] / 2) / 1280 * 1000), int((box["y"] + box["height"] / 2) / 800 * 1000)
        return assist.Decision(kind=kind, code=code, text=text, reason="scripted", x=x, y=y)
    monkeypatch.setattr(assist, "decide", fake)


def test_expired_session_the_ai_decides_restart_and_it_is_remembered(recorded, monkeypatch):
    art = A.load(str(fresh(recorded).save()))
    assert replay(art, inject=["expire_session@s5"]).code == "UNKNOWN_STATE"  # no --ai, no human: nobody decides
    decides(monkeypatch, "restart", "LOGGED OUT", "An internal error has occurred.")
    r = replay(art, inject=["expire_session@s5"], ai=True)
    assert r.status == "success" and "LOGGED_OUT" in r.ai_decisions[0] and any("start over" in x for x in r.recoveries)
    learned = A.load(r.learned)
    assert (learned.outcomes[-1].recover.action, learned.outcomes[-1].source) == ("restart", "ai")
    monkeypatch.setattr(assist, "decide", None)  # next time code handles it alone (calling the AI would crash)
    r2 = replay(learned, inject=["expire_session@s5"])
    assert r2.status == "success" and r2.ai_decisions == []


def test_unknown_popup_fails_with_evidence_when_no_ai_and_no_human(recorded):
    r = replay(recorded, inject=["modal@s5"])  # default: no AI, and no human here
    assert (r.status, r.code, r.failed_step) == ("failed", "UNKNOWN_STATE", "s5")
    assert Path(r.evidence["screenshot"]).exists() and Path(r.evidence["a11y_snapshot"]).exists()


def test_a_stale_server_error_is_not_blamed_for_a_later_miss(recorded, monkeypatch):
    """Only a server error during the current step counts. (ParaBank once served a working page with HTTP 500, and the
    stale status made a later, unknown pop-up look like a server error, so no human was asked.)"""
    inject = Replayer.inject

    async def stale(self, step_id):
        await inject(self, step_id)
        if step_id == "s5":
            self.surface.doc_status = 500  # left over from an earlier page
    monkeypatch.setattr(Replayer, "inject", stale)
    r = replay(recorded, inject=["modal@s5"])
    assert (r.status, r.code) == ("failed", "UNKNOWN_STATE") and r.recoveries == []


def test_learned_popup_is_dismissed(recorded):
    art = fresh(recorded)
    ok = A.Target(description='button "OK"', locators=[A.Locator(by="role", role="button", value="OK")])
    art.outcomes.append(A.Outcome(id="system_notice", kind="recoverable", when=A.Condition(text_visible="System notice"),
                                  recover=A.Recover(action="dismiss", target=ok)))
    r = replay(art, inject=["modal@s5"])
    assert r.status == "success" and any("system_notice" in x for x in r.recoveries)


def test_learned_business_outcome(recorded):
    art = fresh(recorded)
    art.outcomes.append(A.Outcome(id="closed", kind="business_outcome", code="ACCOUNT_CLOSED",
                                  when=A.Condition(text_visible="System notice")))
    assert replay(art, inject=["modal@s5"]).code == "ACCOUNT_CLOSED"


def test_human_takeover_teaches_the_artifact(recorded):
    """Escalate on an unknown pop-up -> operator takes over the SAME session, closes it, labels it ->
    artifact v2 is saved -> the next run handles the pop-up with no human."""
    art = A.load(str(fresh(recorded).save()))  # exactly the recorded version, not a later learned one
    started_at = art.capability.version

    async def operator(rp):
        await until(lambda: rp.ctl.state == "awaiting_human")
        page = rp.surface.page
        await page.evaluate("() => window.__cua_ui('take_over', {})")  # the bar's Take over button
        await page.click("text=OK")  # the human closes the pop-up; captured as a human action
        await asyncio.sleep(0.3)
        await page.evaluate("() => window.__cua_ui('label', {kind: 'dismiss', code: '', text: 'System notice'})")

    async def both():
        rp = Replayer(art, PARAMS, headless=True, human=True, inject=["modal@s5"])
        result, _ = await asyncio.gather(rp.run(), operator(rp))
        return result

    r = asyncio.run(both())
    assert r.status == "success" and r.human_actions == ['click button "OK"'] and r.learned
    learned = A.load(r.learned)  # a new draft version, whatever number the earlier tests left
    assert learned.capability.version > started_at and learned.outcomes[-1].recover.action == "dismiss"
    r2 = replay(learned, inject=["modal@s5"])
    assert r2.status == "success" and r2.human_actions == [] and any("dismissed" in x for x in r2.recoveries)


def test_locator_fallback_is_reported(recorded):
    art = fresh(recorded)
    art.steps[1].target.locators[0].value = "Renamed label:"  # simulate a tenant/version relabel
    r = replay(art)
    assert r.status == "success" and r.locator_fallbacks


def test_disallowed_action_is_blocked(recorded):
    art = fresh(recorded)
    art.policy.allowed_actions.remove("extract")
    r = replay(art)
    assert (r.code, r.failed_step) == ("POLICY_BLOCKED", "s7")


def test_off_allowlist_navigation_is_blocked(recorded):
    art = fresh(recorded)
    art.steps.append(A.Step(id="s99", intent="Leave the site", action="navigate", value="https://example.org/"))
    r = replay(art)
    assert (r.status, r.code) == ("failed", "POLICY_BLOCKED")


def test_risky_step_needs_approval(recorded):
    art = fresh(recorded)
    transfer = A.Target(description='button "Transfer Funds"',
                        locators=[A.Locator(by="role", role="button", value="Transfer Funds")])
    art.steps.append(A.Step(id="s8", intent="Transfer", action="click", target=transfer, risk="risky",
                            expect=A.Condition(text_visible="Transfer Complete!")))
    art.success = []
    assert replay(art).status == "needs_confirmation"
    art.capability.status = "approved"
    assert replay(art).status == "success"


def test_bad_input_rejected_before_browser(recorded):
    from cua.replay import InputError, validate_params
    with pytest.raises(InputError, match="account_id' must be a number"):
        validate_params(recorded, {**PARAMS, "account_id": "abc"})


def test_hover_menu(server):
    """A hover-only menu: grounded from a screen point, opened by a recorded hover, then clicked."""
    from cua.surface import WebSurface

    async def go():
        s = WebSurface(["127.0.0.1"], headless=True)
        await s.open(server + "menu.html")
        try:
            box = await s.page.get_by_text("Reports", exact=True).bounding_box()
            menu, _ = await s.ground(int((box["x"] + 5) / 1280 * 1000), int((box["y"] + 5) / 800 * 1000), "act")
            await s.perform("hover", (await s.resolve(menu, {}, 2))[0])
            link = A.Target(description="link", locators=[A.Locator(by="role", role="link", value="Monthly statement")])
            await s.perform("click", (await s.resolve(link, {}, 2))[0])
            return await s.check(A.Condition(text_visible="Transfer Complete!"), {}, 3)
        finally:
            await s.close()

    assert asyncio.run(go())


RISKY = {"decision": "require_confirmation", "explanation": "Submits a form"}


def record_with_operator(server, script, button):
    """Record with a fake model; when the bar asks, the operator clicks `button`."""
    rec = agent.Recorder(server, GOAL, "fake", human=True, headless=True, secrets={"pw": SECRET}, name="gated")
    fake = FakeModel(rec, script)
    agent.genai.Client = lambda: fake

    async def operator():
        await until(lambda: rec.ctl.state == "awaiting_human")
        # like a real click: fire and forget (the recorder may close the browser right after a "deny")
        await rec.surface.page.evaluate(f"() => {{ setTimeout(() => window.__cua_ui('{button}', {{}}), 0); }}")

    async def both():
        art, _ = await asyncio.gather(rec.run(), operator())
        return art

    return asyncio.run(both()), rec, fake


def test_model_safety_decision_approved(server):
    script = [act("click", "input[type=submit]", intent="Log in", safety_decision=RISKY),
              act("task_complete", "h2", summary="done", capability_name="gated")]
    art, _, fake = record_with_operator(server, script, "approve")
    ack = fake.inputs[1][0]["result"]  # what we sent back for the approved click
    assert art and isinstance(ack, dict) and ack["safety_acknowledgement"] is True  # the API rejects any other shape


def test_model_safety_decision_denied_stops_cleanly(server):
    script = [act("click", "input[type=submit]", intent="Log in", safety_decision=RISKY)]
    art, rec, fake = record_with_operator(server, script, "deny")
    assert art is None and "did not approve" in rec.stop_reason and len(fake.inputs) == 1  # never called again


# ---- the AI decides; its decisions are applied as given ------------------------------------------
def test_ai_plan_and_review_are_applied_as_given(server, monkeypatch):
    odd = assist.Review(checkpoints=[assist.Check(step="s4", url_contains="", text="Invented text")],
                        success_url_contains="", success_text="Balance:",
                        risky=[assist.Risky(step="s5", reason="opens the account")])

    async def fake_review(*a, **k):
        return odd
    monkeypatch.setattr(assist, "review", fake_review)
    rec = agent.Recorder(server, GOAL, "fake", human=False, headless=True, secrets={"pw": SECRET}, name="as-given")
    agent.genai.Client = lambda: FakeModel(rec)
    art = asyncio.run(rec.run())
    s = {st.id: st for st in art.steps}
    assert s["s4"].expect.text_visible == "Invented text"  # not second-guessed, even though it never appeared
    assert (s["s5"].risk, s["s5"].risk_reason) == ("risky", "AI review: opens the account")
    assert art.success[0].text_visible == "Balance:" and art.provenance.history[-1].change.startswith("AI review:")

    rec.apply_plan(assist.Plan(inputs=[assist.InputPlan(name="Amount Due", example="999", type="number", description="x")]))
    assert rec.inputs["amount_due"].type == "number"  # applied as given, although 999 is not in the goal


def test_ai_decides_an_unknown_popup_and_it_is_remembered(recorded, monkeypatch):
    art, holder = A.load(str(fresh(recorded).save())), {}
    decides(monkeypatch, "dismiss", "SYSTEM_NOTICE", "System notice", at="#cua-fault-modal button", holder=holder)
    r = replay(art, holder=holder, inject=["modal@s5"], ai=True)
    assert r.status == "success" and r.ai_decisions and any("AI closed" in x for x in r.recoveries)
    learned = A.load(r.learned)
    o = learned.outcomes[-1]
    assert (o.source, o.recover.action, o.recover.target.locators[0].value) == ("ai", "dismiss", "OK")
    monkeypatch.setattr(assist, "decide", None)
    r2 = replay(learned, inject=["modal@s5"])
    assert r2.status == "success" and r2.ai_decisions == [] and any("dismissed" in x for x in r2.recoveries)


def test_ai_decides_a_business_outcome(recorded, monkeypatch):
    decides(monkeypatch, "business_outcome", "account closed", "System notice")
    r = replay(A.load(str(fresh(recorded).save())), inject=["modal@s5"], ai=True)
    assert (r.status, r.code) == ("business_outcome", "ACCOUNT_CLOSED") and A.load(r.learned).outcomes[-1].source == "ai"


def test_ai_repair_is_written_back_only_if_the_run_completes(recorded, monkeypatch):
    def broken():
        art = fresh(recorded)
        art.steps[1].target.locators = [A.Locator(by="css", value="#renamed-username")]  # every recorded rung fails
        return art

    def pointing_at(selector):
        async def fake_locate(*a, **k):
            box = await holder["rp"].surface.page.locator(selector).bounding_box()
            return assist.Point(found=True, x=int((box["x"] + box["width"] / 2) / 1280 * 1000),
                                y=int((box["y"] + box["height"] / 2) / 800 * 1000))
        return fake_locate

    holder = {}
    assert replay(broken(), holder=holder).code == "UNKNOWN_STATE"  # without --ai

    monkeypatch.setattr(assist, "locate", pointing_at("input[name=p]"))  # the AI picks the wrong field...
    r = replay(broken(), holder=holder, ai=True)
    assert r.status == "failed" and r.learned is None  # ...login fails at its checkpoint, so nothing is written back

    monkeypatch.setattr(assist, "locate", pointing_at("input[name=u]"))
    r = replay(broken(), holder=holder, ai=True)
    assert r.status == "success" and any("AI re-found" in f for f in r.locator_fallbacks)
    saved = A.load(r.learned)
    assert saved.capability.status == "draft" and saved.steps[1].target.locators[0].value == "Username:"


def test_a_restart_never_repeats_a_risky_step(recorded):
    """Guarantee for everyone: once a risky step ran, a learned "start over" must not run it again."""
    art = fresh(recorded)
    art.capability.status = "approved"
    transfer = A.Target(description='button "Transfer Funds"', locators=[A.Locator(by="role", role="button", value="Transfer Funds")])
    art.steps.append(A.Step(id="s8", intent="Transfer", action="click", target=transfer, risk="risky"))
    art.steps.append(A.Step(id="s9", intent="Read receipt", action="click",
                            target=A.Target(description="receipt", locators=[A.Locator(by="css", value="#receipt")])))
    art.outcomes.append(A.Outcome(id="session_expired", kind="recoverable", when=A.Condition(text_visible="Transfer Complete!"),
                                  recover=A.Recover(action="restart")))
    r = replay(art)
    assert (r.status, r.code, r.failed_step) == ("failed", "session_expired", "s9") and "risky step already ran" in r.message


# ---- units ----------------------------------------------------------------------------------------
def test_parse_goal_only_marks_secrets():
    assert agent.parse_goal(GOAL) == ("Log in as jdoe with {{secret:pw}} and read the balance of account 11111", ["pw"])


def test_redaction():
    assert redact("acct 12345678 ssn 123-45-6789 mail a@b.com bal $12345.67", ["pw1"]) == \
        "acct ••••5678 ssn •••-••-•••• mail •••@••• bal $12345.67"
    assert redact("token pw1 here", ["pw1"]) == "token [secret] here"
    assert redact("Timeout 30000ms") == "Timeout 30000ms"


def test_allowlist():
    assert default_domain("https://www.saucedemo.com/inventory.html") == "saucedemo.com"
    assert domain_allowed("https://parabank.parasoft.com/x", ["parabank.parasoft.com"])
    assert not domain_allowed("https://evil.com/?parabank.parasoft.com", ["parabank.parasoft.com"])
    assert not domain_allowed("https://parasoft.com.evil.com/", ["parasoft.com"])
