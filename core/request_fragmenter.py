"""
request_fragmenter.py
---------------------
Fragmentation des requêtes HTTP pour le scraping furtif et les gros envois.

Fonctionnalités :
- Découpage d'un payload (dict / JSON / bytes) en chunks (chunked upload)
- Fragmentation des paramètres d'URL (?a=1&b=2 -> plusieurs requêtes)
- Pagination automatique (page/offset/cursor) avec détection de fin
- Distribution d'une liste d'URLs sur plusieurs proxies / User-Agents
- Envoi en streaming morceau par morceau (Transfer-Encoding: chunked)
- Compatible avec ProxyRotator, UserAgentRotator, DelayJitter (imports optionnels)
- Support sync (requests) et async (aiohttp)
"""

import json
import math
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from itertools import islice


# ---------------------------------------------------------------------------
# Imports optionnels des modules compagnons
# ---------------------------------------------------------------------------
try:
    from proxy_rotator import ProxyRotator  # type: ignore
except ImportError:
    ProxyRotator = None  # type: ignore

try:
    from user_agent_rotator import UserAgentRotator  # type: ignore
except ImportError:
    UserAgentRotator = None  # type: ignore

try:
    from delay_jitter import DelayJitter, RateLimiter, backoff_delay  # type: ignore
except ImportError:
    DelayJitter = None  # type: ignore
    RateLimiter = None  # type: ignore
    backoff_delay = None  # type: ignore


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------
def _chunks(seq: List[Any], size: int) -> Iterator[List[Any]]:
    """Découpe une liste en sous-listes de taille `size`."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _serialize(payload: Any) -> bytes:
    """Sérialise un payload en bytes (dict -> JSON, str -> utf-8, bytes -> tel quel)."""
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------
class RequestFragmenter:
    """
    Fragmente des requêtes HTTP de différentes manières.
    """

    def __init__(self,
                 chunk_size: int = 1024 * 64,      # 64 Ko par défaut
                 max_params_per_request: int = 5,
                 max_urls_per_batch: int = 10,
                 proxy_rotator: Optional[Any] = None,
                 ua_rotator: Optional[Any] = None,
                 jitter: Optional[Any] = None,
                 rate_limiter: Optional[Any] = None,
                 max_retries: int = 3):
        """
        :param chunk_size: Taille des morceaux pour le chunked upload (bytes)
        :param max_params_per_request: Nombre de paramètres par requête fragmentée
        :param max_urls_per_batch: Nombre d'URLs par lot distribué
        :param proxy_rotator: Instance de ProxyRotator (optionnel)
        :param ua_rotator: Instance de UserAgentRotator (optionnel)
        :param jitter: Instance de DelayJitter (optionnel)
        :param rate_limiter: Instance de RateLimiter (optionnel)
        :param max_retries: Nombre de tentatives en cas d'échec
        """
        self.chunk_size = chunk_size
        self.max_params_per_request = max_params_per_request
        self.max_urls_per_batch = max_urls_per_batch
        self.proxy_rotator = proxy_rotator
        self.ua_rotator = ua_rotator
        self.jitter = jitter
        self.rate_limiter = rate_limiter
        self.max_retries = max_retries

    # ------------------------------------------------------------------
    # 1) Fragmentation d'un payload en chunks (chunked upload)
    # ------------------------------------------------------------------
    def fragment_payload(self, payload: Any) -> List[bytes]:
        """
        Découpe un payload (dict / JSON / bytes / str) en morceaux de `chunk_size` octets.
        Retourne une liste de chunks (bytes).
        """
        data = _serialize(payload)
        return [data[i:i + self.chunk_size] for i in range(0, len(data), self.chunk_size)]

    def upload_in_chunks(self,
                         url: str,
                         payload: Any,
                         method: str = "POST",
                         extra_headers: Optional[Dict[str, str]] = None) -> List[Any]:
        """
        Envoie un gros payload en plusieurs requêtes successives (chunked upload applicatif).
        Chaque chunk est envoyé à `url` avec les en-têtes X-Chunk-Index / X-Chunk-Total.
        Retourne la liste des réponses.
        """
        import requests

        chunks = self.fragment_payload(payload)
        total = len(chunks)
        responses = []

        for i, chunk in enumerate(chunks):
            headers = self._build_headers(extra_headers)
            headers.update({
                "Content-Type": "application/octet-stream",
                "X-Chunk-Index": str(i),
                "X-Chunk-Total": str(total),
                "Content-Length": str(len(chunk)),
            })
            resp = self._request_with_retry(
                requests, method, url, data=chunk, headers=headers
            )
            responses.append(resp)
        return responses

    def stream_in_chunks(self,
                         url: str,
                         payload: Any,
                         method: str = "POST",
                         extra_headers: Optional[Dict[str, str]] = None) -> Any:
        """
        Envoie un payload en utilisant Transfer-Encoding: chunked (vrai streaming HTTP).
        `requests` gère automatiquement le chunked si on lui passe un générateur.
        """
        import requests

        chunks = self.fragment_payload(payload)
        headers = self._build_headers(extra_headers)
        headers["Content-Type"] = "application/octet-stream"

        def gen():
            for c in chunks:
                yield c

        return self._request_with_retry(
            requests, method, url, data=gen(), headers=headers
        )

    # ------------------------------------------------------------------
    # 2) Fragmentation des paramètres d'URL
    # ------------------------------------------------------------------
    def fragment_params(self,
                        url: str,
                        params: Dict[str, Any],
                        method: str = "GET",
                        extra_headers: Optional[Dict[str, str]] = None) -> List[Any]:
        """
        Découpe un dictionnaire de paramètres en plusieurs requêtes.
        Exemple : {'a':1,'b':2,'c':3} avec max=2 -> 2 requêtes.
        """
        import requests

        items = list(params.items())
        batches = list(_chunks(items, self.max_params_per_request))
        responses = []

        for batch in batches:
            batch_params = dict(batch)
            headers = self._build_headers(extra_headers)
            resp = self._request_with_retry(
                requests, method, url, params=batch_params, headers=headers
            )
            responses.append(resp)
        return responses

    # ------------------------------------------------------------------
    # 3) Pagination automatique
    # ------------------------------------------------------------------
    def paginate(self,
                 url: str,
                 page_param: str = "page",
                 start: int = 1,
                 max_pages: int = 100,
                 is_last: Optional[Callable[[Any], bool]] = None,
                 method: str = "GET",
                 extra_headers: Optional[Dict[str, str]] = None,
                 extra_params: Optional[Dict[str, Any]] = None) -> List[Any]:
        """
        Enchaîne des requêtes paginées jusqu'à détecter la dernière page.

        :param page_param: Nom du paramètre de page (`page`, `offset`, `p`, ...)
        :param start: Numéro de la première page
        :param max_pages: Nombre maximum de pages (sécurité)
        :param is_last: Fonction qui prend la réponse et retourne True si dernière page.
                        Par défaut : détecte une liste vide / page vide / plus de résultats.
        :return: Liste des réponses HTTP
        """
        import requests

        responses = []
        page = start

        while page < start + max_pages:
            params = dict(extra_params or {})
            params[page_param] = page
            headers = self._build_headers(extra_headers)

            resp = self._request_with_retry(
                requests, method, url, params=params, headers=headers
            )
            if resp is None:
                break

            responses.append(resp)

            if is_last and is_last(resp):
                break
            # Détection automatique de fin
            if not is_last and self._looks_empty(resp):
                break
            if resp.status_code in (404, 410):
                break

            page += 1

        return responses

    # ------------------------------------------------------------------
    # 4) Distribution d'URLs sur plusieurs proxies / UAs
    # ------------------------------------------------------------------
    def distribute(self,
                   urls: List[str],
                   method: str = "GET",
                   extra_headers: Optional[Dict[str, str]] = None,
                   chunk_size: Optional[int] = None) -> List[Any]:
        """
        Distribue une liste d'URLs sur plusieurs proxies / User-Agents.
        Chaque lot (batch) utilise un proxy et un UA différents.
        """
        import requests

        batch_size = chunk_size or self.max_urls_per_batch
        responses = []

        for batch in _chunks(urls, batch_size):
            headers = self._build_headers(extra_headers)
            proxies = self._get_proxies()
            for url in batch:
                resp = self._request_with_retry(
                    requests, method, url, headers=headers, proxies=proxies
                )
                responses.append(resp)
        return responses

    # ------------------------------------------------------------------
    # 5) Fragmentation générique via un itérateur
    # ------------------------------------------------------------------
    @staticmethod
    def fragment_iterable(iterable: Iterable, size: int) -> Iterator[List[Any]]:
        """Découpe n'importe quel itérable en morceaux de `size`."""
        it = iter(iterable)
        while True:
            chunk = list(islice(it, size))
            if not chunk:
                return
            yield chunk

    # ------------------------------------------------------------------
    # 6) Version asynchrone (aiohttp) — payload chunké + distribution
    # ------------------------------------------------------------------
    async def upload_in_chunks_async(self,
                                     url: str,
                                     payload: Any,
                                     method: str = "POST",
                                     extra_headers: Optional[Dict[str, str]] = None) -> List[Any]:
        """
        Version async de upload_in_chunks (nécessite aiohttp).
        """
        try:
            import aiohttp
        except ImportError:
            raise ImportError("aiohttp requis : pip install aiohttp")

        chunks = self.fragment_payload(payload)
        total = len(chunks)
        results = []

        async with aiohttp.ClientSession() as session:
            for i, chunk in enumerate(chunks):
                headers = self._build_headers(extra_headers)
                headers.update({
                    "Content-Type": "application/octet-stream",
                    "X-Chunk-Index": str(i),
                    "X-Chunk-Total": str(total),
                })
                try:
                    async with session.request(method, url, data=chunk, headers=headers) as resp:
                        body = await resp.read()
                        results.append({"status": resp.status, "body": body})
                except Exception as e:
                    results.append({"status": None, "error": str(e)})
        return results

    # ------------------------------------------------------------------
    # Méthodes internes
    # ------------------------------------------------------------------
    def _build_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Construit les en-têtes, en y ajoutant un UA aléatoire si dispo."""
        headers: Dict[str, str] = {}
        if self.ua_rotator is not None:
            headers = self.ua_rotator.get_headers()
        if extra:
            headers.update(extra)
        return headers

    def _get_proxies(self) -> Optional[Dict[str, str]]:
        """Récupère un proxy aléatoire si un rotateur est fourni."""
        if self.proxy_rotator is None:
            return None
        return self.proxy_rotator.get_proxy()

    def _request_with_retry(self,
                            requests_mod,
                            method: str,
                            url: str,
                            **kwargs) -> Any:
        """
        Effectue la requête avec :
        - rate-limiting (optionnel)
        - jitter (optionnel)
        - proxy rotatif (optionnel)
        - retry avec backoff exponentiel
        """
        for attempt in range(self.max_retries):
            try:
                if self.rate_limiter is not None:
                    self.rate_limiter.wait()
                if self.jitter is not None:
                    self.jitter.sleep()
                if "proxies" not in kwargs:
                    kwargs["proxies"] = self._get_proxies()
                kwargs.setdefault("timeout", 15)

                resp = requests_mod.request(method, url, **kwargs)
                if resp.status_code in (403, 429, 503):
                    if self.proxy_rotator is not None and kwargs.get("proxies"):
                        self.proxy_rotator.mark_bad(kwargs["proxies"]["http"])
                    if backoff_delay is not None:
                        import time
                        time.sleep(backoff_delay(attempt, jitter="full"))
                    continue
                return resp
            except Exception as e:
                if attempt == self.max_retries - 1:
                    print(f"[!] Échec définitif {url} : {e}")
                    return None
                if backoff_delay is not None:
                    import time
                    time.sleep(backoff_delay(attempt, jitter="full"))
        return None

    @staticmethod
    def _looks_empty(resp) -> bool:
        """Heuristique : détecte une page sans contenu (fin de pagination)."""
        try:
            data = resp.json()
            if isinstance(data, list) and not data:
                return True
            if isinstance(data, dict):
                for key in ("results", "items", "data", "records"):
                    if key in data and isinstance(data[key], list) and not data[key]:
                        return True
                if data.get("has_more") is False or data.get("next") is None:
                    return True
        except Exception:
            # Réponse non-JSON : on regarde si le corps est très court
            if len(resp.text.strip()) < 10:
                return True
        return False


# ---------------------------------------------------------------------------
# Démo
# ---------------------------------------------------------------------------
def demo():
    from user_agent_rotator import UserAgentRotator
    from delay_jitter import DelayJitter, RateLimiter

    ua = UserAgentRotator()
    jitter = DelayJitter(min_delay=0.3, max_delay=1.2, mode="human")
    limiter = RateLimiter(max_calls=3, period=1.0)

    frag = RequestFragmenter(
        chunk_size=32,                # petits chunks pour la démo
        max_params_per_request=2,
        max_urls_per_batch=2,
        ua_rotator=ua,
        jitter=jitter,
        rate_limiter=limiter,
    )

    # 1) Fragmenter un payload
    payload = {"description": "A" * 200}
    chunks = frag.fragment_payload(payload)
    print(f"=== Payload fragmenté en {len(chunks)} morceaux ===")
    for i, c in enumerate(chunks):
        print(f"  chunk {i}: {len(c)} octets")

    # 2) Fragmenter des paramètres
    print("\n=== Fragmentation des paramètres ===")
    responses = frag.fragment_params(
        "https://httpbin.org/get",
        {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5},
    )
    for r in responses:
        if r is not None:
            print(f"  {r.status_code} -> {r.url}")

    # 3) Distribution d'URLs
    print("\n=== Distribution d'URLs ===")
    urls = [f"https://httpbin.org/anything/{i}" for i in range(6)]
    for r in frag.distribute(urls):
        if r is not None:
            print(f"  {r.status_code} -> {r.url}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fragmentation de requêtes HTTP.")
    parser.add_argument("--demo", action="store_true", help="Exécuter la démo complète")
    parser.add_argument("--chunk-size", type=int, default=1024 * 64)
    parser.add_argument("--params-per-request", type=int, default=5)
    parser.add_argument("--urls-per-batch", type=int, default=10)
    args = parser.parse_args()

    if args.demo:
        demo()
    else:
        print("Utilise --demo pour voir un exemple d'utilisation.")
        print("Ou importe RequestFragmenter dans ton code :")
        print("  from request_fragmenter import RequestFragmenter")
