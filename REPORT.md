# Design report

**Principle: the model discovers once, the file remembers, replay is plain code.** Gemini works out a task on the live UI
and makes the judgment calls a recording needs. The result is a small, typed, versioned YAML capability. `cua run`
replays it with **no model**. It works on any web app: `evidence/` shows a bank (ParaBank) and an unrelated shop
(SauceDemo), each learned with one command. The system is about 2,300 lines of Python and JS in a single process, with
four dependencies.

## 1. Architecture

```
record (once)   goal ─► assist: AI reads inputs ─► Gemini computer use ─► grounding: pixels → verified locators
                                                                         └► assist: AI review (checkpoints, risk)
                ─► artifacts/<name>/vN.yaml
run (always)    artifact + inputs ─► replay.py (code only) ─► Result
                     unexpected screen ─► a human (handoff), or with --ai the AI decides ─► remembered as vN+1
```

- **Screenshot perception, DOM execution.** The model only sees pixels (Gemini 3.8 Flash, `computer_use`), because the
  brief says not to assume a clean DOM. At the moment of each action, `grounding.js` hit-tests the point (through
  iframes and shadow DOM) and derives a **ladder of locators**. The action is executed through that locator, not the
  coordinate, so the recording only contains things that actually worked.
- **AI owns judgment, code owns execution and guarantees.** The AI decides which values in a plain-English goal are
  inputs, every action, each step's checkpoint, the success condition, and which steps are risky. Its answers are
  applied as given. Code runs the steps, checks the checkpoints, and keeps the guarantees: the allowlist, approval before
  risky steps, redaction, and attempt limits.
- **Replay is deterministic by default.** With `--ai`, an unexpected screen or a moved element is handled by the AI
  instead of a human. Either way, the decision is written into the next artifact version, so the next run is plain code again.
- **A CLI plus a control bar injected into the page, not a web app.** There is no server or database. Capabilities are
  files you can diff, review and commit.
- **Targets.** ParaBank (server-rendered legacy markup, `;jsessionid` URLs, id-less table forms, real 502s, a real
  transfer), SauceDemo (lists, per-row buttons, a multi-page checkout), and a local fixture site for offline tests.
- **Trade-off:** replay needs a DOM or, later, an accessibility tree. Pure-canvas surfaces fall back to a coordinate
  rung, which is reported as drift whenever it is used.

## 2. Artifact schema

One Pydantic-validated YAML file per version, `artifacts/<name>/v<N>.yaml`, reading top to bottom as *contract → policy → how → what can go wrong → proof → history*:

```yaml
capability: {id, version, status: draft|approved, description: "…open account {account_number}…", start_url, surface: web}
inputs:   {username: {type: string}, password: {type: secret}, account_number: {type: string}}   # chosen by the AI
outputs:  {balance: {type: money, sensitive: false}}         # the AI decides; sensitive → stored as [sensitive]
policy:   {allowed_domains: [parabank.parasoft.com], allowed_actions: [navigate, click, type, select, …]}
steps:
- id: s5
  intent: Click account {{account_number}} to open it          # the model's own reason
  action: click
  target: {description: 'link "{{account_number}}"', locators: [{by: role, role: link, value: "{{account_number}}"}, …]}
  risk: safe                                                   # risky → approval gate; risk_reason says who flagged it
  expect: {url_contains: /parabank/activity.htm, text_visible: Account Details}   # checkpoint
outcomes:                                                      # what a screen means: remembered AI or human decisions
- {id: system_notice, kind: recoverable, when: {text_visible: System notice}, recover: {action: dismiss, target: …}, source: ai}
success: [{url_contains: …, text_visible: Account Details}]    # plus every output must be extracted
provenance: {model, discovery_run, history: [{version, change, by, at}]}
```

- **A contract first.** Typed inputs and outputs tell a calling agent what to pass and what it gets back. Inputs are
  validated before a browser opens. Outputs are coerced (`$1,229.10` → `1229.1`).
- **Inputs come from the goal.** The AI reads "open account 13344" and decides `account_number` is an input. Code then
  turns every occurrence of `13344` (typed text, link text, URL) into `{{account_number}}`. Secrets are the only thing a
  user marks (`{password:secret}`). The model types the placeholder, and the value is substituted at the keyboard, so
  **the model never sees a secret**.
- **A robust locator ladder:** row → role+name → label → placeholder → text → `near_text` (the input after the text
  "Username", often the only stable handle in id-less legacy tables) → css → point. A rung is kept only if it matches
  **exactly one element, and the same one**, at record time. Targets that depend on an input keep only parameterized
  rungs, so a fallback can never pick another customer's record. That includes "Add to cart" *in the row for
  `{{product_name}}`*.
- **Linear steps plus an outcome catalogue** rather than a state graph: easy to read, and error knowledge grows in one list.
- **Versioned.** Any change writes a new version, resets it to `draft`, and appends to `history`.

## 3. Determinism & error handling

For each step, replay runs a fixed sequence: close known pop-ups → approval gate → resolve the ladder (up to 10 s, the
first rung matching exactly one element) → act → check `expect`. Waiting is on conditions. The only fixed pause is half
a second after an action that can navigate. Same inputs, same steps, no model.

**When a step misses, it is handled in this order:**

| # | Detected by | Response |
|---|-------------|----------|
| 1 | navigation outside the allowlist | hard failure `POLICY_BLOCKED` |
| 2 | a remembered `outcome` matches the screen | business outcome → return it · recoverable → dismiss / retry / start over · failure → stop |
| 3 | HTTP ≥ 500 | recoverable: back off 2 s, 4 s, reload |
| 4 | **an input's element is missing on a page that passed its checkpoint** | business outcome `NOT_FOUND` ("no match for account_number=99999") |
| 5 | anything else | escalate to a human (§5), or `failed UNKNOWN_STATE` with evidence. With `--ai`: the AI re-finds a moved element, or decides what the screen means and acts |

Row 4 is how "no such member" becomes a **result, not a crash**, with no per-site configuration. Whatever a human or the
AI decides in row 5 becomes a row 2 outcome in the next version. Recoveries are bounded (`RECOVERY_EXHAUSTED`).

**Result contract:**
- `status`: success | business_outcome | needs_confirmation | failed
- `code`, `message`, `outputs`
- on failure: `failed_step`, `expected`, `observed` (URL, heading, text, HTTP status)
- `recoveries`, `locator_fallbacks` (drift), `ai_decisions`, `human_actions`, `learned`
- `evidence`: a screenshot, the accessibility snapshot and the JSONL log

`evidence/` shows every class live, including a real 502 outage, and `NOT_FOUND` on both sites.

## 4. Heterogeneity & multi-tenant

**Surface seam.** `recorder/replayer ⇄ Surface` is the only boundary that knows about browsers:
`open, screenshot, ground(x,y), focused, resolve(target), perform(action), check(condition), texts, close`. The schema is
surface-neutral, because role, name, label and text exist in every accessibility API.
- **Legacy web** already works: grounding descends frames and framesets and records `target.frames`, and `near_text`
  covers label-less tables.
- **A `DesktopSurface`** would use Gemini's `environment: desktop`, hit-test through UI Automation or AX to build the same
  ladder (`AutomationId` is the "css" rung), and evaluate checks on the accessibility tree.
- **Citrix or canvas** fall back to OCR anchors plus the point rung, flagged as drift.

**Multi-tenant reuse.** There is a **base capability per vendor product and version** (`fis-corebank@2024.2/get-balance`),
recorded once on a reference tenant, plus a **per-tenant overlay** (a JSON merge-patch) holding only what differs:
`start_url`, `allowed_domains`, relabelled rungs ("Member #" vs "Member ID"), and extra outcomes. The effective artifact
is `base ⊕ overlay`, so fixes to the base reach every tenant. The ordered ladder absorbs many differences with no overlay at all.

**Drift.** Every result carries `locator_fallbacks` and classified misses. Aggregated per (capability, tenant, version),
a rising rate triggers one of three things: promote the working rung into the overlay, re-record that tenant, or cut a
new base. Outcomes seen on several tenants get promoted into the base. The per-run data exists; the plumbing is not built (§7).

## 5. Escalation & handoff

**Detecting "stuck":**
- **While recording:** the model calls `request_human` (a CAPTCHA, which we never solve), three turns pass with no
  visible change, the model asks a question instead of acting, or Gemini flags a risky action (`safety_decision`).
- **During replay:** an unknown screen, a risky step on a draft, or recoveries running out.

**Routing.** Each escalation writes `intervention.json` (capability or goal, step, reason, expected vs observed,
screenshot), rings the terminal and turns the in-page bar red. The file is the seam to a queue, Slack or a pager (mocked).

**Control transfer.** A `Controller` holds exactly one `owner ∈ {agent, human}`:
`agent/running → awaiting_human → (Take over) human/active → (Hand back / Label) agent/running`. Every automated
action first awaits `checkpoint()`, so **automation cannot act while a human holds the session**. The human works in
the **same browser context** (same cookies, same page). Their clicks, typing and choices are grounded by the same code
as model actions, and recorded with redaction. Recording resumes with a note to the model about what the human did.
Replay re-checks the interrupted step and continues.

**Escalations become knowledge.** "This screen means…" (a normal answer / a pop-up I closed / temporary / logged out /
an error) becomes an `outcome` in the next draft version, marked `source: human`. AI decisions under `--ai` are stored
the same way, marked `source: ai`. **Mocked:** remote access. The visible window stands in for a CDP-screencast
operator console, but the ownership model and hand-back are real (see `evidence/`).

## 6. Safety

- **Allowlist at the network layer:** every navigation passes through `page.route`, and anything outside
  `allowed_domains` is aborted (default: the start site and its subdomains; `--allow` adds more, e.g. SSO). Replay also
  refuses steps whose action is not in `allowed_actions`. Drag, right-click and raw mouse actions are removed from the
  model's toolset.
- **Risky steps.** Which steps are risky is the AI's judgment: Gemini's safety flag while acting, plus the review. What
  happens to a risky step is code:
  - it needs a human's Approve while recording
  - it runs unattended only once the capability is `approved` (`cua approve`, which records the reviewer; any change resets to draft)
  - on a draft, it returns `needs_confirmation` without acting
  - it is never retried or re-targeted, and a "start over" never runs once it has executed

  I gate on approval instead of blocking, because "reach the confirmation screen" is a legitimate task.
- **Secrets and personal data.**
  - Secrets live only in memory. The model and the artifacts see placeholders.
  - Every log, result and intervention passes a redactor (account numbers → last 4; SSN, email and phone masked).
  - Password fields are masked in screenshots, and `sensitive` outputs are stored as `[sensitive]`.
  - A test asserts the secret appears in no file.
- **Limits:**
  - Screenshots sent to Gemini contain whatever is on screen: production needs a zero-retention or in-VPC endpoint.
  - Evidence screenshots mask only password fields.
  - Risk has no keyword floor: if the AI misses a risky step, a draft runs it without approval. The mitigation is the review in `cua show` before `cua approve`.
  - Gemini's safety acknowledgement must be a plain-object result. The documented text+image shape returns 400 (verified live).
  - The allowlist governs where the tab navigates (pop-ups included), not a page's own requests or iframes (ads, video, payments).

## 7. Cuts

**Stretch goals chosen:**
- **approval gating** (draft → approved)
- **assisted fallback** (`--ai`: the AI handles an unexpected screen or re-finds a moved element, once per step, applied
  within the guarantees, listed in `ai_decisions` and saved as a draft version)

**Left out on purpose:**
- the tenant overlay resolver and drift dashboard (designed in §4)
- the desktop surface (seam only)
- the remote operator console and real routing (a JSON file and the bar instead)
- an MCP catalogue (`cua run --json` is the machine contract today)
- flakiness scoring
- screen-level PII masking before model calls

**Next:**
1. an MCP server over `cua list` / `cua run --json`
2. tenant overlays and drift aggregation, where the AI could propose overlays the way it re-finds elements
3. a CDP-screencast operator page with a lease on human ownership
4. `DesktopSurface` on UI Automation
