from __future__ import annotations
import time
import random
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, List
from urllib.parse import urlparse
from collections import deque


@dataclass
class DomainState:
    delay: float = 1.0
    base_delay: float = 1.0
    min_delay: float = 0.2
    max_delay: float = 120.0
    success_streak: int = 0
    failure_streak: int = 0
    total_requests: int = 0
    total_failures: int = 0
    recent_total: int = 0
    latencies: deque = field(default_factory=lambda: deque(maxlen=50))
    error_timestamps: deque = field(default_factory=lambda: deque(maxlen=100))
    request_timestamps: deque = field(default_factory=lambda: deque(maxlen=200))
    circuit_open: bool = False
    circuit_opened_at: float = 0.0
    last_update: float = field(default_factory=time.time)
    last_retry_after: Optional[float] = None
    integral: float = 0.0
    previous_error: float = 0.0

    @property
    def avg_latency(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else 0.0

    @property
    def error_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_failures / self.total_requests

    def recent_error_rate(self, window: float = 60.0) -> float:
        now = time.time()
        recent_errors = sum(1 for t in self.error_timestamps if now - t < window)
        recent_total = sum(1 for t in self.request_timestamps if now - t < window)
        if recent_total == 0:
            return 0.0
        return recent_errors / recent_total


class AdaptiveBackoff:
    def __init__(self,
                 strategy: str = "aimd",
                 base_delay: float = 1.0,
                 min_delay: float = 0.2,
                 max_delay: float = 120.0,
                 jitter: str = "full",
                 circuit_breaker_threshold: int = 5,
                 circuit_cooldown: float = 60.0,
                 pid_kp: float = 0.5,
                 pid_ki: float = 0.1,
                 pid_kd: float = 0.05,
                 suspect_patterns: Optional[List[str]] = None,
                 verbose: bool = False):
        self.strategy = strategy
        self.base_delay = base_delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.jitter = jitter
        self.circuit_threshold = circuit_breaker_threshold
        self.circuit_cooldown = circuit_cooldown
        self.pid_kp = pid_kp
        self.pid_ki = pid_ki
        self.pid_kd = pid_kd
        self.verbose = verbose

        self.suspect_patterns = suspect_patterns or [
            "captcha", "are you human", "access denied", "blocked",
            "rate limit", "too many requests", "unusual traffic",
            "cf-challenge", "cloudflare", "please verify",
        ]

        self._states: Dict[str, DomainState] = {}
        self._lock = threading.RLock()
        self._rng = random.Random()

    def _state(self, host: str) -> DomainState:
        if host not in self._states:
            self._states[host] = DomainState(
                delay=self.base_delay,
                base_delay=self.base_delay,
                min_delay=self.min_delay,
                max_delay=self.max_delay,
            )
        return self._states[host]

    @staticmethod
    def host_of(url: str) -> str:
        if "://" in url:
            return urlparse(url).netloc or url
        return url

    def _apply_jitter(self, delay: float) -> float:
        if self.jitter == "full":
            return self._rng.uniform(0, delay)
        if self.jitter == "equal":
            return delay / 2 + self._rng.uniform(0, delay / 2)
        if self.jitter == "decorrelated":
            return self._rng.uniform(self.min_delay, delay)
        if self.jitter == "none":
            return delay
        return delay

    def on_success(self,
                   url_or_host: str,
                   latency: Optional[float] = None,
                   status: int = 200,
                   body: Optional[str] = None) -> float:
        host = self.host_of(url_or_host)
        with self._lock:
            st = self._state(host)
            st.total_requests += 1
            st.request_timestamps.append(time.time())
            st.success_streak += 1
            st.failure_streak = 0
            if latency is not None:
                st.latencies.append(latency)

            if body and self._is_suspect(body):
                if self.verbose:
                    print(f"[backoff] {host}: suspect page even if 200")
                return self._escalate_locked(st, reason="suspect_page")

            if (latency is not None and st.avg_latency > 0
                    and latency > st.avg_latency * 2.5):
                st.delay = min(st.max_delay, st.delay * 1.3)

            self._decrease(st)
            st.last_update = time.time()
            delay = max(st.min_delay, min(st.max_delay, st.delay))
            if self.verbose:
                print(f"[backoff] {host}: OK -> delay={delay:.2f}s")
            return self._apply_jitter(delay)

    def on_failure(self,
                   url_or_host: str,
                   status: Optional[int] = None,
                   retry_after: Optional[float] = None,
                   exception: Optional[Exception] = None) -> float:
        host = self.host_of(url_or_host)
        with self._lock:
            st = self._state(host)
            st.total_requests += 1
            st.request_timestamps.append(time.time())
            st.total_failures += 1
            st.failure_streak += 1
            st.success_streak = 0
            st.error_timestamps.append(time.time())
            st.last_update = time.time()

            if retry_after is not None and retry_after > 0:
                st.last_retry_after = retry_after
                st.delay = max(st.delay, retry_after)
                if self.verbose:
                    print(f"[backoff] {host}: Retry-After={retry_after}s")
                return self._apply_jitter(min(st.max_delay, retry_after))

            if status in (429, 503):
                st.delay = min(st.max_delay, st.delay * 2.0 + 1.0)
            elif status in (403, 401):
                st.delay = min(st.max_delay, st.delay * 3.0)
            elif status is not None and 500 <= status < 600:
                st.delay = min(st.max_delay, st.delay * 1.5)
            elif exception is not None:
                st.delay = min(st.max_delay, st.delay * 1.8)
            else:
                st.delay = min(st.max_delay, st.delay * 1.5)

            self._maybe_open_circuit(st)

            delay = max(st.min_delay, min(st.max_delay, st.delay))
            if self.verbose:
                print(f"[backoff] {host}: FAILED status={status} -> delay={delay:.2f}s")
            return self._apply_jitter(delay)

    def _decrease(self, st: DomainState) -> None:
        if self.strategy == "aimd":
            if st.success_streak >= 3:
                st.delay = max(st.min_delay, st.delay - 0.1)
                st.success_streak = 0

        elif self.strategy == "simple":
            st.delay = max(st.min_delay, st.delay * 0.9)

        elif self.strategy == "pid":
            target_error = 0.05
            err = st.recent_error_rate() - target_error
            st.integral = max(-10.0, min(10.0, st.integral + err))
            derivative = err - st.previous_error
            st.previous_error = err
            correction = (self.pid_kp * err
                          + self.pid_ki * st.integral
                          + self.pid_kd * derivative)
            st.delay = max(st.min_delay,
                           min(st.max_delay, st.delay + max(-2.0, min(2.0, correction))))

        elif self.strategy == "adaptive":
            if st.success_streak >= 5 and st.recent_error_rate() < 0.01:
                st.delay = max(st.min_delay, st.delay * 0.85)
                st.success_streak = 0

    def _escalate_locked(self, st: DomainState, reason: str = "") -> float:
        st.total_failures += 1
        st.failure_streak += 1
        st.success_streak = 0
        st.error_timestamps.append(time.time())
        st.delay = min(st.max_delay, st.delay * 2.0)
        self._maybe_open_circuit(st)
        delay = max(st.min_delay, min(st.max_delay, st.delay))
        if self.verbose:
            print(f"[backoff] escalation ({reason}) -> {delay:.2f}s")
        return self._apply_jitter(delay)

    def _maybe_open_circuit(self, st: DomainState) -> None:
        if st.failure_streak >= self.circuit_threshold and not st.circuit_open:
            st.circuit_open = True
            st.circuit_opened_at = time.time()
            st.delay = st.max_delay
            if self.verbose:
                print(f"[backoff] OPEN CIRCUIT ({st.failure_streak} failed)")

    def is_available(self, url_or_host: str) -> bool:
        host = self.host_of(url_or_host)
        with self._lock:
            st = self._state(host)
            if not st.circuit_open:
                return True
            if time.time() - st.circuit_opened_at > self.circuit_cooldown:
                st.circuit_open = False
                st.failure_streak = 0
                st.delay = st.base_delay
                if self.verbose:
                    print(f"[backoff] {host}: circuit closed (cooldown)")
                return True
            return False

    def wait_if_blocked(self, url_or_host: str) -> float:
        host = self.host_of(url_or_host)
        waited = 0.0
        while True:
            with self._lock:
                st = self._state(host)
                if not st.circuit_open:
                    return waited
                remaining = self.circuit_cooldown - (time.time() - st.circuit_opened_at)
            if remaining <= 0:
                if self.is_available(host):
                    return waited
            sleep_time = min(max(remaining, 0.1), 5.0)
            if self.verbose:
                print(f"[backoff] {host}: waiting circuit {sleep_time:.1f}s")
            time.sleep(sleep_time)
            waited += sleep_time

    def _is_suspect(self, body: str) -> bool:
        low = body.lower()
        return any(p in low for p in self.suspect_patterns)

    @staticmethod
    def parse_retry_after(response) -> Optional[float]:
        header = getattr(response, "headers", {}).get("Retry-After") \
            if hasattr(response, "headers") else None
        if not header:
            return None
        try:
            return float(header)
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(header)
                return max(0.0, dt.timestamp() - time.time())
            except Exception:
                return None

    def request(self, session, method: str, url: str,
                max_retries: int = 5, **kwargs):
        host = self.host_of(url)
        last_exc: Optional[Exception] = None
        last_resp = None

        for attempt in range(max_retries):
            self.wait_if_blocked(host)

            t0 = time.time()
            try:
                resp = session.request(method, url, **kwargs)
                latency = time.time() - t0
                status = resp.status_code
                body = getattr(resp, "text", "") or ""

                if status in (200, 201, 202, 204):
                    delay = self.on_success(
                        host, latency=latency, status=status,
                        body=body[:5000],
                    )
                    time.sleep(delay)
                    return resp

                if status in (429, 503):
                    ra = self.parse_retry_after(resp)
                    delay = self.on_failure(host, status=status, retry_after=ra)
                    last_resp = resp
                    time.sleep(delay)
                    continue

                if status in (403, 401):
                    delay = self.on_failure(host, status=status)
                    last_resp = resp
                    time.sleep(delay)
                    continue

                if 500 <= status < 600:
                    delay = self.on_failure(host, status=status)
                    last_resp = resp
                    time.sleep(delay)
                    continue

                delay = self.on_success(host, latency=latency, status=status)
                time.sleep(delay)
                return resp

            except Exception as e:
                last_exc = e
                delay = self.on_failure(host, exception=e)
                if self.verbose:
                    print(f"[backoff] {host}: exception {e!r} -> {delay:.2f}s")
                time.sleep(delay)

        if self.verbose:
            print(f"[backoff] {host}: failed after {max_retries} attempts")
        if last_resp is not None:
            return last_resp
        if last_exc:
            raise last_exc
        return None

    def stats(self) -> Dict[str, Dict]:
        with self._lock:
            return {
                host: {
                    "delay": round(st.delay, 3),
                    "avg_latency": round(st.avg_latency, 3),
                    "error_rate": round(st.error_rate, 3),
                    "recent_error_rate": round(st.recent_error_rate(), 3),
                    "success_streak": st.success_streak,
                    "failure_streak": st.failure_streak,
                    "circuit_open": st.circuit_open,
                    "total_requests": st.total_requests,
                }
                for host, st in self._states.items()
            }

    def reset(self, url_or_host: Optional[str] = None) -> None:
        with self._lock:
            if url_or_host is None:
                self._states.clear()
            else:
                self._states.pop(self.host_of(url_or_host), None)


def adaptive_backoff(backoff: "AdaptiveBackoff", session=None):
    def decorator(func):
        def wrapper(method: str, url: str, **kwargs):
            sess = session
            if sess is None:
                import requests
                sess = requests.Session()
            return backoff.request(sess, method, url, **kwargs)
        return wrapper
    return decorator


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--strategy",
                   choices=["aimd", "pid", "simple", "adaptive"],
                   default="aimd")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--demo", action="store_true")
    args = p.parse_args()

    if args.demo:
        bo = AdaptiveBackoff(strategy=args.strategy, verbose=True)
        host = "exemple.local"
        print("-- 5 succès --")
        for _ in range(5):
            print(f"  delay={bo.on_success(host, latency=0.4):.3f}")
        print("-- 4x 429 --")
        for _ in range(4):
            print(f"  delay={bo.on_failure(host, status=429):.3f}")
        print("-- Retry-After=15 --")
        print(f"  delay={bo.on_failure(host, status=429, retry_after=15.0):.3f}")
        print("-- 6x 503 --")
        for _ in range(6):
            print(f"  delay={bo.on_failure(host, status=503):.3f} "
                  f"dispo={bo.is_available(host)}")
        print("-- stats --")
        for k, v in bo.stats().items():
            print(f"  {k}: {v}")
    else:
        print(f"AdaptiveBackoff ready (strategy={args.strategy})")