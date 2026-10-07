"""No-model receipt observer: records missed cloud runs without calling an LLM."""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")


class RunObserver:
    def __init__(
        self, root: Path, schedules: dict[str, tuple[int, int, int]], *, now=None
    ):
        self.root, self.schedules = root, schedules
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._locked():
            if not self.path.exists():
                self._write(
                    {
                        "activated_at": (now or datetime.now(UTC)).isoformat(),
                        "events": [],
                    }
                )
            data = self._read()
            definitions = {k: list(v) if v is not None else None for k, v in schedules.items()}
            prior = data.get("schedule_definitions", {})
            activation = data.setdefault("schedule_activated_at", {})
            at = (now or datetime.now(UTC)).isoformat()
            for task, definition in definitions.items():
                if task not in prior or prior[task] != definition:
                    activation[task] = at
            if prior != definitions:
                data["schedule_definitions"] = definitions
                self._write(data)

    @property
    def path(self):
        return self.root / "cloud-runs.json"

    def _locked(self):
        from contextlib import contextmanager

        @contextmanager
        def locked():
            with (self.root / "cloud-runs.lock").open("a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                yield

        return locked()

    def _read(self):
        return json.loads(self.path.read_text())

    def _write(self, state):
        state["events"] = state["events"][-200:]
        p = self.root / "cloud-runs.tmp"
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        p.chmod(0o600)
        p.replace(self.path)

    def _due(self, task, at):
        if self.schedules[task] is None:
            return None
        day, hour, minute = self.schedules[task]
        local = at.astimezone(ZONE)
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day != -1:
            candidate -= timedelta(days=(local.weekday() - day) % 7)
        if candidate > local:
            candidate -= timedelta(days=1 if day == -1 else 7)
        return candidate.astimezone(UTC)

    def receipt(self, task, phase, *, run_id=None, detail="", now=None):
        if task not in self.schedules or phase not in {
            "start",
            "success",
            "skipped",
            "failed",
        }:
            raise ValueError("unknown task or phase")
        at = now or datetime.now(UTC)
        with self._locked():
            data = self._read()
            if phase == "start":
                run_id = uuid.uuid4().hex
                due = self._due(task, at)
                slot = (
                    due.isoformat()
                    if due is not None and timedelta(0) <= at - due <= timedelta(hours=4)
                    else None
                )
            else:
                starts = [
                    e
                    for e in data["events"]
                    if e.get("run_id") == run_id
                    and e["phase"] == "start"
                    and e["task"] == task
                ]
                if not starts:
                    raise ValueError("start receipt required")
                slot = starts[-1]["slot"]
                ended = [
                    e
                    for e in data["events"]
                    if e.get("run_id") == run_id
                    and e["phase"] in {"success", "skipped", "failed"}
                ]
                if ended:
                    return {**ended[-1], "replayed": True}
            event = {
                "task": task,
                "phase": phase,
                "run_id": run_id,
                "at": at.isoformat(),
                "slot": slot,
                "detail": re.sub(r"sk-[A-Za-z0-9_-]{16,}", "[redacted]", str(detail))[
                    :500
                ],
                "delivery": "not_independently_verified",
            }
            data["events"].append(event)
            self._write(data)
            return event

    def check(self, *, now=None):
        at = now or datetime.now(UTC)
        with self._locked():
            data = self._read()
            old_count = len(data["events"])
            activated = datetime.fromisoformat(data["activated_at"])
            for task in self.schedules:
                latest = self._due(task, at)
                if latest is None:
                    continue
                task_activation = datetime.fromisoformat(data.get("schedule_activated_at", {}).get(task, data["activated_at"]))
                offsets = range(14) if self.schedules[task][0] == -1 else (0, 7, 14)
                # Catch up bounded recent missed periods after the Mac resumes.
                for offset in offsets:
                    due = latest - timedelta(days=offset)
                    if (
                        due < max(activated, task_activation)
                        or due < at - timedelta(days=14)
                        or at < due + timedelta(hours=1)
                    ):
                        continue
                    slot = due.isoformat()
                    starts = [e for e in data["events"] if e["task"] == task and e.get("slot") == slot and e["phase"] == "start"]
                    if starts and at - datetime.fromisoformat(starts[-1]["at"]) < timedelta(hours=1):
                        continue
                    terminal = [
                        e
                        for e in data["events"]
                        if e["task"] == task
                        and e.get("slot") == slot
                        and e["phase"]
                        in {"success", "skipped", "failed", "unconfirmed"}
                    ]
                    if not terminal:
                        data["events"].append(
                            {
                                "task": task,
                                "phase": "unconfirmed",
                                "at": at.isoformat(),
                                "slot": slot,
                                "detail": "No completion receipt by the 60-minute deadline. Quota, provider, scheduler, network or local unavailability are possible; exact cause unknown.",
                                "delivery": "not_independently_verified",
                            }
                        )
            if len(data["events"]) != old_count:
                self._write(data)
            return {
                "activated_at": data["activated_at"],
                "schedules": data.get("schedule_definitions", {}),
                "recent": data["events"][-20:],
                "fallback": "record_unconfirmed; this observer never calls or switches models",
                "limits": "Requires local observer online; on resume checks last 14 days. Missing receipt is not proof of exact failure cause or notification delivery.",
            }

    def start_background(self):
        stop = threading.Event()

        def run():
            while not stop.is_set():
                try:
                    self.check()
                except (OSError, ValueError):
                    pass  # source tools stay available; explicit check surfaces errors
                stop.wait(60)

        threading.Thread(target=run, name="cloud-run-observer", daemon=True).start()
        return stop


def _engram_schedules():
    """Public distribution: schedules are explicit; no deployment policy is read."""
    raw = os.environ.get("ENGRAM_RUN_SCHEDULES")
    if not raw:
        return {"manual": None}
    try:
        configured = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("ENGRAM_RUN_SCHEDULES must be a JSON object") from error
    if not isinstance(configured, dict) or len(configured) > 32:
        raise ValueError("ENGRAM_RUN_SCHEDULES must contain at most 32 jobs")
    schedules = {"manual": None}
    for name, value in configured.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
            raise ValueError("invalid schedule name")
        if value is None:
            schedules[name] = None
            continue
        if not isinstance(value, list) or len(value) != 3 or any(type(v) is not int for v in value):
            raise ValueError("schedule must be [weekday, hour, minute] or null")
        day, hour, minute = value
        if not (-1 <= day <= 6 and 0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("schedule values are out of range")
        schedules[name] = (day, hour, minute)
    return schedules


ENGRAM_SCHEDULES = _engram_schedules()
