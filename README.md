# cua: teach a web app once, replay it forever

Many web apps have no API: legacy bank back-office screens, admin consoles, vendor portals, internal tools. Point `cua` at
**any web app URL** with a goal in plain English. Gemini **computer use** works out the task on the real UI once. `cua` saves
what it learned as a **reviewable, versioned capability file**, then **replays it deterministically**: fast, cheap, and the
same every time, with no model. When a replay meets something new, a person takes over the same live browser (or, with
`--ai`, the AI decides), and the capability **remembers** it, so next time plain code handles it.

```
cua record URL "goal"   →   artifacts/<name>/v1.yaml   →   cua run <name> inputs…   →   ✔ success / ● business outcome / ✖ failure
   (AI decides)              (typed, reviewable)             (plain code, no model)
```

Design write-up: [REPORT.md](REPORT.md) · Proof it works: [evidence/](evidence/README.md)

## Setup

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run playwright install chromium
cp .env.example .env        # then put your GEMINI_API_KEY in it
```

`.env` holds the Gemini key and any secrets your goals use. `.env.example` already contains the public demo passwords
(`PASSWORD` for the ParaBank demo customer `cua_demo_6955`, `SAUCE_PASSWORD` for SauceDemo). Secrets are never written to artifacts, logs or screenshots.

To try replay with no Gemini key, copy the capabilities recorded for the evidence: `cp -R evidence/artifacts artifacts`.

## Five commands

```bash
cua record URL "GOAL"          # learn a new capability (Gemini drives a visible browser)
cua run NAME key=value …       # replay it with no model, one-line result
cua list                       # every capability: version, status, last result
cua show NAME                  # plain-English card: what it does, needs, returns, its steps, what can go wrong
cua approve NAME               # draft → approved: lets risky steps (transfers, submits) run unattended
```

Run them with `uv run cua …`, or activate the venv. Write the goal in **plain English**. The AI decides which values in
it are inputs; `cua record` prints them, with their names. The only thing you mark is a secret, as `{name:secret}`,
because the model must never see its value. It is read from `NAME` in `.env`, or asked for once.

## Who decides what

| The AI decides | Code does (the same for everyone, AI or human) |
|---|---|
| **before recording:** which values in your goal are inputs | runs every recorded step through verified locators, and checks each checkpoint |
| **while recording:** every action, and which ones are risky (Gemini's safety flag) | handles what it already knows: remembered outcomes, server errors (retry), a missing record (`NOT_FOUND`) |
| **after recording:** each step's checkpoint, the success condition, more risky steps | enforces the guarantees: allowlist, approval before risky steps, never retrying or re-running a risky step, attempt limits |
| **during replay, only with `--ai` and only when something is unexpected:** what the screen means and what to do (close it, retry, start over, return an answer, fail), and where a moved element went | remembers every decision (AI or human) in a new draft version, so next time code handles it alone; keeps secrets and personal data out of files |

**Replay is plain code with no model.** Something unexpected goes to a person, or fails with evidence when nobody is
watching. `cua run --ai` lets the AI handle it instead. Code never second-guesses an AI decision: it applies it and keeps
the guarantees. People decide approvals, CAPTCHAs, and anything the AI reports it is unsure about.

## Demo 1: a bank app (live ParaBank, about 3 minutes)

ParaBank is Parasoft's public demo bank. We registered our own demo customer, `cua_demo_6955`
(accounts 31437 and 31770), because its shared user `john` was returning server errors. Any ParaBank user works.

```bash
# 1. Learn it. A browser opens; the bar at the bottom shows who is in control.
uv run cua record https://parabank.parasoft.com/parabank/index.htm \
  "Log in as cua_demo_6955 with {password:secret}, open account 31437 and read its balance" \
  --name parabank-account-balance
#    ✔ Learned 'parabank-account-balance' v1 (6 steps)
#      AI read the goal: password (secret), username (string), account_number (string)

# 2. Read it.
uv run cua show parabank-account-balance

# 3. Replay it with the input names printed above. Try other values.
uv run cua run parabank-account-balance username=cua_demo_6955 account_number=31437     # ✔ Success: balance: …
uv run cua run parabank-account-balance username=cua_demo_6955 account_number=99999     # ● NOT_FOUND (a normal answer, not a crash)
uv run cua run parabank-account-balance username=cua_demo_6955                   # ✖ Bad input: missing input 'account_number' (before any browser opens)

# 4. Break it on purpose. Something new: you decide (or the AI, with --ai). After that, code handles it.
uv run cua run parabank-account-balance username=cua_demo_6955 account_number=31437 --inject modal@s5            # asks YOU (see below)
uv run cua run parabank-account-balance username=cua_demo_6955 account_number=31437 --inject expire_session@s5 --ai   # AI: start over
uv run cua run parabank-account-balance username=cua_demo_6955 account_number=31437 --inject expire_session@s5   # code, no AI
uv run cua run parabank-account-balance username=cua_demo_6955 account_number=31437 --inject http500@s5          # code: retry
```

On the pop-up, the bar turns red (**⚠️ Needs you**). Click **Take over**, close the pop-up, then **This screen means… →
"A pop-up I closed" → Save**. The run finishes, and the next run closes that pop-up by itself.

**Risky actions** (transfer, pay, submit…) pause for **Approve / Deny** while recording. A **draft** capability returns
`needs_confirmation` instead of running them unattended. After `cua approve`, they run.

## Demo 2: any other web app (live SauceDemo shop)

Nothing is site-specific. The same command learns a shopping checkout:

```bash
uv run cua record https://www.saucedemo.com/ \
  "Log in as standard_user with {sauce_password:secret}, add the Sauce Labs Backpack to the cart, check out as Ada Lovelace with postal code 10001, reach the checkout overview page and read the item total" \
  --name saucedemo-checkout

uv run cua show saucedemo-checkout        # shows the input names the AI chose, e.g. product_name, first_name
uv run cua run saucedemo-checkout username=standard_user "product_name=Sauce Labs Bike Light" first_name=Grace last_name=Hopper postal_code=94016
# ✔ Success: item_total: 9.99      ("Add to cart" is recorded as "the button in the row for {product_name}")
```

**What works on any site:**
- plain HTML or single-page apps, frames and iframes, shadow DOM, pop-ups and new tabs
- dropdowns, hover menus and double-click
- lists and tables ("the Edit button in the row for {id}")
- logins, including SSO on another domain, via `--allow`

**Not automated; a human steps in instead:**
- CAPTCHAs and bot walls (never solved, by design)
- file uploads and drag-and-drop (not supported yet)

Apps drawn on a canvas fall back to screen coordinates, and every use of that fallback is flagged as fragile.

To regenerate everything in `evidence/`: `./scripts/make_evidence.sh`.

## Without live services

```bash
uv run pytest     # ~3 min, no network, no API key
```

The tests drive the **real** recorder and replayer against a local "legacy" site (`tests/site/`: table layouts, no ids,
a per-row button, late-loading values, cookie session, a dropdown). Scripted stand-ins play every AI part, so they cover:
- every outcome class: success, other inputs, NOT_FOUND, server error, slow load, allowlist and action blocks
- with `--ai`, the AI deciding and the artifact remembering: a pop-up closed, a logout restarted, a business outcome, a moved element
- decisions applied as given, repairs written back only when the run completes
- the guarantees: risky-step approval, and a restart never repeating a risky step
- a human takeover that teaches the artifact, and Gemini's safety approve/deny
- a hover menu, and a secret that never reaches disk

No test calls the real model.

## Options you rarely need

| Flag | On | Meaning |
|------|----|---------|
| `--name` | record | capability name (default: suggested by the model) |
| `--allow DOMAIN` | record | also allow another site, e.g. an SSO login domain (repeatable). The default allows only the start site and its subdomains |
| `--model` | record | Gemini model for recording (default `gemini-3.8-flash`) |
| `--max-steps` | record | model turn budget (default 40) |
| `--version N` | run, show | a specific version (default: latest) |
| `--ai` | run | let the AI handle the unexpected (a new screen, a moved element) instead of a person; remembered for next time |
| `--inject FAULT@STEP` | run | simulate `slow`, `http500`, `expire_session` or `modal` before a step (repeatable) |
| `--json` | run | print the full result contract (for agents), including every `ai_decisions` entry |
| `--headless` | record, run | no visible browser, so nobody is asked: anything needing a person fails with evidence instead |

Exit codes for `run`: `0` success or business outcome, `2` needs confirmation, `1` failure.

## What's where

```
cua/cli.py         the five commands
cua/agent.py       recording: the Gemini loop, the recorder (pixels → verified locators), applying the AI's plan and review
cua/replay.py      replay: plain code; unexpected screens go to a person, or to the AI with --ai
cua/assist.py      the AI's other decisions: goal inputs, review, unexpected screens, moved elements
cua/artifact.py    the capability schema (Pydantic) and versioned YAML storage
cua/surface.py     the perceive/act seam (WebSurface = Playwright)
cua/grounding.js   in-page: point → element → locator ladder; the control bar; human-action capture
cua/handoff.py     who is in control; escalation and hand-back
cua/policy.py      allowlist, redaction
cua/evidence.py    runs/<id>/ log, screenshots, result
artifacts/         capabilities: one YAML per version (commit these)
runs/              per-run evidence (git-ignored)
```
