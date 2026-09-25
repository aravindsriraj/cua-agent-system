"""The capability artifact: a typed, versioned, reviewable description of a recorded flow.

One YAML file per version: artifacts/<name>/v<N>.yaml. Files on disk = diffable, reviewable, git-friendly.
Field order is chosen so the file reads top to bottom: what it is -> contract -> policy -> how -> what can go wrong.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

ARTIFACTS = Path("artifacts")
SCHEMA_VERSION = 1


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Input(BaseModel):
    type: Literal["string", "number", "secret"]
    description: str = ""


class Output(BaseModel):
    type: Literal["string", "number", "money", "date"]
    description: str = ""
    sensitive: bool = False  # sensitive outputs are returned to the caller but masked in logs/results on disk


class Locator(BaseModel):
    """One rung of the ladder. Values may contain {{param}} placeholders."""
    by: Literal["row", "role", "label", "placeholder", "text", "near_text", "css", "point"]
    value: str | None = None  # by=row: text the row must contain, e.g. "{{item}}"
    role: str | None = None  # by=role
    tag: str | None = None  # by=near_text: the element tag after the anchor text; by=row: the row selector
    inner: Locator | None = None  # by=row: which element inside that row, e.g. button "Add to cart"
    x: float | None = None  # by=point: viewport fraction 0..1 (last resort, flagged as drift when used)
    y: float | None = None


class Target(BaseModel):
    description: str
    frames: list[str] = Field(default_factory=list)  # iframe/frame selectors from the top document down
    locators: list[Locator]  # ordered ladder; every rung was verified unique at record time


class Condition(BaseModel):
    """All given fields must hold."""
    url_contains: str | None = None
    text_visible: str | None = None


class Step(BaseModel):
    id: str
    intent: str  # human-readable why, from the model's own intent (or the human's action)
    action: Literal["navigate", "click", "double_click", "hover", "type", "select", "press_key", "go_back", "extract"]
    target: Target | None = None
    value: str | None = None  # text to type / option label to select / key / url; may contain {{param}}
    output: str | None = None  # action=extract: which output this fills
    risk: Literal["safe", "risky"] = "safe"
    risk_reason: str | None = None  # why it is risky: Gemini's safety flag while acting, or the AI review
    expect: Condition | None = None  # checkpoint after the step
    actor: Literal["agent", "human"] = "agent"


class Recover(BaseModel):
    action: Literal["retry", "restart", "dismiss"]
    target: Target | None = None  # action=dismiss: what to click to close it


class Outcome(BaseModel):
    id: str
    kind: Literal["business_outcome", "recoverable", "failure"]
    when: Condition
    code: str | None = None  # business_outcome/failure: what the caller receives
    message: str = ""
    recover: Recover | None = None  # recoverable only
    source: Literal["ai", "human"] = "human"


class Capability(BaseModel):
    id: str
    version: int
    status: Literal["draft", "approved"]
    description: str  # the goal template, e.g. "open account {account_id} and read its balance"
    start_url: str
    surface: Literal["web"]  # seam: "desktop" would plug in a different Surface, same schema


class Policy(BaseModel):
    allowed_domains: list[str]
    allowed_actions: list[str]


class Change(BaseModel):
    version: int
    change: str
    by: str
    at: str


class Provenance(BaseModel):
    recorded_at: str
    model: str
    discovery_run: str
    history: list[Change] = Field(default_factory=list)


class Artifact(BaseModel):
    schema_version: int
    capability: Capability
    inputs: dict[str, Input] = Field(default_factory=dict)
    outputs: dict[str, Output] = Field(default_factory=dict)
    policy: Policy
    steps: list[Step]
    outcomes: list[Outcome] = Field(default_factory=list)
    success: list[Condition] = Field(default_factory=list)  # all must hold at the end (plus every output present)
    provenance: Provenance

    @property
    def name(self) -> str:
        return self.capability.id

    def save(self) -> Path:
        path = ARTIFACTS / self.name / f"v{self.capability.version}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120))
        return path

    def bump(self, change: str, by: str) -> None:
        self.capability.version = latest_version(self.name) + 1
        self.capability.status = "draft"  # any change needs re-approval
        self.provenance.history.append(Change(version=self.capability.version, change=change, by=by, at=now()))


def versions(name: str) -> list[int]:
    return sorted(int(p.stem[1:]) for p in (ARTIFACTS / name).glob("v*.yaml") if p.stem[1:].isdigit())


def latest_version(name: str) -> int:
    return max(versions(name), default=0)


def load(name_or_path: str, version: int | None = None) -> Artifact:
    path = Path(name_or_path)
    if not path.suffix:
        v = version or latest_version(name_or_path)
        path = ARTIFACTS / name_or_path / f"v{v}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No capability '{name_or_path}'" + (f" v{version}" if version else "") + ". Try `cua list`.")
    return Artifact.model_validate(yaml.safe_load(path.read_text()))


def all_names() -> list[str]:
    return sorted(p.name for p in ARTIFACTS.iterdir() if p.is_dir() and versions(p.name)) if ARTIFACTS.exists() else []


_PH = re.compile(r"\{\{(secret:)?(\w+)\}\}")


def fill(text: str | None, params: dict[str, str]) -> str | None:
    """Substitute {{name}} and {{secret:name}} placeholders."""
    if text is None:
        return None
    return _PH.sub(lambda m: params.get(m.group(2), m.group(0)), text)


def templatize(text: str | None, examples: dict[str, str]) -> str | None:
    """Inverse of fill for recording: replace example input values with {{name}} (longest first)."""
    if not text:
        return text
    for k, v in examples.items():
        if text == v:
            return "{{" + k + "}}"  # an exact match is always the input, however short (amount=1)
    for k, v in sorted(examples.items(), key=lambda kv: -len(kv[1])):
        if len(v) >= 3:
            text = text.replace(v, "{{" + k + "}}")
    return text


def uses_param(target: Target | None) -> bool:
    return bool(target) and any(loc.value and _PH.search(loc.value) for loc in target.locators)
