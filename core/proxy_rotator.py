"""
proxy_rotator.py
----------------
Rotation de proxies avec marquage automatique des proxies défectueux.
"""

import requests
import random
import time
from itertools import cycle
from typing import List, Optional, Dict


class ProxyRotator:
    def __init__(self,
                 proxies: Optional[List[str]] = None,
                 proxy_file: Optional[str] = None,
                 rotation: str = "round-robin"):
        self.proxies: List[str] = []
        if proxies:
            self.proxies.extend(proxies)
        if proxy_file:
            self.load_from_file(proxy_file)
        if not self.proxies:
            raise ValueError("Aucun proxy fourni.")

        self.rotation = rotation
        self.cycle = cycle(self.proxies)
        self.bad_proxies = set()
        self.stats: Dict[str, Dict[str, int]] = {p: {"ok": 0, "ko": 0} for p in self.proxies}

    def load_from_file(self, filepath: str) -> None:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    self.proxies.append(line)

    def get_proxy(self) -> Optional[Dict[str, str]]:
        if not self.proxies:
            return None
        available = [p for p in self.proxies if p not in self.bad_proxies]
        if not available:
            return None
        if self.rotation == "random":
            proxy = random.choice(available)
        else:
            # round-robin en ignorant les bad
            for _ in range(len(self.proxies)):
                proxy = next(self.cycle)
                if proxy not in self.bad_proxies:
                    break
        return {"http": proxy, "https": proxy}

    def mark_bad(self, proxy_url: str) -> None:
        self.bad_proxies.add(proxy_url)
        self.stats.setdefault(proxy_url, {"ok": 0, "ko": 0})["ko"] += 1
        print(f"[!] Proxy marqué KO : {proxy_url}")

    def mark_good(self, proxy_url: str) -> None:
        self.stats.setdefault(proxy_url, {"ok": 0, "ko": 0})["ok"] += 1

    def test_proxy(self, proxy_url: str,
                   test_url: str = "http://httpbin.org/ip",
                   timeout: int = 5) -> bool:
        try:
            r = requests.get(test_url,
                             proxies={"http": proxy_url, "https": proxy_url},
                             timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    def request(self, method: str, url: str,
                max_retries: int = 3, **kwargs) -> Optional[requests.Response]:
        for attempt in range(max_retries):
            proxy_dict = self.get_proxy()
            if not proxy_dict:
                print("[-] Plus de proxies disponibles.")
                return None
            proxy_url = proxy_dict["http"]
            try:
                response = requests.request(method, url, proxies=proxy_dict,
                                            timeout=kwargs.pop("timeout", 10), **kwargs)
                if response.status_code in (403, 429, 503):
                    self.mark_bad(proxy_url)
                    continue
                self.mark_good(proxy_url)
                return response
            except Exception as e:
                print(f"[!] Erreur proxy {proxy_url} : {e}")
                self.mark_bad(proxy_url)
                time.sleep(1)
        return None


if __name__ == "__main__":
    proxies_list = [
        # 'http://user:pass@ip1:port1',
        # 'socks5://ip2:port2',
    ]
    if not proxies_list:
        print("Renseigne proxies_list ou utilise proxy_file='proxies.txt'.")
        raise SystemExit(0)
    rotator = ProxyRotator(proxies=proxies_list, rotation="random")
    resp = rotator.request("GET", "https://httpbin.org/ip")
    print(resp.json() if resp else "Échec")
