"""The seam between "how we perceive/act on a surface" and "the recorded flow".

The recorder and replayer only talk to `Surface`. `WebSurface` implements it with Playwright.
A DesktopSurface would implement the same methods on an OS accessibility tree (UIA/AX) + OS input,
and the artifact schema would not change: targets are still ladders of role/name/label/near_text rungs.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Awaitable, Callable, Protocol
from urllib.parse import urlsplit

from playwright.async_api import Error as PWError
from playwright.async_api import Frame, Page, async_playwright

from .artifact import Condition, Locator, Target, fill
from .policy import domain_allowed

JS = Path(__file__).with_name("grounding.js").read_text()
W, H = 1280, 800  # viewport; the model's 0-999 grid maps onto it


class Surface(Protocol):
    async def open(self, url: str) -> None: ...
    async def screenshot(self, evidence: bool = False) -> bytes: ...
    async def ground(self, x: int, y: int, mode: str) -> tuple[Target, dict] | None: ...
    async def focused(self) -> tuple[Target, dict] | None: ...
    async def resolve(self, target: Target, params: dict, timeout: float) -> tuple[object, int] | None: ...
    async def perform(self, action: str, handle: object, value: str | None) -> str | None: ...
    async def check(self, cond: Condition, params: dict, timeout: float = 0) -> bool: ...
    async def texts(self) -> list[str]: ...
    async def close(self) -> None: ...


def canonical(url: str) -> str:
    """Stable URL form: no session ids in the path (legacy Java apps), no fragment."""
    return re.sub(r";jsessionid=[^?#/]*", "", url, flags=re.I).split("#")[0]


def path_query(url: str) -> str:
    p = urlsplit(canonical(url))
    return p.path + (f"?{p.query}" if p.query else "")


def near_xpath(anchor: str, tag: str) -> str:
    q = f"'{anchor}'" if "'" not in anchor else f'"{anchor}"'
    return f"//*[text()[normalize-space()={q}]]/following::{tag}[1]"


def build(scope, loc: Locator, params: dict):
    """Locator rung -> Playwright locator, within a page, frame or frame_locator."""
    v = fill(loc.value, params)
    match loc.by:
        case "row": return build(scope.locator(loc.tag).filter(has_text=v), loc.inner, params)
        case "role": return scope.get_by_role(loc.role, name=v, exact=True)
        case "label": return scope.get_by_label(v, exact=True)
        case "placeholder": return scope.get_by_placeholder(v, exact=True)
        case "text": return scope.get_by_text(v, exact=True)
        case "near_text": return scope.locator("xpath=" + near_xpath(v, loc.tag))
        case "css": return scope.locator(v)
    raise ValueError(f"not a DOM locator: {loc.by}")


class WebSurface:
    def __init__(self, allowed_domains: list[str], headless: bool = False):
        self.allowed = allowed_domains
        self.headless = headless
        self.blocked: list[str] = []  # navigations refused by the allowlist
        self.dialogs: list[str] = []  # JS dialogs seen (dismissed: never auto-confirm)
        self.doc_status = 200  # last main-document HTTP status
        self.faults = {"delay": 0.0, "fail_next_doc": False}  # fault injection for replay demos/tests
        self.on_ui: Callable[[str, dict], Awaitable] | None = None  # control-bar clicks -> Controller
        self.on_human: Callable[..., Awaitable] | None = None  # human page actions -> recorder/replayer
        self.bar: dict | None = None
        self.examples: dict[str, str] = {}  # recording: input example values, to find "the row for {{input}}"
        self._seen: set[int] = set()

    # ---- lifecycle -------------------------------------------------------------------------
    async def open(self, url: str) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        self.ctx = await self._browser.new_context(viewport={"width": W, "height": H})
        await self.ctx.add_init_script(JS)
        await self.ctx.expose_binding("__cua_ui", self._ui)
        await self.ctx.expose_binding("__cua_event", self._event)
        await self.ctx.route("**/*", self._route)
        self.ctx.on("page", self._adopt)  # popups/new tabs become the active page
        self._adopt(await self.ctx.new_page())
        await self.goto(url)

    async def close(self) -> None:
        await self._browser.close()
        await self._pw.stop()

    def _adopt(self, page: Page) -> None:
        if id(page) in self._seen:
            return
        self._seen.add(id(page))
        self.page = page
        page.on("dialog", self._dialog)
        page.on("response", self._response)
        page.on("domcontentloaded", lambda _: asyncio.ensure_future(self.render()))

    async def _route(self, route) -> None:
        req = route.request
        nav = req.is_navigation_request()
        if nav and not domain_allowed(req.url, self.allowed):
            self.blocked.append(req.url)
            return await route.abort("blockedbyclient")
        if self.faults["delay"]:
            await asyncio.sleep(self.faults["delay"])
        if nav and self.faults["fail_next_doc"] and req.frame == self.page.main_frame:
            self.faults["fail_next_doc"] = False
            return await route.fulfill(status=503, content_type="text/html", body="<h1>503 Service Unavailable</h1>")
        await route.continue_()

    def _response(self, resp) -> None:
        if resp.request.is_navigation_request() and resp.frame == self.page.main_frame:
            self.doc_status = resp.status

    async def _dialog(self, dialog) -> None:
        self.dialogs.append(dialog.message)
        await dialog.dismiss()

    async def _ui(self, source, action: str, data: dict) -> None:
        if self.on_ui:
            await self.on_ui(action, data)

    async def _event(self, source, ev: dict) -> None:
        """A human clicked/typed in the page. The ladder comes from the same describe() as model actions,
        but is not identity-verified (the page may already be navigating); replay still requires uniqueness."""
        if not self.on_human:
            return
        info = ev["info"]
        frames = await self._frame_path(source["frame"])
        label = info["name"] or info["anchor"] or info["text"][:40]
        target = Target(description=f'{info["role"] or info["tag"]} "{label}"', frames=frames,
                        locators=[Locator(**c) for c in info["candidates"]])
        await self.on_human(ev["kind"], target, info, ev.get("value"))

    async def render(self, bar: dict | None = None) -> None:
        """Show the control bar (who is in control, and the operator's buttons)."""
        self.bar = bar or self.bar
        if self.bar:
            try:
                await self.page.evaluate("s => window.__cua && window.__cua.render && window.__cua.render(s)", self.bar)
            except PWError:
                pass  # page navigating; re-rendered on domcontentloaded

    # ---- perceive ----------------------------------------------------------------------------
    async def screenshot(self, evidence: bool = False) -> bytes:
        """Model screenshots hide the control bar. Evidence screenshots keep it and mask password fields."""
        if evidence:
            return await self.page.screenshot(mask=[self.page.locator("input[type=password]")])
        return await self.page.screenshot(style="#__cua_bar{display:none!important}")

    async def texts(self) -> list[str]:
        """Short visible texts across all frames (checkpoint candidates for the AI review)."""
        out: list[str] = []
        for f in self.page.frames:
            try:
                out += [t for t in await f.evaluate("() => window.__cua ? window.__cua.texts() : []") if t not in out]
            except PWError:
                pass
        return out

    async def observe(self) -> dict:
        """What is on screen now, for failure reports: url, heading, a text excerpt, and the a11y snapshot."""
        heading = ""
        for f in self.page.frames:
            try:
                heading = heading or await f.evaluate("() => window.__cua ? window.__cua.heading() : ''")
            except PWError:
                pass
        try:
            text = re.sub(r"\s+", " ", await self.page.inner_text("body"))[:400]
            aria = await self.page.locator("body").aria_snapshot()
        except PWError:
            text, aria = "", ""
        return {"url": canonical(self.page.url), "heading": heading, "text": text, "dialogs": self.dialogs[-3:],
                "http_status": self.doc_status, "aria": aria}

    async def text_visible(self, text: str) -> bool:
        for f in self.page.frames:
            try:
                if await f.get_by_text(text).filter(visible=True).count():
                    return True
            except PWError:
                pass
        return any(text.lower() in d.lower() for d in self.dialogs)

    async def check(self, cond: Condition, params: dict, timeout: float = 0) -> bool:
        """Poll until the condition holds or timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            ok = True
            if cond.url_contains and fill(cond.url_contains, params) not in canonical(self.page.url):
                ok = False
            if ok and cond.text_visible and not await self.text_visible(fill(cond.text_visible, params)):
                ok = False
            # An error page or a blocked navigation will not turn into the expected page: stop waiting.
            if ok or loop.time() >= deadline or self.doc_status >= 500 or self.blocked:
                return ok
            await asyncio.sleep(0.25)

    # ---- grounding: pixels -> verified locator ladder ---------------------------------------
    async def ground(self, x: int, y: int, mode: str = "act") -> tuple[Target, dict] | None:
        px, py = x / 1000 * W, y / 1000 * H
        return await self._descend("([x, y, m]) => window.__cua.hit(x, y, m)", (px, py), mode, point=(x / 1000, y / 1000))

    async def focused(self) -> tuple[Target, dict] | None:
        return await self._descend("() => window.__cua.focused()", None, "act")

    async def _descend(self, js: str, xy, mode: str, point=None):
        frame, frames, off = self.page.main_frame, [], (0, 0)
        for _ in range(6):  # nested frames / framesets
            arg = [xy[0] - off[0], xy[1] - off[1], mode] if xy else None
            el = (await frame.evaluate_handle(js, arg)).as_element()
            if not el:
                break
            if await el.evaluate("e => e.tagName === 'IFRAME' || e.tagName === 'FRAME'"):
                info = await el.evaluate("e => window.__cua.describe(e, 'act')")
                frames.append(info["frame_selector"])
                box = await el.bounding_box()  # relative to the main viewport, even when nested
                off = (box["x"], box["y"]) if box else (0, 0)
                frame = await el.content_frame()
                continue
            return await self._target_for(el, frame, frames, mode, point)
        if point and mode == "act":  # canvas / no DOM under the point: last-resort coordinates
            return Target(description=f"point {point[0]:.2f},{point[1]:.2f}",
                          locators=[Locator(by="point", x=point[0], y=point[1])]), {"role": None, "name": "", "password": False}
        return None

    async def _target_for(self, el, frame: Frame, frames: list[str], mode: str, point=None):
        info = await el.evaluate("(e, m) => window.__cua.describe(e, m)", mode)

        async def verified(loc: Locator) -> bool:  # unique AND the same element
            try:
                pw = build(frame, loc, {})
                return await pw.count() == 1 and await pw.evaluate("(e, t) => e === t", el)
            except PWError:
                return False

        ladder = [loc for c in info["candidates"] if await verified(loc := Locator(**c))]
        if not any(ex in (loc.value or "") for loc in ladder for ex in self.examples.values()):
            if row := await self._row_rung(info, verified):
                ladder.insert(0, row)
        if not ladder and point:
            ladder.append(Locator(by="point", x=point[0], y=point[1]))
        if not ladder:
            return None
        if mode == "read" and info["anchor"]:
            return Target(description=f'value after "{info["anchor"]}"', frames=frames, locators=ladder), info
        label = info["name"] or info["anchor"] or info["text"][:40]
        where = f' in the row for "{ladder[0].value}"' if ladder[0].by == "row" else ""
        return Target(description=f'{info["role"] or info["tag"]} "{label}"{where}', frames=frames, locators=ladder), info

    async def _row_rung(self, info: dict, verified) -> Locator | None:
        """The element's own text doesn't mention the input, but its row does ("Add to cart" for {item}):
        target it as <inner> within the innermost container that contains the input value."""
        for ex in (v for v in self.examples.values() if len(v) >= 3):
            for row in info["rows"]:
                if ex not in row["text"]:
                    continue
                for c in info["candidates"]:
                    rung = Locator(by="row", tag=row["sel"], value=ex, inner=Locator(**c))
                    if await verified(rung):
                        return rung
        return None

    async def _frame_path(self, frame: Frame) -> list[str]:
        path = []
        while frame.parent_frame:
            el = await frame.frame_element()
            path.insert(0, (await el.evaluate("e => window.__cua.describe(e, 'act')"))["frame_selector"])
            frame = frame.parent_frame
        return path

    # ---- act ---------------------------------------------------------------------------------
    async def resolve(self, target: Target, params: dict, timeout: float = 10.0):
        """Walk the ladder until a rung matches exactly one element. Returns (handle, rung index) or None."""
        scope = self.page
        for sel in target.frames:
            scope = scope.frame_locator(sel)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for i, loc in enumerate(target.locators):
                if loc.by == "point":
                    return loc, i
                try:
                    pw = build(scope, loc, params)
                    if await pw.count() == 1:
                        return pw, i
                except PWError:
                    pass
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.25)

    async def goto(self, url: str) -> None:
        try:
            await self.page.goto(url, wait_until="load", timeout=30000)
        except PWError:
            if domain_allowed(url, self.allowed):
                raise  # a real load failure; refused-by-allowlist is reported via self.blocked

    async def perform(self, action: str, handle=None, value: str | None = None) -> str | None:
        if action == "navigate":
            await self.goto(value)
        elif action == "go_back":
            await self.page.go_back()
        elif isinstance(handle, Locator):  # point rung
            x, y = handle.x * W, handle.y * H
            await {"hover": self.page.mouse.move, "double_click": self.page.mouse.dblclick}.get(action, self.page.mouse.click)(x, y)
            if action == "type":
                await self.page.keyboard.type(value)
        elif action == "click":
            await handle.click(timeout=5000)
        elif action == "double_click":
            await handle.dblclick(timeout=5000)
        elif action == "hover":
            await handle.hover(timeout=5000)
        elif action == "type":
            await handle.fill(value, timeout=5000)
        elif action == "select":
            await handle.select_option(label=value, timeout=5000)
        elif action == "press_key":
            await (handle.press(value) if handle else self.page.keyboard.press(value))
        elif action == "extract":
            for _ in range(20):  # values often arrive after load (AJAX); wait until non-empty
                text = (await handle.inner_text()).strip()
                if text:
                    return text
                await asyncio.sleep(0.25)
            return ""
        if action not in ("type", "select", "hover"):  # these never navigate; the next locator or check waits anyway
            await self.settle()
        return None

    async def settle(self, extra: float = 0.3) -> None:
        await asyncio.sleep(0.2)
        try:
            await self.page.wait_for_load_state("load", timeout=10000)
        except PWError:
            pass
        await asyncio.sleep(extra)
