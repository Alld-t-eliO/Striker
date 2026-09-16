"""
user_agent_rotator.py
---------------------
Rotation de User-Agents réalistes + headers navigateur cohérents.

API :
    ua = UserAgentRotator()
    headers = ua.get_headers()          # dict {"User-Agent": ..., "Accept": ...}
    ua_str = ua.get()                   # juste la chaîne UA
    ua.rotate()                         # force le passage au suivant
"""

import random
from itertools import cycle
from typing import Dict, List, Optional


# Liste de base — navigateurs récents (mise à jour 2024/2025)
DEFAULT_USER_AGENTS: List[str] = [
    # Chrome / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome / macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Mobile
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
]

# Headers "navigateur" par défaut, cohérents avec un vrai GET
DEFAULT_BROWSER_HEADERS: Dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


class UserAgentRotator:
    """
    Rotation de User-Agents avec stratégies : random | round-robin | weighted.
    Fournit aussi les headers navigateur cohérents.
    """

    def __init__(self,
                 user_agents: Optional[List[str]] = None,
                 rotation: str = "random",
                 include_browser_headers: bool = True,
                 seed: Optional[int] = None):
        """
        :param user_agents: Liste custom (sinon liste par défaut)
        :param rotation: 'random' | 'round-robin'
        :param include_browser_headers: Ajoute Accept, Accept-Language, etc.
        :param seed: Graine aléatoire
        """
        if seed is not None:
            random.seed(seed)

        self.user_agents: List[str] = list(user_agents) if user_agents else list(DEFAULT_USER_AGENTS)
        if not self.user_agents:
            raise ValueError("Aucun User-Agent fourni.")

        self.rotation = rotation.lower()
        self.include_browser_headers = include_browser_headers
        self._cycle = cycle(self.user_agents)
        self._current: str = self.user_agents[0]

    # ---------------------------------------------------------------
    # API publique
    # ---------------------------------------------------------------
    def get(self) -> str:
        """Retourne un User-Agent selon la stratégie."""
        if self.rotation == "round-robin":
            self._current = next(self._cycle)
        else:
            self._current = random.choice(self.user_agents)
        return self._current

    def rotate(self) -> str:
        """Force le passage au suivant (round-robin) ou tire un nouveau (random)."""
        return self.get()

    def get_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """
        Retourne un dict complet de headers navigateur avec un UA rotaté.
        Compatible avec requests / curl_cffi / playwright.
        """
        ua = self.get()
        headers: Dict[str, str] = {"User-Agent": ua}
        if self.include_browser_headers:
            headers.update(DEFAULT_BROWSER_HEADERS)
        # Ajoute quelques indices de cohérence (chrome vs firefox)
        if "Firefox" in ua:
            headers.pop("Sec-Fetch-Dest", None)
            headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        if extra:
            headers.update(extra)
        return headers

    @property
    def current(self) -> str:
        return self._current

    def add(self, ua: str) -> None:
        """Ajoute un UA à la liste."""
        self.user_agents.append(ua)


# ---------------------------------------------------------------------------
# CLI / démo
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Rotateur de User-Agents.")
    parser.add_argument("-n", "--number", type=int, default=5)
    parser.add_argument("--rotation", choices=["random", "round-robin"], default="random")
    parser.add_argument("--headers", action="store_true", help="Afficher les headers complets")
    args = parser.parse_args()

    rot = UserAgentRotator(rotation=args.rotation)
    for i in range(args.number):
        if args.headers:
            print(f"[{i+1}] " + json.dumps(rot.get_headers(), indent=2, ensure_ascii=False))
        else:
            print(f"[{i+1}] {rot.get()}")
