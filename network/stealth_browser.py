from __future__ import annotations
import time
import logging
from typing import Dict, List, Optional

try:
    import requests
except ImportError:
    requests = None

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    curl_requests = None
    CURL_CFFI_AVAILABLE = False

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False

try:
    import nodriver
    NODRIVER_AVAILABLE = True
except ImportError:
    nodriver = None
    NODRIVER_AVAILABLE = False

from protection_bypass.protection_detector import ProtectionInfo
from protection_bypass.captcha_solvers import CaptchaSolverAPI, SolveResult, SolveStrategy


logger = logging.getLogger("stealth_browser")


class StealthBrowser:
    def __init__(self,
                 headless: bool = True,
                 proxy: Optional[Dict[str, str]] = None,
                 user_agent: Optional[str] = None,
                 timeout: int = 60000,
                 locale: str = "fr-FR",
                 timezone: str = "Europe/Paris"):
        self.headless = headless
        self.proxy = proxy
        self.user_agent = user_agent
        self.timeout = timeout
        self.locale = locale
        self.timezone = timezone

    def solve_with_playwright(self,
                              url: str,
                              wait_selectors: Optional[List[str]] = None,
                              solve_captcha: bool = False,
                              captcha_solver: Optional[CaptchaSolverAPI] = None,
                              protection: Optional[ProtectionInfo] = None) -> SolveResult:
        if not PLAYWRIGHT_AVAILABLE:
            return SolveResult(success=False, error="Playwright not installed")

        start = time.time()
        wait_selectors = wait_selectors or ["body"]

        try:
            with sync_playwright() as p:
                browser_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ]
                launch_opts = {"headless": self.headless, "args": browser_args}
                if self.proxy:
                    launch_opts["proxy"] = {
                        "server": self.proxy.get("http") or self.proxy.get("https"),
                    }

                browser = p.chromium.launch(**launch_opts)
                context_opts = {
                    "user_agent": self.user_agent,
                    "locale": self.locale,
                    "timezone_id": self.timezone,
                    "viewport": {"width": 1920, "height": 1080},
                }
                context = browser.new_context(**context_opts)

                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['fr-FR','fr','en']});
                    window.chrome = { runtime: {} };
                """)

                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)

                for _ in range(30):
                    content = page.content()
                    if not any(p in content for p in ["Checking your browser",
                                                      "Just a moment",
                                                      "cf-challenge"]):
                        break
                    time.sleep(1)

                for sel in wait_selectors:
                    try:
                        page.wait_for_selector(sel, timeout=10000)
                        break
                    except Exception:
                        continue

                if solve_captcha and captcha_solver and protection:
                    result = captcha_solver.solve(protection, url)
                    if result.success and result.token:
                        token_js = f"""
                            (function() {{
                                const token = "{result.token}";
                                const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
                                if (ta) {{ ta.value = token; ta.innerHTML = token; }}
                                const cf = document.querySelector('input[name="cf-turnstile-response"]');
                                if (cf) {{ cf.value = token; }}
                                const hc = document.querySelector('textarea[name="h-captcha-response"]');
                                if (hc) {{ hc.value = token; hc.innerHTML = token; }}
                                return true;
                            }})();
                        """
                        try:
                            page.evaluate(token_js)
                            time.sleep(2)
                        except Exception as e:
                            logger.warning(f"Token injection failed: {e}")

                cookies = {c["name"]: c["value"] for c in context.cookies()}
                html = page.content()
                current_ua = page.evaluate("navigator.userAgent")

                browser.close()
                return SolveResult(
                    success=True,
                    cookies=cookies,
                    html=html,
                    user_agent=current_ua,
                    strategy_used=SolveStrategy.BROWSER,
                    duration=time.time() - start,
                )

        except Exception as e:
            return SolveResult(success=False, error=str(e),
                               duration=time.time() - start)

    def solve_with_nodriver(self,
                            url: str,
                            wait_seconds: int = 15) -> SolveResult:
        if not NODRIVER_AVAILABLE:
            return SolveResult(success=False, error="nodriver not installed")

        import asyncio

        async def _run():
            browser = await nodriver.start(
                headless=self.headless,
                browser_args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = await browser.get(url)
                await asyncio.sleep(wait_seconds)
                html = await page.get_content()
                cookies_list = await browser.cookies.get_all()
                cookies = {c.name: c.value for c in cookies_list}
                ua = await page.evaluate("navigator.userAgent")
                return SolveResult(
                    success=True,
                    cookies=cookies,
                    html=html,
                    user_agent=ua,
                    strategy_used=SolveStrategy.BROWSER,
                )
            finally:
                browser.stop()

        try:
            return asyncio.run(_run())
        except Exception as e:
            return SolveResult(success=False, error=str(e))

    def solve(self,
              url: str,
              solve_captcha: bool = False,
              captcha_solver: Optional[CaptchaSolverAPI] = None,
              protection: Optional[ProtectionInfo] = None,
              wait_selectors: Optional[List[str]] = None) -> SolveResult:
        if PLAYWRIGHT_AVAILABLE:
            return self.solve_with_playwright(url, wait_selectors,
                                              solve_captcha, captcha_solver,
                                              protection)
        if NODRIVER_AVAILABLE:
            return self.solve_with_nodriver(url)
        return SolveResult(success=False,
                           error="No browser available (Playwright or nodriver required)")


class TLSImpersonator:
    BROWSER_PROFILES = ["chrome124", "chrome123", "chrome120",
                        "safari17_0", "edge101", "firefox133"]

    def __init__(self, impersonate: Optional[str] = None, rotate: bool = True):
        if not CURL_CFFI_AVAILABLE:
            raise ImportError("curl_cffi required: pip install curl_cffi")
        self.impersonate = impersonate
        self.rotate = rotate

    def _pick_profile(self) -> str:
        if self.impersonate:
            return self.impersonate
        import random
        return random.choice(self.BROWSER_PROFILES)

    def request(self, method: str, url: str, **kwargs):
        profile = self._pick_profile()
        try:
            resp = curl_requests.request(
                method, url,
                impersonate=profile,
                timeout=kwargs.pop("timeout", 20),
                **kwargs,
            )
            logger.debug(f"curl_cffi {method} {url} -> {resp.status_code} (profile={profile})")
            return resp
        except Exception as e:
            logger.warning(f"curl_cffi failed ({profile}): {e}")
            raise