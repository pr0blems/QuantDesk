"""Framework-free safety value objects for every execution mode."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


class SafetyMode(StrEnum):
    NORMAL = "normal"
    SAFE_MODE = "safe_mode"
    KILL_SWITCH = "kill_switch"


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    """One deterministic, redacted readiness check."""

    name: str
    passed: bool
    code: str = "ok"

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 64:
            raise ValueError("preflight check name must contain 1-64 characters")
        if not self.code or len(self.code) > 64:
            raise ValueError("preflight check code must contain 1-64 characters")
        if self.passed and self.code != "ok":
            raise ValueError("a passing preflight check must use the ok code")
        if not self.passed and self.code == "ok":
            raise ValueError("a failed preflight check must provide a failure code")


@dataclass(frozen=True, slots=True)
class PreflightReport:
    checked_at: datetime
    checks: tuple[PreflightCheck, ...]

    def __post_init__(self) -> None:
        _aware(self.checked_at, "checked_at")
        names = [check.name for check in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("preflight check names must be unique")

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(check.code for check in self.checks if not check.passed)


@dataclass(frozen=True, slots=True)
class SafetySnapshot:
    mode: SafetyMode
    consecutive_failures: int
    reason_codes: tuple[str, ...]
    changed_at: datetime

    def __post_init__(self) -> None:
        _aware(self.changed_at, "changed_at")
        if self.consecutive_failures < 0:
            raise ValueError("consecutive_failures must not be negative")
        if self.mode is SafetyMode.NORMAL and self.reason_codes:
            raise ValueError("normal safety mode cannot carry blocking reasons")
        if self.mode is not SafetyMode.NORMAL and not self.reason_codes:
            raise ValueError("a blocking safety mode must carry a reason")


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    allowed: bool
    mode: SafetyMode
    reason_code: str

    def __post_init__(self) -> None:
        if not self.reason_code or len(self.reason_code) > 64:
            raise ValueError("safety reason code must contain 1-64 characters")
        if self.allowed and self.reason_code != "ok":
            raise ValueError("an allowed safety decision must use the ok code")
        if not self.allowed and self.reason_code == "ok":
            raise ValueError("a blocked safety decision must provide a reason")
