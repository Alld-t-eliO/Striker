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

from core.protection_detector import (
    ProtectionDetector, ProtectionInfo, ProtectionType,
)
from core.captcha_solvers import (
    CaptchaSolverAPI, SolveResult, SolveStrategy,
)
from core.stealth_browser import StealthBrowser, TLSImpersonator


logger = logging.getLogger("captcha_waf_bypass")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class CaptchaWafBypass:
    # ... (le code que je t'ai donné dans le message précédent)