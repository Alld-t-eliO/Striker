"""
distributed_bots.py
-------------------
Orchestrateur de workers multi-thread avec file in-memory ou Redis.
"""

from __future__ import annotations

import time
import uuid
import threading
import queue
import signal
import hashlib
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

try:
    from core.delay_jitter import backoff_delay # type: ignore
except ImportError:
    backoff_delay = None  # type: ignore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------
class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    RETRY = "retry"


class WorkerStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    STOPPED = "stopped"


@dataclass
class Task:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    url: str = ""
    payload: Any = None
    meta: Dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    max_attempts: int = 3
    status: TaskStatus = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Any = None
    error: Optional[str] = None
    worker_id: Optional[str] = None
    shard_key: Optional[str] = None

    def clone_for_retry(self) -> "Task":
        """Nouvelle instance pour la file de retry (résultat non transporté)."""
        return Task(
            id=self.id,
            url=self.url,
            payload=self.payload,
            meta=dict(self.meta),
            attempts=self.attempts,
            max_attempts=self.max_attempts,
            status=TaskStatus.RETRY,
            created_at=self.created_at,
            shard_key=self.shard_key,
        )


@dataclass
class WorkerInfo:
    id: str
    status: WorkerStatus = WorkerStatus.IDLE
    current_task: Optional[str] = None
    tasks_done: int = 0
    tasks_failed: int = 0
    last_heartbeat: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)
    current_proxy: Optional[str] = None


# ---------------------------------------------------------------------------
# Files de tâches
# ---------------------------------------------------------------------------
class InMemoryTaskQueue:
    def __init__(self):
        self._q: "queue.Queue[Task]" = queue.Queue()
        self._retry: "queue.Queue[Task]" = queue.Queue()

    def put(self, task: Task, retry: bool = False) -> None:
        (self._retry if retry else self._q).put(task)

    def get(self, timeout: float = 0.5) -> Optional[Task]:
        try:
            return self._retry.get_nowait()
        except queue.Empty:
            pass
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def qsize(self) -> int:
        return self._q.qsize() + self._retry.qsize()

    def empty(self) -> bool:
        return self._q.empty() and self._retry.empty()


class RedisTaskQueue:
    """
    File distribuée via Redis.
    Le résultat n'est PAS sérialisé dans la file (évite la saturation).
    """

    def __init__(self, host: str = "localhost", port: int = 6379,
                 db: int = 0, key: str = "bot:tasks"):
        try:
            import redis  # type: ignore
            import json as _json
        except ImportError:
            raise ImportError("pip install redis")
        self._json = _json
        self.r = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        self.key = key
        self.retry_key = f"{key}:retry"

    def _to_dict(self, t: Task) -> Dict:
        return {
            "id": t.id, "url": t.url, "payload": t.payload,
            "meta": t.meta, "attempts": t.attempts,
            "max_attempts": t.max_attempts,
            "created_at": t.created_at, "shard_key": t.shard_key,
        }

    def _from_dict(self, d: Dict) -> Task:
        return Task(
            id=d["id"], url=d["url"], payload=d.get("payload"),
            meta=d.get("meta", {}), attempts=d.get("attempts", 0),
            max_attempts=d.get("max_attempts", 3),
            created_at=d.get("created_at", time.time()),
            shard_key=d.get("shard_key"),
        )

    def put(self, task: Task, retry: bool = False) -> None:
        key = self.retry_key if retry else self.key
        self.r.lpush(key, self._json.dumps(self._to_dict(task)))

    def get(self, timeout: float = 1.0) -> Optional[Task]:
        for key in (self.retry_key, self.key):
            item = self.r.rpop(key)
            if item:
                return self._from_dict(self._json.loads(item))
        if timeout > 0:
            item = self.r.brpop([self.retry_key, self.key],
                                timeout=int(max(1, timeout)))
            if item:
                return self._from_dict(self._json.loads(item[1]))
        return None

    def qsize(self) -> int:
        return self.r.llen(self.key) + self.r.llen(self.retry_key)

    def empty(self) -> bool:
        return self.qsize() == 0


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------
class DistributedBotPool:
    def __init__(self,
                 tasks: Optional[Iterable[Any]] = None,
                 handler: Optional[Callable[[Task, "BotContext"], Any]] = None,
                 num_workers: int = 4,
                 mode: str = "thread",
                 shard_by: str = "host",
                 proxy_rotator: Optional[Any] = None,
                 ua_rotator: Optional[Any] = None,
                 jitter: Optional[Any] = None,
                 rate_limiter: Optional[Any] = None,
                 backoff: Optional[Any] = None,
                 task_queue: Optional[Any] = None,
                 on_result: Optional[Callable[[Task], None]] = None,
                 on_error: Optional[Callable[[Task], None]] = None,
                 verbose: bool = False):
        if mode != "thread":
            raise ValueError(
                "Seul le mode 'thread' est supporté avec état partagé en mémoire. "
                "Pour du multi-process, utilise RedisTaskQueue + plusieurs "
                "process lancés séparément."
            )
        self.handler = handler
        self.num_workers = num_workers
        self.mode = mode
        self.shard_by = shard_by
        self.proxy_rotator = proxy_rotator
        self.ua_rotator = ua_rotator
        self.jitter = jitter
        self.rate_limiter = rate_limiter
        self.backoff = backoff
        self.on_result = on_result
        self.on_error = on_error
        self.verbose = verbose

        self.queue = task_queue or InMemoryTaskQueue()
        self.workers: Dict[str, WorkerInfo] = {}
        self.results: List[Task] = []
        self.failures: List[Task] = []
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        if tasks:
            for t in tasks:
                self.add_task(t)

    # ------------------------------------------------------------------
    def _normalize_task(self, item: Any) -> Task:
        if isinstance(item, Task):
            task = item
        elif isinstance(item, str):
            task = Task(url=item)
        elif isinstance(item, dict):
            task = Task(
                url=item.get("url", ""),
                payload=item.get("payload"),
                meta=item.get("meta", {}),
                max_attempts=item.get("max_attempts", 3),
            )
        else:
            raise TypeError(f"Type de tâche non supporté : {type(item)}")
        task.shard_key = self._shard_key(task)
        return task

    def _shard_key(self, task: Task) -> Optional[str]:
        if self.shard_by == "none" or not task.url:
            return None
        if self.shard_by == "host":
            return urlparse(task.url).netloc or None
        if self.shard_by == "hash":
            return hashlib.md5(task.url.encode()).hexdigest()[:2]
        return None

    def add_task(self, item: Any) -> Task:
        task = self._normalize_task(item)
        self.queue.put(task)
        return task

    def add_tasks(self, items: Iterable[Any]) -> int:
        n = 0
        for item in items:
            self.add_task(item)
            n += 1
        return n

    def _build_context(self, worker_id: str, task: Task) -> "BotContext":
        return BotContext(
            worker_id=worker_id,
            task=task,
            proxy_rotator=self.proxy_rotator,
            ua_rotator=self.ua_rotator,
            jitter=self.jitter,
            rate_limiter=self.rate_limiter,
            backoff=self.backoff,
            pool=self,
        )

    # ------------------------------------------------------------------
    # Décision d'arrêt
    # ------------------------------------------------------------------
    def _should_stop(self) -> bool:
        """
        À appeler sous verrou. Vrai si la file est vide ET aucun worker BUSY.
        """
        if not self.queue.empty():
            return False
        return not any(w.status == WorkerStatus.BUSY
                       for w in self.workers.values())

    # ------------------------------------------------------------------
    # Boucle worker
    # ------------------------------------------------------------------
    def _worker_loop(self, worker_id: str) -> None:
        info = WorkerInfo(id=worker_id)
        with self._lock:
            self.workers[worker_id] = info

        if self.verbose:
            logger.info("[pool] worker %s démarré", worker_id)

        while not self._stop_event.is_set():
            task = self.queue.get(timeout=0.5)

            if task is None:
                with self._lock:
                    if self._should_stop():
                        break
                continue

            info.status = WorkerStatus.BUSY
            info.current_task = task.id
            info.last_heartbeat = time.time()

            task.status = TaskStatus.RUNNING
            task.started_at = time.time()
            task.worker_id = worker_id
            task.attempts += 1

            ctx = self._build_context(worker_id, task)

            try:
                # Circuit breaker global
                if self.backoff is not None:
                    if not self.backoff.is_available(task.url):
                        self.backoff.wait_if_blocked(task.url)

                if self.rate_limiter is not None:
                    self.rate_limiter.wait()
                if self.jitter is not None:
                    self.jitter.sleep()

                result = self.handler(task, ctx) if self.handler else None

                task.result = result
                task.status = TaskStatus.DONE
                task.finished_at = time.time()
                info.tasks_done += 1

                with self._lock:
                    self.results.append(task)

                if self.on_result:
                    try:
                        self.on_result(task)
                    except Exception:
                        logger.exception("on_result a échoué")

                if self.verbose:
                    logger.info("[pool] %s OK %s", worker_id, task.url[:80])

            except Exception as e:
                task.error = repr(e)
                task.finished_at = time.time()

                if task.attempts < task.max_attempts:
                    task.status = TaskStatus.RETRY
                    if self.backoff is not None:
                        delay = self.backoff.on_failure(task.url, exception=e)
                    elif backoff_delay is not None:
                        delay = backoff_delay(task.attempts - 1, jitter="full")
                    else:
                        delay = min(2 ** task.attempts, 5.0)
                    time.sleep(min(delay, 5.0))
                    self.queue.put(task.clone_for_retry(), retry=True)
                    if self.verbose:
                        logger.info("[pool] %s RETRY %s (%d/%d)",
                                    worker_id, task.url[:60],
                                    task.attempts, task.max_attempts)
                else:
                    task.status = TaskStatus.FAILED
                    info.tasks_failed += 1
                    with self._lock:
                        self.failures.append(task)
                    if self.on_error:
                        try:
                            self.on_error(task)
                        except Exception:
                            logger.exception("on_error a échoué")
                    if self.verbose:
                        logger.warning("[pool] %s FAIL %s -> %r",
                                       worker_id, task.url[:60], e)

            finally:
                info.status = WorkerStatus.IDLE
                info.current_task = None
                info.current_proxy = None
                info.last_heartbeat = time.time()

        info.status = WorkerStatus.STOPPED
        if self.verbose:
            logger.info("[pool] worker %s arrêté (%d ok / %d ko)",
                        worker_id, info.tasks_done, info.tasks_failed)

    # ------------------------------------------------------------------
    # Lancement
    # ------------------------------------------------------------------
    def run(self, progress_every: float = 5.0) -> Dict[str, Any]:
        self._stop_event.clear()
        self.results.clear()
        self.failures.clear()
        self.workers.clear()

        stop_monitor = threading.Event()
        monitor_thread = None
        if self.verbose:
            monitor_thread = threading.Thread(
                target=self._monitor, args=(stop_monitor, progress_every),
                daemon=True,
            )
            monitor_thread.start()

        t0 = time.time()
        try:
            self._run_threads()
        except KeyboardInterrupt:
            logger.info("interruption, arrêt...")
            self.stop()
        finally:
            stop_monitor.set()
            if monitor_thread:
                monitor_thread.join(timeout=2.0)

        duration = time.time() - t0
        return {
            "duration": duration,
            "results": self.results,
            "failures": self.failures,
            "workers": {wid: {
                "tasks_done": w.tasks_done,
                "tasks_failed": w.tasks_failed,
                "status": w.status.value,
            } for wid, w in self.workers.items()},
            "total_done": len(self.results),
            "total_failed": len(self.failures),
        }

    def _run_threads(self) -> None:
        threads = []
        for i in range(self.num_workers):
            wid = f"w{i+1}"
            t = threading.Thread(target=self._worker_loop, args=(wid,),
                                 daemon=False)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

    # ------------------------------------------------------------------
    def _monitor(self, stop_event: threading.Event, every: float) -> None:
        while not stop_event.wait(every):
            with self._lock:
                active = sum(1 for w in self.workers.values()
                             if w.status == WorkerStatus.BUSY)
                done = len(self.results)
                failed = len(self.failures)
            logger.info("[monitor] file=%d actifs=%d/%d ok=%d ko=%d",
                        self.queue.qsize(), active, self.num_workers,
                        done, failed)

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop_event.set()

    def install_signal_handlers(self) -> None:
        def handler(signum, frame):
            logger.info("[pool] signal %s reçu", signum)
            self.stop()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "queue_size": self.queue.qsize(),
                "total_workers": len(self.workers),
                "done": len(self.results),
                "failed": len(self.failures),
                "workers": {wid: {
                    "status": w.status.value,
                    "tasks_done": w.tasks_done,
                    "tasks_failed": w.tasks_failed,
                    "current_task": w.current_task,
                } for wid, w in self.workers.items()},
            }


# ---------------------------------------------------------------------------
# Contexte
# ---------------------------------------------------------------------------
class BotContext:
    def __init__(self,
                 worker_id: str,
                 task: Task,
                 proxy_rotator: Optional[Any] = None,
                 ua_rotator: Optional[Any] = None,
                 jitter: Optional[Any] = None,
                 rate_limiter: Optional[Any] = None,
                 backoff: Optional[Any] = None,
                 pool: Optional[DistributedBotPool] = None):
        self.worker_id = worker_id
        self.task = task
        self.proxy_rotator = proxy_rotator
        self.ua_rotator = ua_rotator
        self.jitter = jitter
        self.rate_limiter = rate_limiter
        self.backoff = backoff
        self.pool = pool
        # Mémorise le proxy courant pour ne pas en tirer un autre
        # au moment de marquer 'bad'
        self._current_proxy_url: Optional[str] = None

    def get_proxies(self) -> Optional[Dict[str, str]]:
        if self.proxy_rotator is None:
            return None
        proxies = self.proxy_rotator.get_proxy()
        if proxies:
            self._current_proxy_url = proxies.get("http")
        return proxies

    def get_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = self.ua_rotator.get_headers() if self.ua_rotator else {}
        if extra:
            headers.update(extra)
        return headers

    def request(self, session, method: str, url: str, **kwargs):
        if "headers" not in kwargs:
            kwargs["headers"] = self.get_headers()
        if "proxies" not in kwargs and self.proxy_rotator is not None:
            kwargs["proxies"] = self.get_proxies()

        if self.backoff is not None:
            return self.backoff.request(session, method, url, **kwargs)
        return session.request(method, url, **kwargs)

    def sleep_jitter(self) -> float:
        return self.jitter.sleep() if self.jitter else 0.0

    def mark_current_proxy_bad(self, reason: str = "") -> None:
        """
        Marque KO le proxy RÉELLEMENT utilisé par ce worker (pas un autre).
        À appeler après un 403/429/503 constaté sur la réponse.
        """
        if self.proxy_rotator is None or self._current_proxy_url is None:
            return
        self.proxy_rotator.mark_bad(self._current_proxy_url, reason=reason)


# ---------------------------------------------------------------------------
# Handler par défaut + démo
# ---------------------------------------------------------------------------
def _demo_handler(task: Task, ctx: BotContext) -> Dict[str, Any]:
    import requests
    sess = requests.Session()
    try:
        resp = ctx.request(sess, "GET", task.url, timeout=10)
        status = getattr(resp, "status_code", None)
        if status in (403, 429, 503):
            ctx.mark_current_proxy_bad(reason=f"HTTP {status}")
        text = getattr(resp, "text", "") or ""
        return {
            "status": status,
            "size": len(text),
            "url": getattr(resp, "url", task.url),
            "worker": ctx.worker_id,
        }
    finally:
        sess.close()


if __name__ == "__main__":
    import argparse, logging as _logging
    p = argparse.ArgumentParser()
    p.add_argument("--urls", nargs="*", help="URLs à traiter")
    p.add_argument("--urls-file", help="Fichier texte (une URL par ligne)")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--shard-by", choices=["host", "hash", "none"], default="host")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    _logging.basicConfig(level=_logging.INFO,
                         format="%(asctime)s %(levelname)s %(message)s")

    urls: List[str] = list(args.urls or [])
    if args.urls_file:
        with open(args.urls_file) as f:
            urls.extend(l.strip() for l in f
                        if l.strip() and not l.startswith("#"))

    if not urls:
        print("Aucune URL. Exemple : python distributed_bots.py --urls http://localhost/ --verbose")
        raise SystemExit(1)

    pool = DistributedBotPool(
        tasks=urls, handler=_demo_handler, num_workers=args.workers,
        shard_by=args.shard_by, verbose=args.verbose,
    )
    pool.install_signal_handlers()
    stats = pool.run(progress_every=3.0)
    print(f"\nDurée: {stats['duration']:.2f}s | ok={stats['total_done']} "
          f"ko={stats['total_failed']}")