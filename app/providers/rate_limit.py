from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


WINDOW_SECONDS = 60.0
DEFAULT_WAIT_SLICE = 0.05


class AdmissionError(RuntimeError):
    error_code = "RATE_LIMIT"

    def __init__(self, message: str, *, error_code: str | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.error_code = error_code or self.error_code
        self.retry_after = retry_after


class AdmissionCancelled(AdmissionError):
    error_code = "RATE_LIMIT_CANCELLED"


class AdmissionTimeout(AdmissionError):
    error_code = "RATE_LIMIT_TIMEOUT"


class AdmissionOversized(AdmissionError):
    error_code = "RATE_LIMIT_OVERSIZED"


class AdmissionInvalidLimits(AdmissionError):
    error_code = "RATE_LIMIT_INVALID_LIMIT"


def account_context_key(provider_name: str, model: str, account_ref: str | None = None) -> str:
    raw = f"{provider_name or 'unknown'}|{model or 'unknown'}|{account_ref or 'anonymous'}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def resolve_rate_limits(config: dict[str, Any] | None) -> dict[str, Any]:
    config = config or {}
    extra = dict(config.get("extra_options") or {})
    rpm = extra.get("rpm", config.get("rpm"))
    tpm = extra.get("tpm", config.get("tpm"))
    try:
        rpm_n = int(rpm) if rpm is not None and str(rpm).strip() != "" else None
    except (TypeError, ValueError):
        rpm_n = None
    try:
        tpm_n = int(tpm) if tpm is not None and str(tpm).strip() != "" else None
    except (TypeError, ValueError):
        tpm_n = None
    invalid = []
    if rpm is not None and str(rpm).strip() != "" and rpm_n is None:
        invalid.append("rpm")
    if tpm is not None and str(tpm).strip() != "" and tpm_n is None:
        invalid.append("tpm")
    if rpm_n is not None and rpm_n <= 0:
        invalid.append("rpm")
    if tpm_n is not None and tpm_n <= 0:
        invalid.append("tpm")
    if invalid:
        return {
            "rpm": rpm_n,
            "tpm": tpm_n,
            "enforced": False,
            "invalid": True,
            "invalid_fields": invalid,
            "provenance": "invalid_limits",
            "note": "Configured RPM/TPM must be positive integers; refusing to enforce nonsensical limits.",
        }
    if rpm_n is None and tpm_n is None:
        return {
            "rpm": None,
            "tpm": None,
            "enforced": False,
            "invalid": False,
            "provenance": "unknown_limits",
            "note": "RPM/TPM are not configured; admission is not claimed to be enforced.",
        }
    return {
        "rpm": rpm_n,
        "tpm": tpm_n,
        "enforced": True,
        "invalid": False,
        "provenance": "deployment_configured",
        "note": "Admission uses deployment-configured RPM/TPM, not live vendor discovery.",
    }


@dataclass
class Reservation:
    reservation_id: str
    context_key: str
    reserved_input: int
    reserved_output: int
    reserved_requests: int = 1
    started_at: float = 0.0
    enforced: bool = False
    provenance: str = "unknown_limits"
    rpm: int | None = None
    tpm: int | None = None
    retry_after: float | None = None


@dataclass
class _Window:
    events: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class SlidingWindowLimiter:
    """Concurrency-safe RPM/TPM sliding window. Fake-clock friendly; no busy-spin."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        window_seconds: float = WINDOW_SECONDS,
    ):
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self.window_seconds = float(window_seconds)
        self._windows: dict[str, _Window] = {}
        self._retry_after_until: dict[str, float] = {}
        self._meta_lock = threading.Lock()
        self._seq = 0

    def _window(self, key: str) -> _Window:
        with self._meta_lock:
            bucket = self._windows.get(key)
            if bucket is None:
                bucket = _Window()
                self._windows[key] = bucket
            return bucket

    def _prune(self, bucket: _Window, now: float) -> None:
        # Half-open (cutoff, now]: an event at exactly now-window has expired.
        cutoff = now - self.window_seconds
        eps = 1e-9
        bucket.events = [row for row in bucket.events if float(row.get("ts") or 0) > cutoff + eps]

    def _totals(self, bucket: _Window) -> tuple[int, int]:
        requests = sum(int(row.get("requests") or 0) for row in bucket.events)
        tokens = sum(int(row.get("tokens") or 0) for row in bucket.events)
        return requests, tokens

    def _next_free_at(self, bucket: _Window, *, rpm: int | None, tpm: int | None, need_tokens: int, now: float) -> float:
        if not bucket.events:
            return now
        candidates: list[float] = []
        if rpm:
            ordered = sorted(bucket.events, key=lambda row: float(row.get("ts") or 0))
            if len(ordered) >= rpm:
                candidates.append(float(ordered[len(ordered) - rpm].get("ts") or 0) + self.window_seconds)
        if tpm:
            remaining = need_tokens
            used = sum(int(row.get("tokens") or 0) for row in bucket.events)
            if used + remaining > tpm:
                ordered = sorted(bucket.events, key=lambda row: float(row.get("ts") or 0))
                running = used
                for row in ordered:
                    running -= int(row.get("tokens") or 0)
                    if running + remaining <= tpm:
                        candidates.append(float(row.get("ts") or 0) + self.window_seconds)
                        break
                else:
                    if ordered:
                        candidates.append(float(ordered[-1].get("ts") or 0) + self.window_seconds)
        return max(candidates) if candidates else now

    def _sleep(self, seconds: float, deadline: float, cancel_event: threading.Event | None) -> None:
        if seconds <= 0:
            return
        remaining = min(max(0.0, seconds), max(0.0, deadline - self._clock()))
        if remaining <= 0:
            return
        if cancel_event is None:
            self._sleeper(remaining)
            return
        end = self._clock() + remaining
        while True:
            if cancel_event.is_set():
                raise AdmissionCancelled("Rate-limit wait cancelled.")
            now = self._clock()
            left = end - now
            if left <= 0:
                return
            self._sleeper(min(DEFAULT_WAIT_SLICE, left))

    def admit(
        self,
        *,
        provider_name: str,
        model: str,
        account_ref: str | None,
        config: dict[str, Any] | None,
        estimated_input: int,
        estimated_output: int,
        deadline_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
        retry_after: float | None = None,
    ) -> Reservation:
        limits = resolve_rate_limits(config)
        key = account_context_key(provider_name, model, account_ref)
        reserved_in = max(0, int(estimated_input or 0))
        reserved_out = max(0, int(estimated_output or 0))
        need = reserved_in + reserved_out
        now = self._clock()
        with self._meta_lock:
            self._seq += 1
            reservation_id = f"{key}-{self._seq}"
        reservation = Reservation(
            reservation_id=reservation_id,
            context_key=key,
            reserved_input=reserved_in,
            reserved_output=reserved_out,
            started_at=now,
            enforced=bool(limits["enforced"]),
            provenance=str(limits["provenance"]),
            rpm=limits.get("rpm"),
            tpm=limits.get("tpm"),
            retry_after=retry_after,
        )
        if limits.get("invalid"):
            raise AdmissionInvalidLimits(
                "Configured RPM/TPM must be positive integers; refusing admission.",
            )
        retry_after_s = None
        try:
            if retry_after is not None and float(retry_after) > 0:
                retry_after_s = float(retry_after)
        except (TypeError, ValueError):
            retry_after_s = None
        if retry_after_s:
            with self._meta_lock:
                self._retry_after_until[key] = max(float(self._retry_after_until.get(key) or 0), now + retry_after_s)
        default_deadline = 30.0 if deadline_seconds is None else float(deadline_seconds)
        if retry_after_s:
            default_deadline = max(default_deadline, retry_after_s + 1.0)
        deadline = now + max(1.0, default_deadline)
        self._wait_retry_after(key, deadline, cancel_event)
        now = self._clock()
        if not limits["enforced"]:
            reservation.started_at = now
            return reservation
        rpm = limits.get("rpm")
        tpm = limits.get("tpm")
        if tpm is not None and need > int(tpm):
            raise AdmissionOversized(
                f"Request token budget {need} exceeds configured TPM window {tpm}.",
                retry_after=None,
            )
        bucket = self._window(key)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise AdmissionCancelled("Rate-limit wait cancelled.")
            now = self._clock()
            if now >= deadline - 1e-12:
                raise AdmissionTimeout("Rate-limit admission deadline exceeded.")
            wait_for = 0.0
            with bucket.lock:
                self._prune(bucket, now)
                requests, tokens = self._totals(bucket)
                rpm_blocked = rpm is not None and requests + 1 > int(rpm)
                tpm_blocked = tpm is not None and tokens + need > int(tpm)
                if not rpm_blocked and not tpm_blocked:
                    bucket.events.append({
                        "id": reservation_id,
                        "ts": now,
                        "requests": 1,
                        "tokens": need,
                        "input": reserved_in,
                        "output": reserved_out,
                    })
                    reservation.started_at = now
                    return reservation
                free_at = self._next_free_at(bucket, rpm=rpm, tpm=tpm, need_tokens=need, now=now)
                wait_for = max(0.0, min(free_at, deadline) - now)
            if wait_for <= 1e-12:
                raise AdmissionTimeout("Rate-limit window is full and the wait deadline was reached.")
            self._sleep(wait_for, deadline, cancel_event)

    def _wait_retry_after(self, key: str, deadline: float, cancel_event: threading.Event | None) -> None:
        with self._meta_lock:
            until = float(self._retry_after_until.get(key) or 0)
        now = self._clock()
        if until <= now:
            return
        wait_for = min(until, deadline) - now
        if wait_for <= 1e-12:
            if until > deadline + 1e-12:
                raise AdmissionTimeout("Retry-After wait exceeded admission deadline.")
            return
        self._sleep(wait_for, deadline, cancel_event)
        if self._clock() + 1e-12 < until and self._clock() >= deadline - 1e-12:
            raise AdmissionTimeout("Retry-After wait exceeded admission deadline.")

    def reconcile(self, reservation: Reservation, *, actual_input: int | None, actual_output: int | None) -> None:
        if not reservation.enforced:
            return
        bucket = self._window(reservation.context_key)
        actual_in = reservation.reserved_input if actual_input is None else max(0, int(actual_input))
        actual_out = reservation.reserved_output if actual_output is None else max(0, int(actual_output))
        # Conservative: never shrink below the reserved budget inside the live window.
        tokens = max(reservation.reserved_input + reservation.reserved_output, actual_in + actual_out)
        with bucket.lock:
            for row in bucket.events:
                if row.get("id") == reservation.reservation_id:
                    row["tokens"] = tokens
                    row["input"] = max(reservation.reserved_input, actual_in)
                    row["output"] = max(reservation.reserved_output, actual_out)
                    return

    def snapshot(self, context_key: str) -> dict[str, Any]:
        bucket = self._window(context_key)
        now = self._clock()
        with bucket.lock:
            self._prune(bucket, now)
            requests, tokens = self._totals(bucket)
            return {"requests": requests, "tokens": tokens, "events": len(bucket.events)}


_DEFAULT = SlidingWindowLimiter()


def default_limiter() -> SlidingWindowLimiter:
    return _DEFAULT


def reset_default_limiter_for_tests(limiter: SlidingWindowLimiter | None = None) -> SlidingWindowLimiter:
    global _DEFAULT
    _DEFAULT = limiter or SlidingWindowLimiter()
    return _DEFAULT
