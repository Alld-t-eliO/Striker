"""
delay_jitter.py
---------------
Gestion des délais aléatoires (jitter) entre requêtes HTTP.

Fonctionnalités :
- Délai uniforme, gaussien, exponentiel, log-normal
- Mode "human-like" : petites pauses fréquentes + pauses longues rares
- Backoff exponentiel avec jitter (pour les retries)
- Rate-limiter (max N requêtes / seconde)
- Décorateur @with_delay pour envelopper une fonction
- Contexte "session" pour enchaîner plusieurs requêtes
- Support async (asyncio)
"""

import time
import random
import asyncio
from typing import Optional, Callable, Any
from functools import wraps


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------
class DelayJitter:
    """
    Générateur de délais aléatoires (jitter) pour espacer les requêtes.
    """

    def __init__(self,
                 min_delay: float = 0.5,
                 max_delay: float = 3.0,
                 mode: str = "uniform",
                 seed: Optional[int] = None):
        """
        :param min_delay: Délai minimum (secondes)
        :param max_delay: Délai maximum (secondes)
        :param mode: 'uniform' | 'gaussian' | 'exponential' | 'lognormal' | 'human'
        :param seed: Graine aléatoire (pour reproductibilité)
        """
        if min_delay < 0 or max_delay < min_delay:
            raise ValueError("min_delay doit être >= 0 et max_delay >= min_delay.")
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.mode = mode.lower()
        if seed is not None:
            random.seed(seed)

    # ------- Calcul du délai selon le mode -------
    def _compute(self) -> float:
        lo, hi = self.min_delay, self.max_delay
        mean = (lo + hi) / 2
        sigma = (hi - lo) / 6  # ~99.7% des valeurs dans [lo, hi]

        if self.mode == "uniform":
            return random.uniform(lo, hi)

        elif self.mode == "gaussian":
            # Valeur gaussienne tronquée dans [lo, hi]
            while True:
                v = random.gauss(mean, sigma)
                if lo <= v <= hi:
                    return v

        elif self.mode == "exponential":
            # Espacement exponentiel : petites pauses fréquentes, longues rares
            # On normalise pour retomber dans [lo, hi]
            scale = (hi - lo) / 3  # moyenne ≈ lo + scale
            while True:
                v = lo + random.expovariate(1 / scale)
                if v <= hi:
                    return v

        elif self.mode == "lognormal":
            # Log-normale : très proche du comportement humain
            # Paramètres ajustés pour rester dans [lo, hi]
            mu = (lo + hi) / 2
            sigma_logn = 0.5
            while True:
                v = random.lognormvariate(0, sigma_logn) * mu
                if lo <= v <= hi:
                    return v

        elif self.mode == "human":
            # 90% : petite pause courte ; 10% : pause longue ("lecture")
            if random.random() < 0.9:
                return random.uniform(lo, min(hi, lo + (hi - lo) * 0.4))
            else:
                return random.uniform(lo + (hi - lo) * 0.4, hi)

        else:
            raise ValueError(f"Mode inconnu : {self.mode}")

    # ------- API publique -------
    def get(self) -> float:
        """Retourne un délai aléatoire (en secondes) sans attendre."""
        return self._compute()

    def sleep(self) -> float:
        """Attend un délai aléatoire. Retourne le délai effectivement attendu."""
        delay = self._compute()
        time.sleep(delay)
        return delay

    async def async_sleep(self) -> float:
        """Version asynchrone de sleep()."""
        delay = self._compute()
        await asyncio.sleep(delay)
        return delay


# ---------------------------------------------------------------------------
# Backoff exponentiel avec jitter (pour retries)
# ---------------------------------------------------------------------------
def backoff_delay(attempt: int,
                  base: float = 1.0,
                  factor: float = 2.0,
                  max_delay: float = 60.0,
                  jitter: str = "full") -> float:
    """
    Calcule un délai de backoff exponentiel avec jitter.

    :param attempt: Numéro de la tentative (0, 1, 2, ...)
    :param base: Délai initial
    :param factor: Multiplicateur à chaque tentative
    :param max_delay: Plafond
    :param jitter: 'full' | 'equal' | 'decorrelated' | 'none'
    :return: Délai en secondes
    """
    raw = min(max_delay, base * (factor ** attempt))

    if jitter == "full":
        return random.uniform(0, raw)
    elif jitter == "equal":
        return raw / 2 + random.uniform(0, raw / 2)
    elif jitter == "decorrelated":
        return random.uniform(base, raw)
    elif jitter == "none":
        return raw
    else:
        raise ValueError(f"Type de jitter inconnu : {jitter}")


# ---------------------------------------------------------------------------
# Rate limiter simple (max N requêtes / seconde)
# ---------------------------------------------------------------------------
class RateLimiter:
    """
    Limite le débit à `max_calls` appels par `period` secondes.
    """

    def __init__(self, max_calls: int = 5, period: float = 1.0):
        self.max_calls = max_calls
        self.period = period
        self.calls = []

    def wait(self) -> float:
        """Bloque si nécessaire pour respecter la limite."""
        now = time.time()
        # On garde seulement les appels dans la fenêtre glissante
        self.calls = [t for t in self.calls if now - t < self.period]
        if len(self.calls) >= self.max_calls:
            sleep_time = self.period - (now - self.calls[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
            now = time.time()
            self.calls = [t for t in self.calls if now - t < self.period]
        self.calls.append(time.time())
        return 0.0


# ---------------------------------------------------------------------------
# Décorateur @with_delay
# ---------------------------------------------------------------------------
def with_delay(jitter: DelayJitter):
    """
    Décorateur qui applique un délai aléatoire AVANT chaque appel de la fonction.
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            jitter.sleep()
            return func(*args, **kwargs)
        return wrapper
    return decorator


def with_async_delay(jitter: DelayJitter):
    """Version async du décorateur."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            await jitter.async_sleep()
            return await func(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Démo + intégration requests
# ---------------------------------------------------------------------------
def demo_basic():
    print("=== Délais par mode (3 tirages chacun) ===")
    for mode in ["uniform", "gaussian", "exponential", "lognormal", "human"]:
        j = DelayJitter(min_delay=0.5, max_delay=3.0, mode=mode)
        values = [round(j.get(), 3) for _ in range(3)]
        print(f"{mode:>12} : {values}")

    print("\n=== Backoff exponentiel (full jitter) ===")
    for attempt in range(6):
        d = backoff_delay(attempt, base=1.0, factor=2.0, max_delay=30.0, jitter="full")
        print(f"Tentative {attempt} -> {d:.2f}s")


def demo_requests():
    try:
        import requests
    except ImportError:
        print("pip install requests")
        return

    from user_agent_rotator import UserAgentRotator  # si tu as le fichier précédent
    ua_rot = UserAgentRotator()
    jitter = DelayJitter(min_delay=0.5, max_delay=2.0, mode="human")
    limiter = RateLimiter(max_calls=2, period=1.0)

    urls = [
        "https://httpbin.org/get",
        "https://httpbin.org/user-agent",
        "https://httpbin.org/headers",
        "https://httpbin.org/ip",
    ]

    for i, url in enumerate(urls, 1):
        limiter.wait()
        delay = jitter.sleep()
        headers = ua_rot.get_headers()
        print(f"[{i}] pause {delay:.2f}s -> {url}")
        try:
            r = requests.get(url, headers=headers, timeout=10)
            print(f"    status={r.status_code}")
        except Exception as e:
            print(f"    erreur : {e}")


async def demo_async():
    print("\n=== Démo async (5 tâches) ===")
    jitter = DelayJitter(min_delay=0.2, max_delay=1.0, mode="exponential")

    @with_async_delay(jitter)
    async def task(n: int):
        print(f"tâche {n} exécutée à {time.strftime('%H:%M:%S')}")

    await asyncio.gather(*(task(i) for i in range(5)))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Générateur de délais aléatoires (jitter).")
    parser.add_argument("--mode", choices=["uniform", "gaussian", "exponential", "lognormal", "human"],
                        default="uniform", help="Stratégie de jitter")
    parser.add_argument("--min", type=float, default=0.5, dest="min_delay", help="Délai min")
    parser.add_argument("--max", type=float, default=3.0, dest="max_delay", help="Délai max")
    parser.add_argument("-n", "--number", type=int, default=10, help="Nombre de valeurs à générer")
    parser.add_argument("--sleep", action="store_true", help="Attendre réellement (sinon affiche seulement)")
    parser.add_argument("--demo", action="store_true", help="Démo avec requests + UA rotator")
    parser.add_argument("--demo-async", action="store_true", help="Démo asynchrone")
    parser.add_argument("--backoff", type=int, metavar="N", help="Afficher N backoffs exponentiels")
    args = parser.parse_args()

    if args.demo:
        demo_requests()
        return
    if args.demo_async:
        asyncio.run(demo_async())
        return
    if args.backoff is not None:
        for i in range(args.backoff):
            print(f"attempt {i}: {backoff_delay(i):.2f}s")
        return

    j = DelayJitter(min_delay=args.min_delay, max_delay=args.max_delay, mode=args.mode)
    for i in range(args.number):
        if args.sleep:
            d = j.sleep()
            print(f"[{i+1}] attendu {d:.3f}s")
        else:
            print(f"[{i+1}] {j.get():.3f}s")


if __name__ == "__main__":
    main()
