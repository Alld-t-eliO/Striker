from __future__ import annotations
import json
import time
import logging
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional
from itertools import islice

try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    from core.delay_jitter import backoff_delay  # type: ignore
except ImportError:
    backoff_delay = None  # type: ignore

logger = logging.getLogger(__name__)


def _chunks(seq: List[Any], size: int) -> Iterator[List[Any]]:
    if size <= 0:
        raise ValueError("size doit être > 0")
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _serialize(payload: Any) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload).encode("utf-8")


class RequestFragmenter:
    def __init__(self,
                 chunk_size: int = 64 * 1024,
                 max_params_per_request: int = 5,
                 max_urls_per_batch: int = 10,
                 proxy_rotator: Optional[Any] = None,
                 ua_rotator: Optional[Any] = None,
                 jitter: Optional[Any] = None,
                 rate_limiter: Optional[Any] = None,
                 max_retries: int = 3,
                 timeout: float = 15.0,
                 session: Optional[Any] = None):
        self.chunk_size = chunk_size
        self.max_params_per_request = max_params_per_request
        self.max_urls_per_batch = max_urls_per_batch
        self.proxy_rotator = proxy_rotator
        self.ua_rotator = ua_rotator
        self.jitter = jitter
        self.rate_limiter = rate_limiter
        self.max_retries = max_retries
        self.timeout = timeout

        if requests is None:
            raise ImportError("requests requis : pip install requests")
        self.session = session or requests.Session()

    def fragment_payload(self, payload: Any) -> List[bytes]:
        data = _serialize(payload)
        return [data[i:i + self.chunk_size]
                for i in range(0, len(data), self.chunk_size)]

    def upload_in_chunks(self,
                         url: str,
                         payload: Any,
                         method: str = "POST",
                         extra_headers: Optional[Dict[str, str]] = None) -> List[Any]:

        chunks = self.fragment_payload(payload)
        total = len(chunks)
        responses = []

        for i, chunk in enumerate(chunks):
            headers = self._build_headers(extra_headers)
            headers.update({
                "Content-Type": "application/octet-stream",
                "X-Chunk-Index": str(i),
                "X-Chunk-Total": str(total),
            })
            resp = self._request_with_retry(
                method, url, data=chunk, headers=headers
            )
            responses.append(resp)
        return responses

    def stream_in_chunks(self,
                         url: str,
                         payload: Any,
                         method: str = "POST",
                         extra_headers: Optional[Dict[str, str]] = None) -> Any:

        chunks = self.fragment_payload(payload)
        headers = self._build_headers(extra_headers)
        headers["Content-Type"] = "application/octet-stream"

        def gen():
            for c in chunks:
                yield c

        return self._request_with_retry(method, url, data=gen(), headers=headers)

    def split_params(self,
                     url: str,
                     params: Dict[str, Any],
                     method: str = "GET",
                     extra_headers: Optional[Dict[str, str]] = None) -> List[Any]:

        items = list(params.items())
        batches = list(_chunks(items, self.max_params_per_request))
        responses = []

        for batch in batches:
            headers = self._build_headers(extra_headers)
            resp = self._request_with_retry(
                method, url, params=dict(batch), headers=headers
            )
            responses.append(resp)
        return responses

    def paginate(self,
                 url: str,
                 page_param: str = "page",
                 start: int = 1,
                 max_pages: int = 100,
                 is_last: Optional[Callable[[Any], bool]] = None,
                 method: str = "GET",
                 extra_headers: Optional[Dict[str, str]] = None,
                 extra_params: Optional[Dict[str, Any]] = None) -> List[Any]:
        responses = []
        page = start

        while page < start + max_pages:
            params = dict(extra_params or {})
            params[page_param] = page
            headers = self._build_headers(extra_headers)

            resp = self._request_with_retry(
                method, url, params=params, headers=headers
            )
            if resp is None:
                break

            responses.append(resp)

            if is_last and is_last(resp):
                break
            if not is_last and self._looks_empty(resp):
                break
            if resp.status_code in (404, 410):
                break

            page += 1

        return responses

    def distribute(self,
                   urls: List[str],
                   method: str = "GET",
                   extra_headers: Optional[Dict[str, str]] = None,
                   chunk_size: Optional[int] = None) -> List[Any]:
        batch_size = chunk_size or self.max_urls_per_batch
        responses = []

        for batch in _chunks(urls, batch_size):
            headers = self._build_headers(extra_headers)
            proxies = self._get_proxies()
            for url in batch:
                resp = self._request_with_retry(
                    method, url, headers=headers, proxies=proxies
                )
                responses.append(resp)
        return responses

    @staticmethod
    def fragment_iterable(iterable: Iterable, size: int) -> Iterator[List[Any]]:
        it = iter(iterable)
        while True:
            chunk = list(islice(it, size))
            if not chunk:
                return
            yield chunk

    async def upload_in_chunks_async(self,
                                     url: str,
                                     payload: Any,
                                     method: str = "POST",
                                     extra_headers: Optional[Dict[str, str]] = None) -> List[Any]:
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

    def _build_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self.ua_rotator is not None:
            headers = self.ua_rotator.get_headers()
        if extra:
            headers.update(extra)
        return headers

    def _get_proxies(self) -> Optional[Dict[str, str]]:
        if self.proxy_rotator is None:
            return None
        return self.proxy_rotator.get_proxy()

    def _request_with_retry(self, method: str, url: str, **kwargs) -> Any:

        if self.backoff is not None:
            return self.backoff.request(self.session, method, url, **kwargs)

        kwargs.setdefault("timeout", self.timeout)

        for attempt in range(self.max_retries):
            try:
                if  self.rate_limiter is not None:
                    self.rate_limiter.wait()
                if  self.jitter is not None:
                    self.jitter.sleep()

                proxies = kwargs.get("proxies") or self._get_proxies()
                if proxies:
                    kwargs["proxies"] = proxies

                resp = self.session.request(method, url, **kwargs)

                if resp.status_code in (403, 429, 502, 503, 504):
                    if self.proxy_rotator is not None and proxies:
                        self.proxy_rotator.mark_bad(
                            proxies.get("http", ""),
                            reason=f"HTTP {resp.status_code}",
                        )
                    if attempt < self.max_retries - 1:
                        delay = (backoff_delay(attempt, jitter="full")
                                if backoff_delay else 2 ** attempt)
                        time.sleep(delay)
                        continue
                return resp

            except Exception as e:
                logger.warning("Attempt %d failed on %s: %r", attempt + 1, url, e)
                if attempt == self.max_retries - 1:
                    return None
                delay = (backoff_delay(attempt, jitter="full")
                        if backoff_delay else 2 ** attempt)
                time.sleep(delay)
        return None

    @staticmethod
    def _looks_empty(resp) -> bool:
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
            if len(resp.text.strip()) < 10:
                return True
        return False


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--chunk-size", type=int, default=32)
    p.add_argument("--params-per-request", type=int, default=2)
    args = p.parse_args()

    frag = RequestFragmenter(
        chunk_size=args.chunk_size,
        max_params_per_request=args.params_per_request,
    )
    payload = {"description": "A" * 200}
    chunks = frag.fragment_payload(payload)
    print(f"Payload fragmenté en {len(chunks)} morceaux :")
    for i, c in enumerate(chunks):
        print(f"  chunk {i}: {len(c)} octets")