from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


logger = logging.getLogger("protection_detector")


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
                info.type = (ProtectionType.CLOUDFLARE_JS
                             if "cloudflare" in (info.waf_name or "")
                             else ProtectionType.GENERIC_WAF)
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
            logger.debug(f"Protection detected: {info.type.value} "
                         f"(waf={info.waf_name}, conf={info.confidence:.2f})")
        return info