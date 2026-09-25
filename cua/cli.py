"""cua: record a task once with Gemini, replay it deterministically forever.

  cua record URL "GOAL"          learn a new capability from a plain-English goal (mark secrets as {name:secret})
  cua run NAME key=value ...     replay it (no model; --ai lets the AI handle the unexpected), one-line result
  cua list                       all capabilities with version, status and last result
  cua show NAME                  a plain-English card: what it does, needs, returns, steps, known outcomes
  cua approve NAME               draft -> approved (lets risky steps run unattended)
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import traceback
from pathlib import Path

from . import artifact as A
from .evidence import last_result


def load_dotenv(path: Path = Path(".env")) -> None:
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.strip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def secret(name: str) -> str:
    """Secrets come from the environment (.env) or a hidden prompt. They are never written anywhere."""
    value = os.environ.get(name.upper())
    if value:
        return value
    if sys.stdin.isatty():
        return getpass.getpass(f"{name} (hidden, not saved): ")
    sys.exit(f"Missing secret '{name}': set {name.upper()} in .env or the environment.")


# ---- commands --------------------------------------------------------------------------------------
def cmd_record(a) -> int:
    from .agent import MODEL, Recorder, parse_goal

    secret_names = parse_goal(a.goal)[1]
    secrets = {n: secret(n) for n in secret_names}
    rec = Recorder(a.url, a.goal, a.model or MODEL, human=not a.headless, headless=a.headless,
                   secrets=secrets, name=a.name, allow=a.allow)
    print(f"Recording on {a.url} (allowed: {', '.join(rec.domains)}). The browser shows who is in control.")
    try:
        art = asyncio.run(rec.run(a.max_steps))
    except Exception as e:  # e.g. the model API refused a request: one clear line, full trace in the run log
        rec.log.log("crash", error=repr(e), trace=traceback.format_exc())
        print(f"✖ Recording stopped by an error: {str(e).splitlines()[0][:300]}\n  details: {rec.log.dir}")
        return 1
    if not art:
        print(f"✖ Recording stopped: {rec.stop_reason}. Nothing was saved.\n  details: {rec.log.dir}")
        return 1
    print(f"✔ Learned '{art.name}' v{art.capability.version} ({len(art.steps)} steps) -> {A.ARTIFACTS / art.name}")
    print("  AI read the goal: " + (", ".join(f"{k} ({v.type})" for k, v in art.inputs.items()) or "no inputs"))
    for note in rec.review_notes:
        print(f"  AI review: {note}")
    print(f"  Try it:  cua run {art.name} " + " ".join(f"{k}=…" for k, v in art.inputs.items() if v.type != "secret"))
    print(f"  Review:  cua show {art.name}")
    return 0


def cmd_run(a) -> int:
    from .replay import InputError, Replayer, validate_params

    art = A.load(a.name, a.version)
    params = dict(p.split("=", 1) for p in a.params if "=" in p)
    for k, spec in art.inputs.items():
        if spec.type == "secret" and k not in params:
            params[k] = secret(k)
    try:
        validate_params(art, params)
    except InputError as e:
        print(f"✖ Bad input: {e}")
        print("  Needs: " + ", ".join(f"{k} ({v.type})" for k, v in art.inputs.items()))
        return 1
    # a human can only take over a visible browser
    r = asyncio.run(Replayer(art, params, headless=a.headless, human=not a.headless, inject=a.inject, ai=a.ai).run())
    if a.json:
        print(r.model_dump_json(indent=2))
    else:
        print(summary(art, r, {k: v for k, v in params.items() if art.inputs[k].type != "secret"}))
    return {"success": 0, "business_outcome": 0, "needs_confirmation": 2}.get(r.status, 1)


def summary(art: A.Artifact, r, params: dict) -> str:
    steps = {s.id: s for s in art.steps}
    if r.status == "success":
        outs = ", ".join(f"{k}: {v}" for k, v in r.outputs.items()) or "done"
        line = f"✔ Success: {outs} ({len(art.steps)} steps, {r.duration_s}s)"
    elif r.status == "business_outcome":
        line = f"● {r.code}: {r.message}"
    elif r.status == "needs_confirmation":
        line = f"⏸ Needs confirmation: {r.message}"
    else:
        where = f' at step {r.failed_step} "{A.fill(steps[r.failed_step].intent, params)}"' if r.failed_step in steps else ""
        line = f"✖ Failed{where}: {r.code}. {r.message}"
        if r.expected:
            line += f"\n  expected: {r.expected}"
        if r.observed:
            line += f"\n  saw:      {r.observed.get('heading') or ''} {r.observed.get('url', '')}".rstrip()
    for label, items in (("handled", r.recoveries), ("fallback locators", r.locator_fallbacks),
                         ("AI decided", r.ai_decisions), ("human did", r.human_actions)):
        if items:
            line += f"\n  {label}: " + "; ".join(items)
    if r.learned:
        line += f"\n  learned: {r.learned}"
    return line + f"\n  details: {r.evidence.get('run_dir')}"


def cmd_list(a) -> int:
    names = A.all_names()
    if not names:
        print('No capabilities yet. Record one:  cua record URL "goal"')
        return 0
    print(f"{'NAME':40} {'VER':>4}  {'STATUS':9} LAST RUN")
    for n in names:
        art = A.load(n)
        last = last_result(n)
        lr = f"{last['status']}{' ' + last['code'] if last.get('code') else ''}" if last else "never run"
        print(f"{n:40} {'v' + str(art.capability.version):>4}  {art.capability.status:9} {lr}")
    return 0


def cmd_show(a) -> int:
    art = A.load(a.name, a.version)
    c = art.capability
    print(f"{c.id}  v{c.version} · {c.status} · {c.surface} · allowed: {', '.join(art.policy.allowed_domains)}\n")
    print(f"What it does  {c.description}")
    print("Needs         " + (" · ".join(f"{k} ({v.type})" for k, v in art.inputs.items()) or "nothing"))
    print("Returns       " + (" · ".join(f"{k} ({v.type}{', sensitive' if v.sensitive else ''})" for k, v in art.outputs.items()) or "nothing"))
    print("Succeeds when " + " and ".join(_cond(x) for x in art.success))
    print("\nSteps")
    for n, s in enumerate(art.steps, 1):
        risky = f"⚠ risky ({s.risk_reason})" if s.risk_reason else "⚠ risky"
        flags = " ".join(f for f in (risky if s.risk == "risky" else "", "[human]" if s.actor == "human" else "") if f)
        print(f"  {n:>2}. {s.intent} {flags}".rstrip())
    print("\nKnown outcomes")
    for o in art.outcomes:
        what = {"business_outcome": f"normal answer {o.code}", "failure": f"error {o.code}"}.get(
            o.kind, f"recover: {o.recover.action if o.recover else '?'}")
        print(f"  • {_cond(o.when)} → {what}  (from {o.source})")
    print("  • always (code): off-allowlist → stop · server error → retry · record not found → NOT_FOUND"
          "\n  • anything new: a person decides (or the AI, with --ai), and it is remembered here")
    print("\nHistory")
    for h in art.provenance.history:
        print(f"  v{h.version}  {h.at[:16].replace('T', ' ')} UTC  {h.change}  ({h.by})")
    print(f"\nFile: {A.ARTIFACTS / c.id / f'v{c.version}.yaml'}")
    return 0


def _cond(c: A.Condition) -> str:
    parts = [f'"{c.text_visible}" is shown' if c.text_visible else "", f"URL has {c.url_contains}" if c.url_contains else ""]
    return " and ".join(p for p in parts if p) or "always"


def cmd_approve(a) -> int:
    art = A.load(a.name)
    art.capability.status = "approved"
    art.provenance.history.append(A.Change(version=art.capability.version, change="approved for unattended runs",
                                           by=a.by or getpass.getuser(), at=A.now()))
    art.save()
    print(f"✔ {art.name} v{art.capability.version} approved. Risky steps will now run without asking.")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="cua", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="learn a new capability with Gemini computer use")
    r.add_argument("url")
    r.add_argument("goal", help='plain English; mark only secrets, e.g. "Log in as john with {password:secret} and open account 13344"')
    r.add_argument("--name", help="capability name (default: suggested by the model)")
    r.add_argument("--allow", action="append", default=[], metavar="DOMAIN",
                   help="also allow this site, e.g. a login provider (repeatable)")
    r.add_argument("--model", help="Gemini model (default: gemini-3.8-flash)")
    r.add_argument("--max-steps", type=int, default=40)
    r.add_argument("--headless", action="store_true", help="no visible browser, so nobody is asked; fail instead of waiting")

    u = sub.add_parser("run", help="replay a capability deterministically (no model)")
    u.add_argument("name")
    u.add_argument("params", nargs="*", help="key=value inputs")
    u.add_argument("--version", type=int, help="default: latest")
    u.add_argument("--inject", action="append", default=[], metavar="FAULT@STEP",
                   help="demo/test a failure: slow, http500, expire_session, modal (e.g. modal@s4)")
    u.add_argument("--ai", action="store_true",
                   help="let the AI handle the unexpected (a new screen, a moved element); remembered for next time")
    u.add_argument("--headless", action="store_true", help="no visible browser, so nobody is asked; fail instead of waiting")
    u.add_argument("--json", action="store_true", help="print the full result as JSON (for agents)")

    sub.add_parser("list", help="list capabilities")
    s = sub.add_parser("show", help="explain a capability in plain English")
    s.add_argument("name")
    s.add_argument("--version", type=int)
    ap = sub.add_parser("approve", help="approve a capability for unattended runs")
    ap.add_argument("name")
    ap.add_argument("--by", help="reviewer name (default: your OS user)")

    a = p.parse_args(argv)
    try:
        return {"record": cmd_record, "run": cmd_run, "list": cmd_list, "show": cmd_show, "approve": cmd_approve}[a.cmd](a)
    except FileNotFoundError as e:
        print(f"✖ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
