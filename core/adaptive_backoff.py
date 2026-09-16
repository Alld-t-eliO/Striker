"""
adaptive_backoff.py
-------------------
Backoff adaptatif pour éviter la détection lors du scraping.

Contrairement au backoff exponentiel fixe, ce module APPREND du serveur :
- Lit Retry-After (429 / 503)
- Observe la latence des réponses (signe de throttling silencieux)
- Mesure le taux d'erreur glissant par domaine
- Ajuste le délai selon plusieurs algorithmes : AIMD, PID, adaptatif simple
- Détecte les réponses "pièges" (captcha, page de blocage)
- Circuit breaker par domaine (coupe si trop d'erreurs)
- Compatible avec ProxyRotator, UserAgentRotator, DelayJitter, RequestFragmenter

Usage typique :
    backoff = AdaptiveBackoff()
    delay = backoff.on_success("api.exemple.com", latency=0.8)
    time.sleep(delay)

    delay = backoff.on_failure("api.exemple.com", status=429, retry_after=30)
    time.sleep(delay)
"""

import time
import random
import math
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, List
from urllib.parse import urlparse
from collections import deque


# ---------------------------------------------------------------------------
# État par domaine
# ---------------------------------------------------------------------------
@dataclass
class DomainState:
    """État adaptatif d'un domaine (host)."""
    delay: float = 1.0                            # Délai courant recommandé (s)
    base_delay: float = 1.0                       # Délai de base
    min_delay: float = 0.2                        # Plancher
    max_delay: float = 120.0                      # Plafond
    success_streak: int = 0                       # Succès consécutifs
    failure_streak: int = 0                       # Échecs consécutifs
    total_requests: int = 0
    total_failures: int = 0
    latencies: deque = field(default_factory=lambda: deque(maxlen=50))
    error_timestamps: deque = field(default_factory=lambda: deque(maxlen=100))
    circuit_open: bool = False                    # Circuit breaker
    circuit_opened_at: float = 0.0
    last_update: float = field(default_factory=time.time)
    last_retry_after: Optional[float] = None
    # PID
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
        """Taux d'erreur sur les `window` dernières secondes."""
        now = time.time()
        recent = [t for t in self.error_timestamps if now - t < window]
        return len(recent) / max(1, window)


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------
class AdaptiveBackoff:
    """
    Backoff adaptatif multi-algorithmes pour éviter la détection.
    """

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
        """
        :param strategy: 'aimd' | 'pid' | 'simple' | 'adaptive'
        :param base_delay: Délai de départ
        :param min_delay: Plancher (jamais en dessous)
        :param max_delay: Plafond (jamais au-dessus)
        :param jitter: 'full' | 'equal' | 'decorrelated' | 'none'
        :param circuit_breaker_threshold: Nb d'échecs avant ouverture du circuit
        :param circuit_cooldown: Temps (s) avant de réessayer un domaine bloqué
        :param suspect_patterns: Sous-chaînes indiquant une page de blocage
        :param verbose: Affiche les décisions
        """
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
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Accès à l'état d'un domaine
    # ------------------------------------------------------------------
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
        """Extrait le host d'une URL ou le retourne tel quel."""
        if "://" in url:
            return urlparse(url).netloc
        return url

    # ------------------------------------------------------------------
    # Application du jitter (cohérent avec delay_jitter.py)
    # ------------------------------------------------------------------
    def _apply_jitter(self, delay: float) -> float:
        if self.jitter == "full":
            return random.uniform(0, delay)
        elif self.jitter == "equal":
            return delay / 2 + random.uniform(0, delay / 2)
        elif self.jitter == "decorrelated":
            return random.uniform(self.min_delay, delay)
        elif self.jitter == "none":
            return delay
        return delay

    # ------------------------------------------------------------------
    # Signaux d'entrée : succès / échec
    # ------------------------------------------------------------------
    def on_success(self,
                   url_or_host: str,
                   latency: Optional[float] = None,
                   status: int = 200,
                   body: Optional[str] = None) -> float:
        """
        À appeler après une requête réussie.
        Retourne le délai (avec jitter) à attendre avant la prochaine requête.
        """
        host = self.host_of(url_or_host)
        with self._lock:
            st = self._state(host)
            st.total_requests += 1
            st.success_streak += 1
            st.failure_streak = 0
            if latency is not None:
                st.latencies.append(latency)

            # Détection de page "piège" même avec status 200
            if body and self._is_suspect(body):
                if self.verbose:
                    print(f"[backoff] {host}: page suspecte détectée malgré 200")
                st.total_failures += 1
                st.error_timestamps.append(time.time())
                return self._escalate(st, reason="suspect_page")

            # Latence anormalement élevée = throttling silencieux probable
            if latency is not None and st.avg_latency > 0 and latency > st.avg_latency * 2.5:
                if self.verbose:
                    print(f"[backoff] {host}: latence anormale ({latency:.2f}s vs {st.avg_latency:.2f}s)")
                # On ralentit un peu mais sans escalade violente
                st.delay = min(st.max_delay, st.delay * 1.3)

            # Succès : on réduit le délai selon la stratégie
            self._decrease(st)
            st.last_update = time.time()
            delay = max(st.min_delay, min(st.max_delay, st.delay))
            if self.verbose:
                print(f"[backoff] {host}: OK -> delay={delay:.2f}s (strategy={self.strategy})")
            return self._apply_jitter(delay)

    def on_failure(self,
                   url_or_host: str,
                   status: Optional[int] = None,
                   retry_after: Optional[float] = None,
                   exception: Optional[Exception] = None) -> float:
        """
        À appeler après un échec (429, 503, timeout, etc.).
        Retourne le délai à attendre avant de réessayer.
        """
        host = self.host_of(url_or_host)
        with self._lock:
            st = self._state(host)
            st.total_requests += 1
            st.total_failures += 1
            st.failure_streak += 1
            st.success_streak = 0
            st.error_timestamps.append(time.time())
            st.last_update = time.time()

            # 1) Retry-After prioritaire (le serveur dit exactement quoi faire)
            if retry_after is not None and retry_after > 0:
                st.last_retry_after = retry_after
                st.delay = max(st.delay, retry_after)
                if self.verbose:
                    print(f"[backoff] {host}: Retry-After={retry_after}s respecté")
                return self._apply_jitter(min(st.max_delay, retry_after))

            # 2) Codes d'erreur spécifiques
            if status in (429, 503):
                # Rate limit explicite : on escalade fortement
                st.delay = min(st.max_delay, st.delay * 2.0 + 1.0)
            elif status in (403, 401):
                # Blocage : on multiplie fortement
                st.delay = min(st.max_delay, st.delay * 3.0)
            elif status is not None and 500 <= status < 600:
                st.delay = min(st.max_delay, st.delay * 1.5)
            elif exception is not None:
                # Timeout, connexion refusée, etc.
                st.delay = min(st.max_delay, st.delay * 1.8)

            # 3) Circuit breaker
            if st.failure_streak >= self.circuit_threshold:
                st.circuit_open = True
                st.circuit_opened_at = time.time()
                st.delay = st.max_delay
                if self.verbose:
                    print(f"[backoff] {host}: CIRCUIT OUVERT ({st.failure_streak} échecs)")

            delay = max(st.min_delay, min(st.max_delay, st.delay))
            if self.verbose:
                print(f"[backoff] {host}: ÉCHEC status={status} -> delay={delay:.2f}s")
            return self._apply_jitter(delay)

    # ------------------------------------------------------------------
    # Stratégies de diminution du délai (après succès)
    # ------------------------------------------------------------------
    def _decrease(self, st: DomainState) -> None:
        if self.strategy == "aimd":
            # Additive Increase Multiplicative Decrease (TCP-like)
            # Ici : on augmente le "débit" (donc on DIMINUE le délai) additivement.
            # On attend plusieurs succès consécutifs avant de réduire.
            if st.success_streak >= 3:
                st.delay = max(st.min_delay, st.delay - 0.1)
                st.success_streak = 0

        elif self.strategy == "simple":
            # Réduction proportionnelle simple
            st.delay = max(st.min_delay, st.delay * 0.9)

        elif self.strategy == "pid":
            # Contrôleur PID : consigne = taux d'erreur cible 5%
            target_error = 0.05
            error = st.recent_error_rate() - target_error
            st.integral += error
            derivative = error - st.previous_error
            st.previous_error = error
            correction = (self.pid_kp * error +
                          self.pid_ki * st.integral +
                          self.pid_kd * derivative)
            st.delay = max(st.min_delay, min(st.max_delay, st.delay + correction))

        elif self.strategy == "adaptive":
            # Réduction si le taux d'erreur récent est nul et succès consécutifs
            if st.success_streak >= 5 and st.recent_error_rate() < 0.01:
                st.delay = max(st.min_delay, st.delay * 0.85)
                st.success_streak = 0

    def _escalate(self, st: DomainState, reason: str = "") -> float:
        st.delay = min(st.max_delay, st.delay * 2.0)
        if st.failure_streak >= self.circuit_threshold:
            st.circuit_open = True
            st.circuit_opened_at = time.time()
        delay = max(st.min_delay, min(st.max_delay, st.delay))
        return self._apply_jitter(delay)

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------
    def is_available(self, url_or_host: str) -> bool:
        """
        Vérifie si le domaine est disponible (circuit fermé).
        Si le cooldown est passé, referme le circuit automatiquement.
        """
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
                    print(f"[backoff] {host}: circuit refermé (cooldown écoulé)")
                return True
            return False

    def wait_if_blocked(self, url_or_host: str) -> float:
        """
        Bloque jusqu'à ce que le domaine soit à nouveau disponible.
        Retourne le temps réellement attendu.
        """
        host = self.host_of(url_or_host)
        waited = 0.0
        while not self.is_available(host):
            remaining = self.circuit_cooldown - (time.time() - self._state(host).circuit_opened_at)
            if remaining <= 0:
                break
            sleep_time = min(remaining, 5.0)
            if self.verbose:
                print(f"[backoff] {host}: bloqué, attente {sleep_time:.1f}s")
            time.sleep(sleep_time)
            waited += sleep_time
        return waited

    # ------------------------------------------------------------------
    # Détection de pages "pièges"
    # ------------------------------------------------------------------
    def _is_suspect(self, body: str) -> bool:
        body_lower = body.lower()
        return any(p in body_lower for p in self.suspect_patterns)

    # ------------------------------------------------------------------
    # Extraction de Retry-After depuis une réponse requests
    # ------------------------------------------------------------------
    @staticmethod
    def parse_retry_after(response) -> Optional[float]:
        """
        Extrait Retry-After depuis une réponse requests (entier en secondes
        ou date HTTP). Retourne None si absent / invalide.
        """
        header = response.headers.get("Retry-After")
        if not header:
            return None
        try:
            return float(header)
        except ValueError:
            # Format date HTTP : "Wed, 21 Oct 2025 07:28:00 GMT"
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(header)
                delta = dt.timestamp() - time.time()
                return max(0.0, delta)
            except Exception:
                return None

    # ------------------------------------------------------------------
    # API haut niveau : wrapper pour requests
    # ------------------------------------------------------------------
    def request(self,
                session,
                method: str,
                url: str,
                max_retries: int = 5,
                **kwargs):
        """
        Effectue une requête avec backoff adaptatif automatique.
        `session` : objet requests.Session (ou requests lui-même).

        Exemple :
            import requests
            backoff = AdaptiveBackoff(strategy="adaptive", verbose=True)
            resp = backoff.request(requests.Session(), "GET", "https://exemple.com")
        """
        host = self.host_of(url)
        last_exc = None

        for attempt in range(max_retries):
            # Attendre si le circuit est ouvert
            self.wait_if_blocked(host)

            t0 = time.time()
            try:
                resp = session.request(method, url, **kwargs)
                latency = time.time() - t0

                if resp.status_code in (200, 201, 202, 204):
                    delay = self.on_success(host, latency=latency,
                                            status=resp.status_code,
                                            body=resp.text[:5000] if resp.text else None)
                    time.sleep(delay)
                    return resp

                if resp.status_code in (429, 503):
                    retry_after = self.parse_retry_after(resp)
                    delay = self.on_failure(host, status=resp.status_code,
                                            retry_after=retry_after)
                    time.sleep(delay)
                    continue

                if resp.status_code in (403, 401):
                    delay = self.on_failure(host, status=resp.status_code)
                    time.sleep(delay)
                    continue

                if 500 <= resp.status_code < 600:
                    delay = self.on_failure(host, status=resp.status_code)
                    time.sleep(delay)
                    continue

                # Autres codes : considérés comme succès (404, 400, etc.)
                delay = self.on_success(host, latency=latency, status=resp.status_code)
                time.sleep(delay)
                return resp

            except Exception as e:
                last_exc = e
                delay = self.on_failure(host, exception=e)
                if self.verbose:
                    print(f"[backoff] {host}: exception {e} -> attente {delay:.2f}s")
                time.sleep(delay)

        if self.verbose:
            print(f"[backoff] {host}: échec après {max_retries} tentatives")
        if last_exc:
            raise last_exc
        return None

    # ------------------------------------------------------------------
    # Diagnostics / stats
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Dict]:
        """Retourne un snapshot des états par domaine."""
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
        """Réinitialise l'état d'un domaine (ou tout)."""
        with self._lock:
            if url_or_host is None:
                self._states.clear()
            else:
                self._states.pop(self.host_of(url_or_host), None)


# ---------------------------------------------------------------------------
# Décorateur utilitaire
# ---------------------------------------------------------------------------
def adaptive_backoff(backoff: AdaptiveBackoff, session=None):
    """
    Décorateur : applique le backoff adaptatif à une fonction qui prend (method, url, **kwargs)
    et retourne une réponse requests.
    """
    def decorator(func):
        def wrapper(method: str, url: str, **kwargs):
            sess = session
            if sess is None:
                import requests
                sess = requests.Session()
            return backoff.request(sess, method, url, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Démo
# ---------------------------------------------------------------------------
def demo():
    print("=== Démo 1 : simulation de réponses serveur ===")
    bo = AdaptiveBackoff(strategy="aimd", verbose=True)

    host = "api.exemple.com"

    print("\n-- 5 succès consécutifs --")
    for _ in range(5):
        d = bo.on_success(host, latency=0.4)
        print(f"   délai appliqué : {d:.3f}s")

    print("\n-- Burst de 429 --")
    for _ in range(4):
        d = bo.on_failure(host, status=429)
        print(f"   délai appliqué : {d:.3f}s")

    print("\n-- Retry-After imposé --")
    d = bo.on_failure(host, status=429, retry_after=15.0)
    print(f"   délai appliqué : {d:.3f}s")

    print("\n-- Déclenchement du circuit breaker --")
    for _ in range(6):
        d = bo.on_failure(host, status=503)
        print(f"   délai appliqué : {d:.3f}s (dispo={bo.is_available(host)})")

    print("\n-- Stats --")
    for k, v in bo.stats().items():
        print(f"   {k}: {v}")

    print("\n=== Démo 2 : avec requests (si installé) ===")
    try:
        import requests
    except ImportError:
        print("requests non installé, démo HTTP sautée.")
        return

    bo2 = AdaptiveBackoff(strategy="adaptive", verbose=True)
    sess = requests.Session()
    try:
        resp = bo2.request(sess, "GET", "https://httpbin.org/get")
        print(f"   status={resp.status_code}, size={len(resp.text)}")
    except Exception as e:
        print(f"   erreur réseau : {e}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Backoff adaptatif anti-détection.")
    parser.add_argument("--strategy", choices=["aimd", "pid", "simple", "adaptive"],
                        default="aimd")
    parser.add_argument("--base-delay", type=float, default=1.0)
    parser.add_argument("--min-delay", type=float, default=0.2)
    parser.add_argument("--max-delay", type=float, default=120.0)
    parser.add_argument("--jitter", choices=["full", "equal", "decorrelated", "none"],
                        default="full")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()

    if args.demo:
        demo()
    else:
        bo = AdaptiveBackoff(
            strategy=args.strategy,
            base_delay=args.base_delay,
            min_delay=args.min_delay,
            max_delay=args.max_delay,
            jitter=args.jitter,
            verbose=args.verbose,
        )
        print(f"AdaptiveBackoff prêt (strategy={args.strategy}).")
        print("Exemple :")
        print("  d = bo.on_success('api.exemple.com', latency=0.5)")
        print("  d = bo.on_failure('api.exemple.com', status=429, retry_after=30)")
        print("  resp = bo.request(requests.Session(), 'GET', 'https://exemple.com')")
