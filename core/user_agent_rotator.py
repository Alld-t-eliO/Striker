"""
user_agent_rotator.py
---------------------
Rotation de User-Agents + headers navigateur cohérents.
"""

from __future__ import annotations

import random
from itertools import cycle
from typing import Dict, List, Optional


DEFAULT_USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
]

# Headers de base pour un GET navigateur réaliste
BROWSER_HEADERS_CHROMIUM: Dict[str, str] = {
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

BROWSER_HEADERS_FIREFOX: Dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def _detect_family(ua: str) -> str:
    u = ua.lower()
    if "firefox" in u:
        return "firefox"
    if "edg/" in u:
        return "edge"
    if "chrome" in u or "chromium" in u:
        return "chrome"
    if "safari" in u:
        return "safari"
    return "unknown"


# Mapping famille -> profil curl_cffi cohérent (TLS/JA3)
CURL_CFFI_PROFILE_BY_FAMILY = {
    "chrome": "chrome124",
    "edge": "edge101",
    "firefox": "firefox133",
    "safari": "safari17_0",
    "unknown": "chrome124",
}


class UserAgentRotator:
    """
    Rotation d'UA + headers navigateur cohérents.
    Instance-scoped RNG (n'affecte pas random.seed global).
    """

    def __init__(self,
                 user_agents: Optional[List[str]] = None,
                 ua_file: Optional[str] = None,
                 rotation: str = "random",
                 include_browser_headers: bool = True,
                 seed: Optional[int] = None):
        self._rng = random.Random(seed)

        uas: List[str] = []
        if ua_file:
            with open(ua_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        uas.append(line)
        if user_agents:
            uas.extend(user_agents)
        if not uas:
            uas = list(DEFAULT_USER_AGENTS)

        self.user_agents: List[str] = uas
        self.rotation = rotation.lower()
        self.include_browser_headers = include_browser_headers
        self._cycle = cycle(self.user_agents)
        self._current: str = self.user_agents[0]

    # ------------------------------------------------------------------
    def get(self) -> str:
        if self.rotation == "round-robin":
            self._current = next(self._cycle)
        else:
            self._current = self._rng.choice(self.user_agents)
        return self._current

    def rotate(self) -> str:
        return self.get()

    def get_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        ua = self.get()
        family = _detect_family(ua)

        headers: Dict[str, str] = {"User-Agent": ua}
        if self.include_browser_headers:
            if family == "firefox":
                headers.update(BROWSER_HEADERS_FIREFOX)
            else:
                headers.update(BROWSER_HEADERS_CHROMIUM)

        if extra:
            headers.update(extra)
        return headers

    def get_curl_cffi_profile(self) -> str:
        """Retourne le profil curl_cffi cohérent avec l'UA courant."""
        return CURL_CFFI_PROFILE_BY_FAMILY.get(_detect_family(self._current), "chrome124")

    @property
    def current(self) -> str:
        return self._current

    def add(self, ua: str) -> None:
        self.user_agents.append(ua)


if __name__ == "__main__":
    import argparse, json
    p = argparse.ArgumentParser()
    p.add_argument("-n", "--number", type=int, default=5)
    p.add_argument("--rotation", choices=["random", "round-robin"], default="random")
    p.add_argument("--headers", action="store_true")
    args = p.parse_args()

    rot = UserAgentRotator(rotation=args.rotation)
    for i in range(args.number):
        if args.headers:
            print(f"[{i+1}] " + json.dumps(rot.get_headers(), indent=2, ensure_ascii=False))
        else:
            print(f"[{i+1}] {rot.get()}")