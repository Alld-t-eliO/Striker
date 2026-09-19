from __future__ import annotations
import json
import time
import logging
from typing import Any, Dict, Optional

try:
    import requests
except ImportError:
    requests = None

try:
    import cloudscraper
    CLOUDSCRAPER_AVAILABLE = True
except ImportError:
    cloudscraper = None
    CLOUDSCRAPER_AVAILABLE = False

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    curl_requests = None
    CURL_CFFI_AVAILABLE = False

from protection_bypass.protection_detector import (
    ProtectionDetector, ProtectionInfo, ProtectionType,
)
from protection_bypass.captcha_solvers import (
    CaptchaSolverAPI, SolveResult, SolveStrategy,
)
from http.stealth_browser import StealthBrowser, TLSImpersonator


logger = logging.getLogger("captcha_waf_bypass")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class CaptchaWafBypass:
    """
    Orchestrates HTTP transport, protection detection, CAPTCHA solving,
    and stealth browser fallback.
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
        self.captcha_solver = (CaptchaSolverAPI(captcha_service, captcha_api_key)
                               if captcha_api_key else None)
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

        if self.use_tls_impersonation:
            try:
                self.tls_client = TLSImpersonator()
                logger.info("TLS fingerprint enabled (curl_cffi)")
            except ImportError:
                self.tls_client = None
        else:
            self.tls_client = None

        self.cloudscraper_client = None
        if CLOUDSCRAPER_AVAILABLE:
            try:
                self.cloudscraper_client = cloudscraper.create_scraper()
                logger.info("Cloudscraper available for JS challenges")
            except Exception:
                self.cloudscraper_client = None

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

    def _raw_request(self,
                     method: str,
                     url: str,
                     headers: Optional[Dict[str, str]] = None,
                     proxies: Optional[Dict[str, str]] = None,
                     **kwargs):
        if self.use_tls_impersonation and self.tls_client:
            try:
                return self.tls_client.request(method, url, headers=headers,
                                               proxies=proxies, **kwargs)
            except Exception as e:
                logger.warning(f"TLS impersonation failed, fallback to requests: {e}")

        if requests is None:
            raise RuntimeError("Neither curl_cffi nor requests available")
        return requests.request(method, url, headers=headers,
                                proxies=proxies,
                                timeout=kwargs.pop("timeout", 20), **kwargs)

    def request(self,
                method: str,
                url: str,
                headers: Optional[Dict[str, str]] = None,
                **kwargs) -> Any:
        last_response = None
        last_error = None

        for attempt in range(self.max_retries):
            self._apply_jitter_and_rate_limit()

            hdrs = self._build_headers(headers)
            proxies = self._get_proxies()

            try:
                resp = self._raw_request(method, url, headers=hdrs,
                                         proxies=proxies, **kwargs)
            except Exception as e:
                last_error = e
                logger.warning(f"Attempt {attempt+1} failed: {e}")
                if self.backoff is not None:
                    time.sleep(self.backoff.on_failure(url, exception=e))
                continue

            last_response = resp
            status = getattr(resp, "status_code", 0)
            body = getattr(resp, "text", "") or ""

            protection = ProtectionDetector.detect(resp, body)

            if (status == 200
                    and protection.type == ProtectionType.NONE
                    and not protection.requires_browser):
                return resp

            if (protection.type == ProtectionType.CLOUDFLARE_JS
                    and self.cloudscraper_client is not None
                    and not protection.requires_browser):
                try:
                    cs_resp = self.cloudscraper_client.request(
                        method, url, headers=hdrs, proxies=proxies,
                        timeout=kwargs.get("timeout", 20)
                    )
                    if cs_resp.status_code == 200:
                        logger.info(f"Cloudscraper solved {url}")
                        return cs_resp
                    last_response = cs_resp
                except Exception as e:
                    logger.warning(f"Cloudscraper failed: {e}")

            if (protection.type in (ProtectionType.RECAPTCHA_V2,
                                    ProtectionType.RECAPTCHA_V3,
                                    ProtectionType.HCAPTCHA,
                                    ProtectionType.CLOUDFLARE_TURNSTILE,
                                    ProtectionType.AWS_WAF,
                                    ProtectionType.DATADOME)
                    and self.captcha_solver is not None
                    and protection.captcha_site_key):
                logger.info(f"CAPTCHA detected ({protection.type.value}), solving via API...")
                solve_res = self.captcha_solver.solve(protection, url)
                if solve_res.success and solve_res.token:
                    submit_resp = self._submit_captcha_token(
                        method, url, protection, solve_res, hdrs, proxies, **kwargs
                    )
                    if submit_resp is not None and submit_resp.status_code == 200:
                        logger.info("CAPTCHA solved via API")
                        return submit_resp
                    last_response = submit_resp or last_response
                else:
                    logger.warning(f"API solve failed: {solve_res.error}")

            if (protection.requires_browser or protection.js_challenge
                    or protection.type in (ProtectionType.DATADOME,
                                           ProtectionType.AKAMAI,
                                           ProtectionType.IMPERVA)) \
                    and self.use_browser_fallback:
                logger.info(f"Complex challenge ({protection.type.value}), switching to browser...")
                browser = StealthBrowser(
                    headless=self.headless,
                    proxy=proxies,
                    user_agent=hdrs.get("User-Agent"),
                )
                result = browser.solve(
                    url,
                    solve_captcha=self.captcha_solver is not None,
                    captcha_solver=self.captcha_solver,
                    protection=protection,
                )
                if result.success:
                    logger.info(f"Browser solved {url} ({result.duration:.1f}s)")
                    return self._wrap_browser_result(result, url)

            if self.backoff is not None:
                delay = self.backoff.on_failure(
                    url, status=status,
                    retry_after=self._parse_retry_after(resp),
                )
                time.sleep(delay)
            else:
                time.sleep(2 ** attempt)

        if last_response is not None:
            return last_response
        if last_error:
            raise last_error
        return None

    def _submit_captcha_token(self,
                              method: str,
                              url: str,
                              protection: ProtectionInfo,
                              solve_res: SolveResult,
                              headers: Dict[str, str],
                              proxies: Optional[Dict[str, str]],
                              **kwargs):
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
            logger.warning(f"Token submission failed: {e}")
            return None

    def _wrap_browser_result(self, result: SolveResult, url: str):
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
                self.elapsed = type("Elapsed", (),
                                    {"total_seconds": lambda s: res.duration})()

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

    @staticmethod
    def _parse_retry_after(resp) -> Optional[float]:
        header = (getattr(resp, "headers", {}).get("Retry-After")
                  if hasattr(resp, "headers") else None)
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

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def close(self):
        if self.cloudscraper_client is not None:
            try:
                self.cloudscraper_client.close()
            except Exception:
                pass