"""Per-run evidence folder: runs/<run_id>/ with log.jsonl, screenshots, result.json. Everything is redacted on write."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .policy import redact_obj

RUNS = Path("runs")


class RunLog:
    def __init__(self, name: str, kind: str, secrets: list[str]):
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_id = f"{stamp}-{kind}-{name}"
        self.dir = RUNS / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.secrets = secrets
        self._shots = 0

    def log(self, event: str, **fields) -> None:
        line = {"ts": dt.datetime.now().isoformat(timespec="milliseconds"), "event": event, **fields}
        with open(self.dir / "log.jsonl", "a") as f:
            f.write(json.dumps(redact_obj(line, self.secrets), default=str) + "\n")

    def image(self, png: bytes, label: str) -> str:
        self._shots += 1
        path = self.dir / f"{self._shots:02d}-{label}.png"
        path.write_bytes(png)
        return str(path)

    def write(self, filename: str, data: dict | str) -> str:
        path = self.dir / filename
        clean = redact_obj(data, self.secrets)
        path.write_text(clean if isinstance(clean, str) else json.dumps(clean, indent=2, default=str))
        return str(path)


def last_result(name: str) -> dict | None:
    """Most recent replay result for a capability (for `cua list`)."""
    runs = sorted(RUNS.glob(f"*-run-{name}/result.json")) if RUNS.exists() else []
    return json.loads(runs[-1].read_text()) if runs else None
