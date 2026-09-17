from __future__ import annotations
import threading
import time
import random
import logging
from itertools import cycle
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

TRANSIENT_HTTP = {403, 407, 429, 502, 503, 504}
PERMANENT_HTTP = {511}


class ProxyRotator:
    def __init__(self,
                 proxies: Optional[List[str]] = None,
                 proxy_file: Optional[str] = None,
                 rotation: str = "round-robin",
                 bad_ttl: float = 300.0,
                 timeout: float = 10.0):
        self.proxies: List[str] = []
        if proxies:
            self.proxies.extend(proxies)
        if proxy_file:
            self.load_from_file(proxy_file)
        if not self.proxies:
            raise ValueError("Aucun proxy fourni.")

        self.rotation = rotation.lower()
        self.bad_ttl = bad_ttl
        self.timeout = timeout

        self._cycle = cycle(self.proxies)
        self._lock = threading.Lock()

        # proxy_url -> timestamp du marquage KO
        self._bad_until: Dict[str, float] = {}
        # bans permanents
        self._permanent_bad: set = set()
        self.stats: Dict[str, Dict[str, int]] = {
            p: {"ok": 0, "ko": 0} for p in self.proxies
        }

    # ------------------------------------------------------------------
    # Chargement
    # ------------------------------------------------------------------
    def load_from_file(self, filepath: str) -> None:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    self.proxies.append(line)

    # ------------------------------------------------------------------
    # État interne
    # ------------------------------------------------------------------
    def _is_available(self, proxy: str) -> bool:
        if proxy in self._permanent_bad:
            return False
        until = self._bad_until.get(proxy)
        if until is None:
            return True
        if time.time() >= until:
            # Réhabilitation : on nettoie l'entrée
            self._bad_until.pop(proxy, None)
            return True
        return False

    def _available_list(self) -> List[str]:
        return [p for p in self.proxies if self._is_available(p)]

    # ------------------------------------------------------------------
    # Sélection
    # ------------------------------------------------------------------
    def get_proxy(self) -> Optional[Dict[str, str]]:
        """Retourne un dict {'http':..., 'https':...} ou None si aucun dispo."""
        with self._lock:
            available = self._available_list()
            if not available:
                return None
            if self.rotation == "random":
                proxy = random.choice(available)
            else:
                proxy = None
                for _ in range(len(self.proxies)):
                    candidate = next(self._cycle)
                    if self._is_available(candidate):
                        proxy = candidate
                        break
                if proxy is None:
                    proxy = available[0]
        return {"http": proxy, "https": proxy}

    # ------------------------------------------------------------------
    # Marquage
    # ------------------------------------------------------------------
    def mark_bad(self, proxy_url: str,
                 permanent: bool = False,
                 reason: str = "") -> None:
        with self._lock:
            self.stats.setdefault(proxy_url, {"ok": 0, "ko": 0})["ko"] += 1
            if permanent:
                self._permanent_bad.add(proxy_url)
                logger.warning("Proxy BANNI définitivement : %s (%s)",
                               proxy_url, reason or "n/a")
            else:
                self._bad_until[proxy_url] = time.time() + self.bad_ttl
                logger.info("Proxy KO temporaire (%ss) : %s (%s)",
                            int(self.bad_ttl), proxy_url, reason or "n/a")

    def mark_good(self, proxy_url: str) -> None:
        with self._lock:
            self.stats.setdefault(proxy_url, {"ok": 0, "ko": 0})["ok"] += 1

    def reset_bad(self) -> None:
        """Réhabilite tous les proxies (hors bans permanents)."""
        with self._lock:
            self._bad_until.clear()

    # ------------------------------------------------------------------
    # Requête avec rotation + retry
    # ------------------------------------------------------------------
    def request(self, method: str, url: str,
                max_retries: int = 3,
                timeout: Optional[float] = None,
                **kwargs):
        """
        Effectue une requête en tournant sur les proxies.
        Le timeout est fixé UNE FOIS (ne fuit plus entre tentatives).
        """
        try:
            import requests
        except ImportError:
            raise RuntimeError("requests requis")

        to = timeout if timeout is not None else self.timeout

        for attempt in range(max_retries):
            proxy_dict = self.get_proxy()
            if not proxy_dict:
                logger.error("Plus de proxies disponibles.")
                return None
            proxy_url = proxy_dict["http"]

            try:
                response = requests.request(
                    method, url,
                    proxies=proxy_dict,
                    timeout=to,
                    **kwargs,
                )
                if response.status_code in PERMANENT_HTTP:
                    self.mark_bad(proxy_url, permanent=True,
                                  reason=f"HTTP {response.status_code}")
                    continue
                if response.status_code in TRANSIENT_HTTP:
                    self.mark_bad(proxy_url, reason=f"HTTP {response.status_code}")
                    continue
                self.mark_good(proxy_url)
                return response

            except Exception as e:
                self.mark_bad(proxy_url, reason=type(e).__name__)
                # backoff minimal entre tentatives, sans jitter ici
                time.sleep(min(2 ** attempt, 5))

        return None

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------
    def test_proxy(self, proxy_url: str,
                   test_url: str = "http://httpbin.org/ip",
                   timeout: float = 5.0) -> bool:
        try:
            import requests
            r = requests.get(
                test_url,
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=timeout,
            )
            return r.status_code == 200
        except Exception:
            return False

    def stats_snapshot(self) -> Dict[str, Dict[str, int]]:
        with self._lock:
            return {k: dict(v) for k, v in self.stats.items()}


if __name__ == "__main__":
    # Démo sans cible externe : nécessite une liste de proxies en argument
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--proxies-file", required=True)
    p.add_argument("--url", default="http://httpbin.org/ip")
    args = p.parse_args()

    rot = ProxyRotator(proxy_file=args.proxies_file, rotation="random")
    resp = rot.request("GET", args.url)
    print(resp.json() if resp else "Échec")