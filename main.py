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
import core.ddos as ddos

logger = logging.getLogger("scraping_stack")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

class ScrapingStack:
    def __init__(self,
                 # 
                 proxies: Optional[List[str]] = None,
                 proxy_file: Optional[str] = None,
                 proxy_rotation: str = "round-robin",
                 user_agents: Optional[List[str]] = None,
                 ua_rotation: str = "random",
                 min_delay: float = 0.4,
                 max_delay: float = 2.5,
                 jitter_mode: str = "human",
                 rate_max_calls: int = 5,
                 rate_period: float = 1.0,
                 backoff_strategy: str = "adaptive",
                 backoff_max_delay: float = 120.0,
                 captcha_service: str = "capsolver",
                 captcha_api_key: Optional[str] = None,
                 use_tls_impersonation: bool = True,
                 use_browser_fallback: bool = True,
                 headless: bool = True,
                 chunk_size: int = 64 * 1024,
                 params_per_request: int = 5,
                 verbose: bool = False):
        self.verbose = verbose
        if verbose:
            logger.setLevel(logging.DEBUG)

        if proxies or proxy_file:
            self.proxy_rotator: Optional[ProxyRotator] = ProxyRotator(
                proxies=proxies,
                proxy_file=proxy_file,
                rotation=proxy_rotation,
            )
        else:
            self.proxy_rotator = None

        self.ua_rotator = UserAgentRotator(
            user_agents=user_agents,
            rotation=ua_rotation,
        )

        self.jitter = DelayJitter(min_delay=min_delay,
                                  max_delay=max_delay,
                                  mode=jitter_mode)
        self.rate_limiter = RateLimiter(max_calls=rate_max_calls,
                                        period=rate_period)

        self.backoff = AdaptiveBackoff(
            strategy=backoff_strategy,
            max_delay=backoff_max_delay,
            verbose=verbose,
        )

        self.fragmenter = RequestFragmenter(
            chunk_size=chunk_size,
            max_params_per_request=params_per_request,
            proxy_rotator=self.proxy_rotator,
            ua_rotator=self.ua_rotator,
            jitter=self.jitter,
            rate_limiter=self.rate_limiter,
        )

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

    def fetch(self, method: str, url: str, **kwargs) -> Any:
        return self.bypass.request(method, url, **kwargs)

    def get(self, url: str, **kwargs):
        return self.fetch("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.fetch("POST", url, **kwargs)

    def fetch_many(self,
                   urls: Iterable[str],
                   workers: int = 4,
                   handler: Optional[Any] = None,
                   mode: str = "thread",
                   shard_by: str = "host",
                   on_result: Optional[Any] = None,
                   on_error: Optional[Any] = None,
                   progress_every: float = 5.0) -> Dict[str, Any]:

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

    def upload_chunked(self, url: str, payload: Any,
                       method: str = "POST", **kwargs) -> List[Any]:
        return self.fragmenter.upload_in_chunks(url, payload, method, **kwargs)

    def paginate(self, url: str, **kwargs) -> List[Any]:
        return self.fragmenter.paginate(url, **kwargs)

    def fragment_params(self, url: str, params: Dict[str, Any], **kwargs) -> List[Any]:
        return self.fragmenter.fragment_params(url, params, **kwargs)

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
        try:
            self.bypass.close()
        except Exception:
            pass


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


def _build_argparser():
    import argparse
    p = argparse.ArgumentParser(description="ScrapingStack - orchestrateur unifié.")
    p.add_argument("urls", nargs="*", help="URLs à traiter")
    p.add_argument("--urls-file", help="Fichier texte (une URL par ligne)")
    p.add_argument("--proxies-file", help="Fichier de proxies")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--mode", choices=["thread", "process", "async"], default="thread")
    p.add_argument("--captcha-key", default=None,
                   help="Clé API CapSolver / 2Captcha")
    p.add_argument("--captcha-service", default="capsolver",
                   choices=["capsolver", "2captcha", "anticaptcha"])
    p.add_argument("--no-browser", action="store_true",
                   help="Désactiver le fallback navigateur")
    p.add_argument("--no-tls", action="store_true",
                   help="Désactiver curl_cffi")
    p.add_argument("--min-delay", type=float, default=0.4)
    p.add_argument("--max-delay", type=float, default=2.5)
    p.add_argument("--verbose", action="store_true")
    return p


def main():
    import argparse

    args = _build_argparser().parse_args()
    urls: List[str] = list(args.urls)

    if args.urls_file:
        with open(args.urls_file, "r", encoding="utf-8") as f:
            urls.extend(l.strip() for l in f if l.strip() and not l.startswith("#"))

    if not urls:
        print("Aucune URL fournie. Exemple :")
        print("  python main.py https://httpbin.org/get --verbose")
        print("  python main.py --urls-file urls.txt --workers 8")
        return

    stack = ScrapingStack(
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