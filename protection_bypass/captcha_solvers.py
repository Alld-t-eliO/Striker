from __future__ import annotations
import os
import time
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

try:
    import requests
except ImportError:
    requests = None

from core.protection_detector import ProtectionInfo, ProtectionType


logger = logging.getLogger("captcha_solvers")


class SolveStrategy(str, Enum):
    API_SERVICE = "api_service"
    BROWSER = "browser"
    CLOUDSCRAPER = "cloudscraper"
    TLS_IMPERSONATE = "tls_impersonate"
    HYBRID = "hybrid"


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


class CaptchaSolverAPI:
    def __init__(self,
                 service: str = "capsolver",
                 api_key: Optional[str] = None,
                 timeout: int = 120,
                 poll_interval: float = 3.0):
        self.service = service.lower()
        self.api_key = api_key or self._env_key()
        self.timeout = timeout
        self.poll_interval = poll_interval

        if not self.api_key:
            logger.warning(f"No API key provided for {self.service}.")

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
            return SolveResult(success=False, error="requests not installed")

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
            return SolveResult(success=False, error=f"createTask failed: {e}")

        if data.get("errorId") not in (0, None):
            return SolveResult(success=False,
                               error=data.get("errorDescription", "unknown error"))

        task_id = data.get("taskId")
        if not task_id:
            return SolveResult(success=False, error="missing taskId")

        start = time.time()
        while time.time() - start < self.timeout:
            time.sleep(self.poll_interval)
            try:
                r = requests.post(f"{base}/getTaskResult",
                                  json={"clientKey": self.api_key, "taskId": task_id},
                                  timeout=30)
                res = r.json()
            except Exception:
                continue

            if res.get("status") == "ready":
                solution = res.get("solution", {})
                token = (solution.get("gRecaptchaResponse")
                         or solution.get("token")
                         or solution.get("captchaResponse"))
                return SolveResult(
                    success=True,
                    token=token,
                    cookies=solution.get("cookies"),
                    user_agent=solution.get("userAgent"),
                    strategy_used=SolveStrategy.API_SERVICE,
                )
            if res.get("errorId"):
                return SolveResult(success=False, error=res.get("errorDescription"))

        return SolveResult(success=False, error="solve timeout")

    def solve_2captcha(self, method: str, params: Dict[str, Any]) -> SolveResult:
        if requests is None:
            return SolveResult(success=False, error="requests not installed")

        base = "https://2captcha.com"
        payload = {"key": self.api_key, "json": 1, **params}

        try:
            r = requests.post(f"{base}/in.php", data=payload, timeout=30)
            data = r.json()
        except Exception as e:
            return SolveResult(success=False, error=f"in.php failed: {e}")

        if data.get("status") != 1:
            return SolveResult(success=False, error=data.get("request", "error"))

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
            return SolveResult(success=False, error="missing API key")

        ptype = protection.type

        if self.service == "capsolver":
            if ptype == ProtectionType.CLOUDFLARE_TURNSTILE:
                return self.solve_capsolver(
                    "AntiTurnstileTaskProxyLess", website_url,
                    protection.captcha_site_key,
                    extra=({"metadata": {"action": protection.captcha_action}}
                           if protection.captcha_action else None),
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
                return self.solve_capsolver("AwsWafClassification", website_url)
            if ptype == ProtectionType.DATADOME:
                return self.solve_capsolver("DatadomeSliderTask", website_url)
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

        return SolveResult(success=False,
                           error=f"type not supported by {self.service}")