import os
import re
import time
import json
import base64
import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Callable
from urllib.parse import urlparse, urlencode, quote

from core.proxy_rotator import ProxyRotator
from core.user_agent_rotator import UserAgentRotator
from core.delay_jitter import DelayJitter, RateLimiter, backoff_delay
from core.adaptive_backoff import AdaptiveBackoff

try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    from curl_cffi import requests as curl_requests  # type: ignore
    CURL_CFFI_AVAILABLE = True
except ImportError:
    curl_requests = None  # type: ignore
    CURL_CFFI_AVAILABLE = False

try:
    import cloudscraper  # type: ignore
    CLOUDSCRAPER_AVAILABLE = True
except ImportError:
    cloudscraper = None  # type: ignore
    CLOUDSCRAPER_AVAILABLE = False

try:
    from playwright.sync_api import sync_playwright  # type: ignore
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    sync_playwright = None  # type: ignore
    PLAYWRIGHT_AVAILABLE = False

try:
    import nodriver  # type: ignore
    NODRIVER_AVAILABLE = True
except ImportError:
    nodriver = None  # type: ignore
    NODRIVER_AVAILABLE = False


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

try:
    from adaptive_backoff import AdaptiveBackoff  # type: ignore
except ImportError:
    AdaptiveBackoff = None  # type: ignore


logger = logging.getLogger("captcha_waf_bypass")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class ProtectionType(str, Enum):
    NONE = "none"
    CLOUDFLARE_JS = "cloudflare_js"
    CLOUDFLARE_TURNSTILE = "cloudflare_turnstile"
    RECAPTCHA_V2 = "recaptcha_v2"
    RECAPTCHA_V3 = "recaptcha_v3"
    HCAPTCHA = "hcaptcha"
    AWS_WAF = "aws_waf"
    DATADOME = "datadome"
    AKAMAI = "akamai"
    IMPERVA = "imperva"
    GENERIC_WAF = "generic_waf"
    UNKNOWN = "unknown"


class SolveStrategy(str, Enum):
    API_SERVICE = "api_service"     
    BROWSER = "browser"             
    CLOUDSCRAPER = "cloudscraper"    
    TLS_IMPERSONATE = "tls_impersonate"  
    HYBRID = "hybrid"               


@dataclass
class ProtectionInfo:
    type: ProtectionType = ProtectionType.NONE
    waf_name: Optional[str] = None
    captcha_site_key: Optional[str] = None
    captcha_action: Optional[str] = None
    challenge_url: Optional[str] = None
    js_challenge: bool = False
    requires_browser: bool = False
    confidence: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SolveResult:
    success: bool
    token: Optional[str] = None
    cookies: Optional[Dict[str, str]] = None
    headers: Optional[Dict[str, str]] = None
    user_agent: Optional[str] = None
    html: Optional[str] = None
    strategy_used: Optional[SolveStrategy] = None
    duration: float = 0.0
    error: Optional[str] = None


class ProtectionDetector:
    WAF_SIGNATURES = {
        "cloudflare": [
            ("header", "cf-ray"),
            ("header", "cf-cache-status"),
            ("body", "Checking your browser before accessing"),
            ("body", "cf-challenge"),
            ("body", "challenge-platform"),
            ("body", "__cf_chl_"),
        ],
        "aws_waf": [
            ("header", "x-amzn-waf-action"),
            ("body", "awswaf"),
            ("body", "AWS WAF"),
            ("body", "challenge.js"),
        ],
        "datadome": [
            ("header", "x-datadome"),
            ("body", "datadome"),
            ("body", "geo.captcha-delivery.com"),
        ],
        "akamai": [
            ("header", "akamai-grn"),
            ("body", "_abck"),
            ("body", "akamai"),
        ],
        "imperva": [
            ("header", "x-iinfo"),
            ("header", "incap_ses"),
            ("body", "Incapsula"),
            ("body", "_Incapsula_Resource"),
        ],
        "sucuri": [
            ("header", "x-sucuri-id"),
            ("body", "Sucuri WebSite Firewall"),
        ],
        "f5_big_ip": [
            ("header", "x-wa-info"),
            ("body", "BigIP"),
        ],
    }

    CAPTCHA_SIGNATURES = {
        ProtectionType.RECAPTCHA_V2: [
            r'data-sitekey=["\']([^"\']+)["\']',
            r'g-recaptcha',
            r'www\.google\.com/recaptcha/api\.js',
        ],
        ProtectionType.RECAPTCHA_V3: [
            r'grecaptcha\.execute\([\'"]([^\'"]+)[\'"]',
            r'recaptcha/api\.js\?render=([^"&\']+)',
        ],
        ProtectionType.HCAPTCHA: [
            r'data-sitekey=["\']([^"\']+)["\']',
            r'h-captcha',
            r'js\.hcaptcha\.com',
        ],
        ProtectionType.CLOUDFLARE_TURNSTILE: [
            r'data-sitekey=["\']([^"\']+)["\']',
            r'challenges\.cloudflare\.com/turnstile',
            r'cf-turnstile',
        ],
    }

    BROWSER_REQUIRED_PATTERNS = [
        "challenge-platform", "cf-challenge", "awswaf", "datadome",
        "captcha-delivery", "_abck", "incap_ses", "Just a moment",
    ]

    @classmethod
    def detect(cls, response, body: Optional[str] = None) -> ProtectionInfo:
        info = ProtectionInfo()
        if response is None:
            return info

        status = getattr(response, "status_code", 0)
        headers = {k.lower(): v for k, v in getattr(response, "headers", {}).items()}
        html = body if body is not None else getattr(response, "text", "") or ""

        for waf_name, signatures in cls.WAF_SIGNATURES.items():
            for sig_type, pattern in signatures:
                if sig_type == "header" and pattern.lower() in headers:
                    info.waf_name = waf_name
                    info.confidence = max(info.confidence, 0.7)
                elif sig_type == "body" and pattern.lower() in html.lower():
                    info.waf_name = info.waf_name or waf_name
                    info.confidence = max(info.confidence, 0.6)

        for captcha_type, patterns in cls.CAPTCHA_SIGNATURES.items():
            for pattern in patterns:
                match = re.search(pattern, html, re.IGNORECASE)
                if match:
                    info.type = captcha_type
                    if match.groups():
                        info.captcha_site_key = match.group(1)
                    info.confidence = max(info.confidence, 0.85)
                    break

        if not info.type or info.type == ProtectionType.NONE:
            if any(p.lower() in html.lower() for p in cls.BROWSER_REQUIRED_PATTERNS):
                info.type = ProtectionType.CLOUDFLARE_JS if "cloudflare" in (info.waf_name or "") else ProtectionType.GENERIC_WAF
                info.js_challenge = True
                info.requires_browser = True
                info.confidence = max(info.confidence, 0.7)

        if info.type == ProtectionType.RECAPTCHA_V3 and not info.captcha_action:
            action_match = re.search(r'render=["\']([^"\']+)["\']', html)
            if action_match:
                info.captcha_action = action_match.group(1)

        if status in (403, 503) and info.confidence < 0.5:
            info.type = ProtectionType.GENERIC_WAF
            info.confidence = 0.4
            info.requires_browser = True

        if info.type in (ProtectionType.CLOUDFLARE_JS, ProtectionType.AWS_WAF,
                         ProtectionType.DATADOME, ProtectionType.AKAMAI,
                         ProtectionType.IMPERVA):
            info.requires_browser = True

        if info.confidence > 0:
            logger.debug(f"Protection detected : {info.type.value} "
                         f"(waf={info.waf_name}, conf={info.confidence:.2f})")
        return info


class CaptchaSolverAPI:
    def __init__(self,
                 service: str = "capsolver",
                 api_key: Optional[str] = None,
                 timeout: int = 120,
                 poll_interval: float = 3.0):
        """
        :param service: 'capsolver' | '2captcha' | 'anticaptcha'
        :param api_key: Clé API (ou variable d'env CAPSOLVER_API_KEY / TWOCAPTCHA_API_KEY / ANTICAPTCHA_API_KEY)
        :param timeout: Timeout max de résolution (secondes)
        :param poll_interval: Intervalle de polling (secondes)
        """
        self.service = service.lower()
        self.api_key = api_key or self._env_key()
        self.timeout = timeout
        self.poll_interval = poll_interval

        if not self.api_key:
            logger.warning(f"No API key found fournie for {self.service}.")

    def _env_key(self) -> Optional[str]:
        mapping = {
            "capsolver": "CAPSOLVER_API_KEY",
            "2captcha": "TWOCAPTCHA_API_KEY",
            "anticaptcha": "ANTICAPTCHA_API_KEY",
        }
        return os.getenv(mapping.get(self.service, "CAPTCHA_API_KEY"))


    def solve_capsolver(self,
                        captcha_type: str,
                        website_url: str,
                        website_key: Optional[str] = None,
                        page_action: Optional[str] = None,
                        extra: Optional[Dict] = None) -> SolveResult:
        if requests is None:
            return SolveResult(success=False, error="requests non installé")

        base = "https://api.capsolver.com"
        task = {"type": captcha_type, "websiteURL": website_url}
        if website_key:
            task["websiteKey"] = website_key
        if page_action:
            task["pageAction"] = page_action
        if extra:
            task.update(extra)

        try:
            r = requests.post(f"{base}/createTask",
                              json={"clientKey": self.api_key, "task": task},
                              timeout=30)
            data = r.json()
        except Exception as e:
            return SolveResult(success=False, error=f"createTask échoué : {e}")

        if data.get("errorId") not in (0, None):
            return SolveResult(success=False, error=data.get("errorDescription", "erreur inconnue"))

        task_id = data.get("taskId")
        if not task_id:
            return SolveResult(success=False, error="taskId manquant")

        start = time.time()
        while time.time() - start < self.timeout:
            time.sleep(self.poll_interval)
            try:
                r = requests.post(f"{base}/getTaskResult",
                                  json={"clientKey": self.api_key, "taskId": task_id},
                                  timeout=30)
                res = r.json()
            except Exception as e:
                continue

            if res.get("status") == "ready":
                solution = res.get("solution", {})
                token = (solution.get("gRecaptchaResponse")
                         or solution.get("token")
                         or solution.get("captchaResponse"))
                cookies = solution.get("cookies")
                ua = solution.get("userAgent")
                return SolveResult(
                    success=True,
                    token=token,
                    cookies=cookies,
                    user_agent=ua,
                    strategy_used=SolveStrategy.API_SERVICE,
                )
            if res.get("errorId"):
                return SolveResult(success=False, error=res.get("errorDescription"))

        return SolveResult(success=False, error="timeout de résolution")

    def solve_2captcha(self,
                       method: str,
                       params: Dict[str, Any]) -> SolveResult:
        if requests is None:
            return SolveResult(success=False, error="requests non installé")

        base = "https://2captcha.com"
        payload = {"key": self.api_key, "json": 1, **params}

        try:
            r = requests.post(f"{base}/in.php", data=payload, timeout=30)
            data = r.json()
        except Exception as e:
            return SolveResult(success=False, error=f"in.php échoué : {e}")

        if data.get("status") != 1:
            return SolveResult(success=False, error=data.get("request", "erreur"))

        request_id = data.get("request")
        start = time.time()

        while time.time() - start < self.timeout:
            time.sleep(self.poll_interval)
            try:
                r = requests.get(f"{base}/res.php",
                                 params={"key": self.api_key, "action": "get",
                                         "id": request_id, "json": 1},
                                 timeout=30)
                res = r.json()
            except Exception:
                continue

            if res.get("status") == 1:
                return SolveResult(success=True, token=res.get("request"),
                                   strategy_used=SolveStrategy.API_SERVICE)
            if res.get("request") != "CAPCHA_NOT_READY":
                return SolveResult(success=False, error=res.get("request"))

        return SolveResult(success=False, error="timeout")


    def solve(self,
              protection: ProtectionInfo,
              website_url: str,
              user_agent: Optional[str] = None) -> SolveResult:
        if not self.api_key:
            return SolveResult(success=False, error="clé API manquante")

        ptype = protection.type

        if self.service == "capsolver":
            if ptype == ProtectionType.CLOUDFLARE_TURNSTILE:
                return self.solve_capsolver(
                    "AntiTurnstileTaskProxyLess", website_url,
                    protection.captcha_site_key,
                    extra={"metadata": {"action": protection.captcha_action}} if protection.captcha_action else None,
                )
            if ptype == ProtectionType.RECAPTCHA_V2:
                return self.solve_capsolver("ReCaptchaV2TaskProxyLess",
                                            website_url, protection.captcha_site_key)
            if ptype == ProtectionType.RECAPTCHA_V3:
                return self.solve_capsolver("ReCaptchaV3TaskProxyLess",
                                            website_url, protection.captcha_site_key,
                                            page_action=protection.captcha_action or "verify")
            if ptype == ProtectionType.HCAPTCHA:
                return self.solve_capsolver("HCaptchaTaskProxyLess",
                                            website_url, protection.captcha_site_key)
            if ptype == ProtectionType.AWS_WAF:
                return self.solve_capsolver("AwsWafClassification",
                                            website_url)
            if ptype in (ProtectionType.DATADOME,):
                return self.solve_capsolver("DatadomeSliderTask",
                                            website_url)
            return self.solve_capsolver("ReCaptchaV2TaskProxyLess",
                                        website_url, protection.captcha_site_key)

        if self.service == "2captcha":
            if ptype == ProtectionType.RECAPTCHA_V2:
                return self.solve_2captcha("userrecaptcha", {
                    "method": "userrecaptcha",
                    "googlekey": protection.captcha_site_key,
                    "pageurl": website_url,
                })
            if ptype == ProtectionType.RECAPTCHA_V3:
                return self.solve_2captcha("userrecaptcha", {
                    "method": "userrecaptcha",
                    "version": "v3",
                    "action": protection.captcha_action or "verify",
                    "googlekey": protection.captcha_site_key,
                    "pageurl": website_url,
                })
            if ptype == ProtectionType.HCAPTCHA:
                return self.solve_2captcha("hcaptcha", {
                    "method": "hcaptcha",
                    "sitekey": protection.captcha_site_key,
                    "pageurl": website_url,
                })
            if ptype == ProtectionType.CLOUDFLARE_TURNSTILE:
                return self.solve_2captcha("turnstile", {
                    "method": "turnstile",
                    "sitekey": protection.captcha_site_key,
                    "pageurl": website_url,
                })

        return SolveResult(success=False, error=f"type non supporté par {self.service}")


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
            return SolveResult(success=False, error="Playwright non installé")

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
                                // reCAPTCHA
                                const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
                                if (ta) {{ ta.value = token; ta.innerHTML = token; }}
                                // Turnstile
                                const cf = document.querySelector('input[name="cf-turnstile-response"]');
                                if (cf) {{ cf.value = token; }}
                                // hCaptcha
                                const hc = document.querySelector('textarea[name="h-captcha-response"]');
                                if (hc) {{ hc.value = token; hc.innerHTML = token; }}
                                // Déclencher les callbacks
                                if (window.___grecaptcha_cfg) {{
                                    try {{
                                        Object.values(window.___grecaptcha_cfg.clients || {{}}).forEach(c => {{
                                            Object.values(c).forEach(v => {{
                                                if (v && v.callback) v.callback(token);
                                            }});
                                        }});
                                    }} catch(e) {{}}
                                }}
                                return true;
                            }})();
                        """
                        try:
                            page.evaluate(token_js)
                            time.sleep(2)
                            # Soumettre le formulaire si présent
                            try:
                                page.evaluate("""
                                    (function(){
                                        const forms = document.querySelectorAll('form');
                                        for (const f of forms) {
                                            if (f.querySelector('[name="g-recaptcha-response"], [name="cf-turnstile-response"], [name="h-captcha-response"]')) {
                                                f.submit();
                                                return true;
                                            }
                                        }
                                        return false;
                                    })();
                                """)
                                time.sleep(2)
                            except Exception:
                                pass
                        except Exception as e:
                            logger.warning(f"Injection du token échouée : {e}")

                # Récupérer cookies + HTML final
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
        """Résout via nodriver (successeur d'undetected-chromedriver)."""
        if not NODRIVER_AVAILABLE:
            return SolveResult(success=False, error="nodriver non installé")

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
        """Point d'entrée unifié."""
        if PLAYWRIGHT_AVAILABLE:
            return self.solve_with_playwright(url, wait_selectors,
                                              solve_captcha, captcha_solver,
                                              protection)
        if NODRIVER_AVAILABLE:
            return self.solve_with_nodriver(url)
        return SolveResult(success=False, error="Aucun navigateur disponible (Playwright ou nodriver requis)")


# ---------------------------------------------------------------------------
# Client TLS impersoné (curl_cffi)
# ---------------------------------------------------------------------------
class TLSImpersonator:
    """
    Client HTTP qui imite l'empreinte TLS/JA3 d'un vrai navigateur via curl_cffi.
    C'est la première ligne de défense contre les WAF modernes qui inspectent
    le handshake TLS avant même de lire la requête HTTP.
    """

    BROWSER_PROFILES = ["chrome124", "chrome123", "chrome120",
                        "safari17_0", "edge101", "firefox133"]

    def __init__(self, impersonate: Optional[str] = None, rotate: bool = True):
        if not CURL_CFFI_AVAILABLE:
            raise ImportError("curl_cffi requis : pip install curl_cffi")
        self.impersonate = impersonate
        self.rotate = rotate

    def _pick_profile(self) -> str:
        if self.impersonate:
            return self.impersonate
        import random
        return random.choice(self.BROWSER_PROFILES)

    def request(self, method: str, url: str, **kwargs):
        """Effectue une requête avec empreinte TLS réaliste."""
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
            logger.warning(f"curl_cffi échec ({profile}) : {e}")
            raise


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------
class CaptchaWafBypass:
    """
    Client unifié de contournement de CAPTCHA et WAF.

    Combine :
    - Détection automatique du type de protection
    - Empreinte TLS réaliste (curl_cffi)
    - Résolution via service API (CapSolver, 2Captcha...)
    - Bascule vers navigateur furtif (Playwright / nodriver)
    - Intégration proxy / UA / jitter / backoff
    """

    def __init__(self,
                 captcha_service: str = "capsolver",
                 captcha_api_key: Optional[str] = None,
                 proxy_rotator: Optional[Any] = None,
                 ua_rotator: Optional[Any] = None,
                 jitter: Optional[Any] = None,
                 rate_limiter: Optional[Any] = None,
                 backoff: Optional[Any] = None,
                 use_tls_impersonation: bool = True,
                 use_browser_fallback: bool = True,
                 headless: bool = True,
                 max_retries: int = 3,
                 verbose: bool = False):
        """
        :param captcha_service: 'capsolver' | '2captcha' | 'anticaptcha'
        :param captcha_api_key: Clé API du service
        :param proxy_rotator: Instance ProxyRotator
        :param ua_rotator: Instance UserAgentRotator
        :param jitter: Instance DelayJitter
        :param rate_limiter: Instance RateLimiter
        :param backoff: Instance AdaptiveBackoff
        :param use_tls_impersonation: Utiliser curl_cffi si disponible
        :param use_browser_fallback: Basculer vers navigateur si nécessaire
        :param headless: Navigateur en mode headless
        :param max_retries: Nombre de tentatives
        :param verbose: Logs détaillés
        """
        self.captcha_solver = CaptchaSolverAPI(captcha_service, captcha_api_key) if captcha_api_key else None
        self.proxy_rotator = proxy_rotator
        self.ua_rotator = ua_rotator
        self.jitter = jitter
        self.rate_limiter = rate_limiter
        self.backoff = backoff
        self.use_tls_impersonation = use_tls_impersonation and CURL_CFFI_AVAILABLE
        self.use_browser_fallback = use_browser_fallback
        self.headless = headless
        self.max_retries = max_retries
        self.verbose = verbose

        if verbose:
            logger.setLevel(logging.DEBUG)

        # Client HTTP de base
        if self.use_tls_impersonation:
            try:
                self.tls_client = TLSImpersonator()
                logger.info("Empreinte TLS activée (curl_cffi)")
            except ImportError:
                self.tls_client = None
        else:
            self.tls_client = None

        # Fallback cloudscraper pour Cloudflare JS
        self.cloudscraper_client = None
        if CLOUDSCRAPER_AVAILABLE:
            try:
                self.cloudscraper_client = cloudscraper.create_scraper()
                logger.info("Cloudscraper disponible pour challenges JS")
            except Exception:
                self.cloudscraper_client = None

    # ------------------------------------------------------------------
    # Construction des headers / proxies
    # ------------------------------------------------------------------
    def _build_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {}
        if self.ua_rotator is not None:
            headers = self.ua_rotator.get_headers()
        if extra:
            headers.update(extra)
        return headers

    def _get_proxies(self) -> Optional[Dict[str, str]]:
        if self.proxy_rotator is None:
            return None
        return self.proxy_rotator.get_proxy()

    def _apply_jitter_and_rate_limit(self) -> None:
        if self.rate_limiter is not None:
            self.rate_limiter.wait()
        if self.jitter is not None:
            self.jitter.sleep()

    # ------------------------------------------------------------------
    # Requête de base (avant détection)
    # ------------------------------------------------------------------
    def _raw_request(self,
                     method: str,
                     url: str,
                     headers: Optional[Dict[str, str]] = None,
                     proxies: Optional[Dict[str, str]] = None,
                     **kwargs):
        """Effectue la requête HTTP brute avec empreinte TLS si possible."""
        if self.use_tls_impersonation and self.tls_client:
            try:
                return self.tls_client.request(method, url, headers=headers,
                                               proxies=proxies, **kwargs)
            except Exception as e:
                logger.warning(f"TLS impersonation échouée, fallback requests : {e}")

        if requests is None:
            raise RuntimeError("Ni curl_cffi ni requests disponibles")
        return requests.request(method, url, headers=headers,
                                proxies=proxies,
                                timeout=kwargs.pop("timeout", 20), **kwargs)

    # ------------------------------------------------------------------
    # Méthode principale
    # ------------------------------------------------------------------
    def request(self,
                method: str,
                url: str,
                headers: Optional[Dict[str, str]] = None,
                **kwargs) -> Any:
        """
        Effectue une requête en contournant automatiquement CAPTCHA / WAF.
        Retourne la réponse finale (avec cookies de session si navigateur utilisé).
        """
        last_response = None
        last_error = None

        for attempt in range(self.max_retries):
            self._apply_jitter_and_rate_limit()

            hdrs = self._build_headers(headers)
            proxies = self._get_proxies()

            # 1) Requête brute
            try:
                resp = self._raw_request(method, url, headers=hdrs,
                                         proxies=proxies, **kwargs)
            except Exception as e:
                last_error = e
                logger.warning(f"Tentative {attempt+1} échouée : {e}")
                if self.backoff is not None:
                    time.sleep(self.backoff.on_failure(url, exception=e))
                continue

            last_response = resp
            status = getattr(resp, "status_code", 0)
            body = getattr(resp, "text", "") or ""

            # 2) Détection protection
            protection = ProtectionDetector.detect(resp, body)

            # 3) Succès direct
            if status == 200 and protection.type == ProtectionType.NONE and not protection.requires_browser:
                if self.verbose:
                    logger.info(f"✅ {method} {url} -> 200 (pas de protection)")
                return resp

            # 4) Cloudflare JS simple -> cloudscraper
            if (protection.type == ProtectionType.CLOUDFLARE_JS
                    and self.cloudscraper_client is not None
                    and not protection.requires_browser):
                try:
                    cs_resp = self.cloudscraper_client.request(
                        method, url, headers=hdrs, proxies=proxies,
                        timeout=kwargs.get("timeout", 20)
                    )
                    if cs_resp.status_code == 200:
                        logger.info(f"✅ Cloudscraper a résolu {url}")
                        return cs_resp
                    last_response = cs_resp
                except Exception as e:
                    logger.warning(f"Cloudscraper échoué : {e}")

            # 5) CAPTCHA résoluble via API
            if (protection.type in (ProtectionType.RECAPTCHA_V2,
                                    ProtectionType.RECAPTCHA_V3,
                                    ProtectionType.HCAPTCHA,
                                    ProtectionType.CLOUDFLARE_TURNSTILE,
                                    ProtectionType.AWS_WAF,
                                    ProtectionType.DATADOME)
                    and self.captcha_solver is not None
                    and protection.captcha_site_key):
                logger.info(f"[INFO] CAPTCHA detected ({protection.type.value}), resolution using API...")
                solve_res = self.captcha_solver.solve(protection, url)
                if solve_res.success and solve_res.token:
                    submit_resp = self._submit_captcha_token(
                        method, url, protection, solve_res, hdrs, proxies, **kwargs
                    )
                    if submit_resp is not None and submit_resp.status_code == 200:
                        logger.info("[SUCCESS] CAPTCHA resolved using API")
                        return submit_resp
                    last_response = submit_resp or last_response
                else:
                    logger.warning(f"API resolution failed: {solve_res.error}")

            if (protection.requires_browser or protection.js_challenge
                    or protection.type in (ProtectionType.DATADOME,
                                           ProtectionType.AKAMAI,
                                           ProtectionType.IMPERVA)) \
                    and self.use_browser_fallback:
                logger.info(f"🌐 Challenge complexe ({protection.type.value}), bascule navigateur...")
                browser = StealthBrowser(
                    headless=self.headless,
                    proxy=proxies,
                    user_agent=hdrs.get("User-Agent"),
                )
                solve_captcha = self.captcha_solver is not None
                result = browser.solve(
                    url,
                    solve_captcha=solve_captcha,
                    captcha_solver=self.captcha_solver,
                    protection=protection,
                )
                if result.success:
                    logger.info(f"✅ Navigateur a résolu {url} ({result.duration:.1f}s)")
                    return self._wrap_browser_result(result, url)

            # 7) Backoff avant retry
            if self.backoff is not None:
                delay = self.backoff.on_failure(
                    url, status=status,
                    retry_after=self._parse_retry_after(resp),
                )
                time.sleep(delay)
            else:
                time.sleep(2 ** attempt)

        # Échec définitif
        if last_response is not None:
            return last_response
        if last_error:
            raise last_error
        return None

    # ------------------------------------------------------------------
    # Soumission du token CAPTCHA
    # ------------------------------------------------------------------
    def _submit_captcha_token(self,
                              method: str,
                              url: str,
                              protection: ProtectionInfo,
                              solve_res: SolveResult,
                              headers: Dict[str, str],
                              proxies: Optional[Dict[str, str]],
                              **kwargs):
        """
        Soumet le token CAPTCHA. Stratégie : reconstruire la requête avec
        le token ajouté au body ou aux paramètres.
        """
        try:
            data = kwargs.get("data") or {}
            json_data = kwargs.get("json") or {}
            params = kwargs.get("params") or {}

            token_field = "g-recaptcha-response"
            if protection.type == ProtectionType.CLOUDFLARE_TURNSTILE:
                token_field = "cf-turnstile-response"
            elif protection.type == ProtectionType.HCAPTCHA:
                token_field = "h-captcha-response"

            if isinstance(data, dict):
                data[token_field] = solve_res.token
            elif isinstance(json_data, dict):
                json_data[token_field] = solve_res.token
            else:
                data = {token_field: solve_res.token}

            new_kwargs = dict(kwargs)
            new_kwargs["data"] = data
            if json_data:
                new_kwargs["json"] = json_data
            if params:
                new_kwargs["params"] = params

            return self._raw_request(method, url, headers=headers,
                                     proxies=proxies, **new_kwargs)
        except Exception as e:
            logger.warning(f"Soumission du token échouée : {e}")
            return None

    # ------------------------------------------------------------------
    # Résultat navigateur -> objet compatible requests
    # ------------------------------------------------------------------
    def _wrap_browser_result(self, result: SolveResult, url: str):
        """
        Convertit un SolveResult (navigateur) en objet compatible requests.Response.
        """
        class BrowserResponse:
            def __init__(self, res, url):
                self.status_code = 200 if res.success else 403
                self.text = res.html or ""
                self.content = (res.html or "").encode("utf-8")
                self.url = url
                self.headers = {"User-Agent": res.user_agent or ""}
                self.cookies = self._cookies_to_jar(res.cookies or {})
                self.encoding = "utf-8"
                self.reason = "OK" if res.success else "Blocked"
                self.elapsed = type("Elapsed", (), {"total_seconds": lambda s: res.duration})()

            @staticmethod
            def _cookies_to_jar(cookies_dict):
                class CookieJar:
                    def __init__(self, d):
                        self._d = d
                    def get_dict(self):
                        return self._d
                    def __iter__(self):
                        return iter(self._d.items())
                return CookieJar(cookies_dict)

            def json(self):
                return json.loads(self.text)

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise Exception(f"HTTP {self.status_code}")

        return BrowserResponse(result, url)

    # ------------------------------------------------------------------
    # Retry-After
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_retry_after(resp) -> Optional[float]:
        header = getattr(resp, "headers", {}).get("Retry-After") if hasattr(resp, "headers") else None
        if not header:
            return None
        try:
            return float(header)
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(header)
                return max(0.0, dt.timestamp() - time.time())
            except Exception:
                return None

    # ------------------------------------------------------------------
    # API haut niveau
    # ------------------------------------------------------------------
    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def close(self):
        """Libère les ressources."""
        if self.cloudscraper_client is not None:
            try:
                self.cloudscraper_client.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Démo
# ---------------------------------------------------------------------------
def demo():
    import argparse

    parser = argparse.ArgumentParser(description="Contournement CAPTCHA / WAF.")
    parser.add_argument("url", nargs="?", default="https://httpbin.org/headers")
    parser.add_argument("--capsolver-key", default=os.getenv("CAPSOLVER_API_KEY"))
    parser.add_argument("--service", default="capsolver",
                        choices=["capsolver", "2captcha", "anticaptcha"])
    parser.add_argument("--no-tls", action="store_true", help="Désactiver curl_cffi")
    parser.add_argument("--no-browser", action="store_true", help="Désactiver Playwright/nodriver")
    parser.add_argument("--headful", action="store_true", help="Navigateur visible")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    # Modules compagnons
    ua = UserAgentRotator() if UserAgentRotator else None
    jitter = DelayJitter(min_delay=0.3, max_delay=1.5, mode="human") if DelayJitter else None
    limiter = RateLimiter(max_calls=3, period=1.0) if RateLimiter else None
    backoff = AdaptiveBackoff(strategy="adaptive", verbose=args.verbose) if AdaptiveBackoff else None

    bypass = CaptchaWafBypass(
        captcha_service=args.service,
        captcha_api_key=args.capsolver_key,
        ua_rotator=ua,
        jitter=jitter,
        rate_limiter=limiter,
        backoff=backoff,
        use_tls_impersonation=not args.no_tls,
        use_browser_fallback=not args.no_browser,
        headless=not args.headful,
        verbose=args.verbose,
    )

    try:
        resp = bypass.get(args.url)
        if resp is not None:
            print(f"\n✅ Status: {resp.status_code}")
            print(f"   Longueur: {len(resp.text)} octets")
            print(f"   Extraits:\n{resp.text[:500]}")
        else:
            print("❌ Aucune réponse")
    finally:
        bypass.close()


if __name__ == "__main__":
    demo()
