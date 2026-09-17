from __future__ import annotations
import time
import random
import asyncio
import threading
from typing import Optional, Callable, Any
from functools import wraps


class DelayJitter:

    def __init__(self,
                 min_delay: float = 0.5,
                 max_delay: float = 3.0,
                 mode: str = "uniform",
                 seed: Optional[int] = None):
        if min_delay < 0 or max_delay < min_delay:
            raise ValueError("min_delay >= 0 et max_delay >= min_delay requis")
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.mode = mode.lower()
        self._rng = random.Random(seed)

    def _compute(self) -> float:
        lo, hi = self.min_delay, self.max_delay
        mean = (lo + hi) / 2
        sigma = max((hi - lo) / 6, 1e-6)

        if self.mode == "uniform":
            return self._rng.uniform(lo, hi)

        if self.mode == "gaussian":
            for _ in range(100):
                v = self._rng.gauss(mean, sigma)
                if lo <= v <= hi:
                    return v
            return mean

        if self.mode == "exponential":
            scale = max((hi - lo) / 3, 1e-6)
            for _ in range(100):
                v = lo + self._rng.expovariate(1 / scale)
                if v <= hi:
                    return v
            return hi

        if self.mode == "lognormal":
            mu = max((lo + hi) / 2, 1e-6)
            for _ in range(100):
                v = self._rng.lognormvariate(0, 0.5) * mu
                if lo <= v <= hi:
                    return v
            return mu

        if self.mode == "human":
            if self._rng.random() < 0.9:
                return self._rng.uniform(lo, min(hi, lo + (hi - lo) * 0.4))
            return self._rng.uniform(lo + (hi - lo) * 0.4, hi)

        raise ValueError(f"Mode inconnu : {self.mode}")

    def get(self) -> float:
        return self._compute()

    def sleep(self) -> float:
        d = self._compute()
        time.sleep(d)
        return d

    async def async_sleep(self) -> float:
        d = self._compute()
        await asyncio.sleep(d)
        return d


def backoff_delay(attempt: int,
                  base: float = 1.0,
                  factor: float = 2.0,
                  max_delay: float = 60.0,
                  jitter: str = "full",
                  min_delay: float = 0.1) -> float:
    """Backoff exponentiel avec jitter. Jamais < min_delay (évite busy-loop)."""
    raw = min(max_delay, base * (factor ** attempt))

    if jitter == "full":
        return max(min_delay, random.uniform(0, raw))
    if jitter == "equal":
        return max(min_delay, raw / 2 + random.uniform(0, raw / 2))
    if jitter == "decorrelated":
        return max(min_delay, random.uniform(base, raw))
    if jitter == "none":
        return max(min_delay, raw)
    raise ValueError(f"Jitter inconnu : {jitter}")


class RateLimiter:
    """
    Limite à `max_calls` appels par `period` secondes.
    Thread-safe : utilisable depuis DistributedBotPool.
    """

    def __init__(self, max_calls: int = 5, period: float = 1.0):
        if max_calls <= 0 or period <= 0:
            raise ValueError("max_calls et period doivent être > 0")
        self.max_calls = max_calls
        self.period = period
        self._calls: list = []
        self._lock = threading.Lock()

    def wait(self) -> float:
        """Bloque si nécessaire. Retourne le temps d'attente effectif."""
        waited = 0.0
        with self._lock:
            now = time.time()
            self._calls = [t for t in self._calls if now - t < self.period]
            if len(self._calls) >= self.max_calls:
                sleep_time = self.period - (now - self._calls[0])
                if sleep_time > 0:
                    waited = sleep_time
            # Ajout immédiat pour réserver le slot AVANT de dormir
            self._calls.append(time.time() + waited)
        if waited > 0:
            time.sleep(waited)
        return waited


def with_delay(jitter: DelayJitter):
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            jitter.sleep()
            return func(*args, **kwargs)
        return wrapper
    return decorator


def with_async_delay(jitter: DelayJitter):
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            await jitter.async_sleep()
            return await func(*args, **kwargs)
        return wrapper
    return decorator


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mode", default="human",
                   choices=["uniform", "gaussian", "exponential", "lognormal", "human"])
    p.add_argument("--min", type=float, default=0.5, dest="min_delay")
    p.add_argument("--max", type=float, default=3.0, dest="max_delay")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--sleep", action="store_true")
    args = p.parse_args()

    j = DelayJitter(args.min_delay, args.max_delay, args.mode)
    for i in range(args.n):
        if args.sleep:
            print(f"[{i+1}] dormi {j.sleep():.3f}s")
        else:
            print(f"[{i+1}] {j.get():.3f}s")