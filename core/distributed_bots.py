"""
distributed_bots.py
-------------------
Orchestration de bots distribués pour scraping/crawl distribué.

Fonctionnalités :
- Pool de workers (threads, processus ou asyncio)
- File de tâches partagée (mémoire ou Redis)
- État global coordonné : proxies, backoff adaptatif, rate-limit
- Distribution des tâches par domaine (sharding anti-collision)
- Heartbeat / monitoring de santé des workers
- Agrégation des résultats + callbacks
- Récupération automatique des tâches échouées (retry queue)
- Compatible avec ProxyRotator, UserAgentRotator, DelayJitter,
  RequestFragmenter et AdaptiveBackoff

Usage simple :
    pool = DistributedBotPool(num_workers=4, tasks=urls, handler=my_handler)
    results = pool.run()
"""

import time
import uuid
import threading
import queue
import signal
import random
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Imports optionnels des modules compagnons
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Structures de données
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
    DEAD = "dead"


@dataclass
class Task:
    """Une tâche unitaire à exécuter."""
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
    shard_key: Optional[str] = None  # ex: host, pour sharding par domaine


@dataclass
class WorkerInfo:
    """État d'un worker."""
    id: str
    status: WorkerStatus = WorkerStatus.IDLE
    current_task: Optional[str] = None
    tasks_done: int = 0
    tasks_failed: int = 0
    last_heartbeat: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# File de tâches : in-memory (défaut) + Redis (optionnel)
# ---------------------------------------------------------------------------
class InMemoryTaskQueue:
    """File FIFO thread-safe en mémoire."""

    def __init__(self):
        self._q: "queue.Queue[Task]" = queue.Queue()
        self._retry: "queue.Queue[Task]" = queue.Queue()

    def put(self, task: Task, retry: bool = False) -> None:
        (self._retry if retry else self._q).put(task)

    def get(self, timeout: float = 1.0) -> Optional[Task]:
        # Priorité aux retries
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
    File de tâches distribuée via Redis (nécessite `redis` + un serveur Redis).
    Permet à plusieurs machines de partager la même file.
    """

    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0, key: str = "bot:tasks"):
        try:
            import redis  # type: ignore
        except ImportError:
            raise ImportError("pip install redis")
        import json
        self._json = json
        self.r = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        self.key = key
        self.retry_key = f"{key}:retry"

    @staticmethod
    def _to_dict(task: Task) -> Dict:
        return {
            "id": task.id, "url": task.url, "payload": task.payload,
            "meta": task.meta, "attempts": task.attempts,
            "max_attempts": task.max_attempts,
            "created_at": task.created_at, "shard_key": task.shard_key,
        }

    @staticmethod
    def _from_dict(d: Dict) -> Task:
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
        # Priorité aux retries
        for key in (self.retry_key, self.key):
            item = self.r.rpop(key)
            if item:
                return self._from_dict(self._json.loads(item))
        if timeout > 0:
            # Blocage court
            item = self.r.brpop([self.retry_key, self.key], timeout=int(max(1, timeout)))
            if item:
                return self._from_dict(self._json.loads(item[1]))
        return None

    def qsize(self) -> int:
        return self.r.llen(self.key) + self.r.llen(self.retry_key)

    def empty(self) -> bool:
        return self.qsize() == 0


# ---------------------------------------------------------------------------
# Pool de bots distribués
# ---------------------------------------------------------------------------
class DistributedBotPool:
    """
    Orchestrateur de bots distribués.
    """

    def __init__(self,
                 tasks: Optional[Iterable[Any]] = None,
                 handler: Optional[Callable[[Task, "BotContext"], Any]] = None,
                 num_workers: int = 4,
                 mode: str = "thread",                    # 'thread' | 'process' | 'async'
                 shard_by: str = "host",                  # 'host' | 'none' | 'hash'
                 proxy_rotator: Optional[Any] = None,
                 ua_rotator: Optional[Any] = None,
                 jitter: Optional[Any] = None,
                 rate_limiter: Optional[Any] = None,
                 backoff: Optional[Any] = None,
                 task_queue: Optional[Any] = None,        # InMemoryTaskQueue | RedisTaskQueue
                 on_result: Optional[Callable[[Task], None]] = None,
                 on_error: Optional[Callable[[Task], None]] = None,
                 verbose: bool = False):
        """
        :param tasks: Itérable initial de tâches (URLs ou dicts avec 'url')
        :param handler: Fonction (task, context) -> résultat. Reçoit le contexte
                        (proxies, headers, backoff, session) pour exécuter la requête.
        :param num_workers: Nombre de workers parallèles
        :param mode: Type d'exécution
        :param shard_by: Stratégie de sharding (par host, par hash, aucun)
        :param proxy_rotator / ua_rotator / jitter / rate_limiter / backoff :
                        Instances des modules compagnons (optionnels)
        :param task_queue: File custom (Redis pour vraie distribution multi-machines)
        :param on_result / on_error: Callbacks
        :param verbose: Logs
        """
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

        # Chargement initial des tâches
        if tasks:
            for t in tasks:
                self.add_task(t)

    # ------------------------------------------------------------------
    # Gestion des tâches
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
        # Sharding
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

    # ------------------------------------------------------------------
    # Contexte passé au handler
    # ------------------------------------------------------------------
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
    # Boucle d'un worker
    # ------------------------------------------------------------------
    def _worker_loop(self, worker_id: str) -> None:
        info = WorkerInfo(id=worker_id)
        with self._lock:
            self.workers[worker_id] = info

        if self.verbose:
            print(f"[pool] worker {worker_id} démarré")

        while not self._stop_event.is_set():
            task = self.queue.get(timeout=0.5)

            if task is None:
                if self.queue.empty():
                    # File vide : on vérifie si d'autres workers tournent encore
                    with self._lock:
                        busy = any(w.status == WorkerStatus.BUSY for w in self.workers.values())
                    if not busy:
                        break
                continue

            info.status = WorkerStatus.BUSY
            info.current_task = task.id
            info.last_heartbeat = time.time()

            task.status = TaskStatus.RUNNING
            task.started_at = time.time()
            task.worker_id = worker_id
            task.attempts += 1

            context = self._build_context(worker_id, task)

            try:
                # Attendre si le domaine est bloqué (backoff adaptatif)
                if self.backoff is not None and self.backoff.is_available(task.url):
                    self.backoff.wait_if_blocked(task.url)
                elif self.backoff is not None:
                    # Circuit ouvert : on remet en retry
                    task.status = TaskStatus.RETRY
                    self.queue.put(task, retry=True)
                    info.status = WorkerStatus.IDLE
                    info.current_task = None
                    continue

                # Rate limit + jitter globaux
                if self.rate_limiter is not None:
                    self.rate_limiter.wait()
                if self.jitter is not None:
                    self.jitter.sleep()

                result = self.handler(task, context) if self.handler else None

                task.result = result
                task.status = TaskStatus.DONE
                task.finished_at = time.time()
                info.tasks_done += 1

                with self._lock:
                    self.results.append(task)

                if self.on_result:
                    try:
                        self.on_result(task)
                    except Exception as e:
                        if self.verbose:
                            print(f"[pool] on_result a échoué : {e}")

                if self.verbose:
                    print(f"[pool] worker {worker_id} OK {task.url[:80]}")

            except Exception as e:
                task.error = str(e)
                task.finished_at = time.time()

                # Retry si possible
                if task.attempts < task.max_attempts:
                    task.status = TaskStatus.RETRY
                    delay = 1.0
                    if self.backoff is not None:
                        delay = self.backoff.on_failure(task.url, exception=e)
                    elif backoff_delay is not None:
                        delay = backoff_delay(task.attempts - 1, jitter="full")
                    time.sleep(min(delay, 5.0))  # cap court avant remise en file
                    self.queue.put(task, retry=True)
                    if self.verbose:
                        print(f"[pool] worker {worker_id} RETRY {task.url[:80]} ({task.attempts}/{task.max_attempts})")
                else:
                    task.status = TaskStatus.FAILED
                    info.tasks_failed += 1
                    with self._lock:
                        self.failures.append(task)
                    if self.on_error:
                        try:
                            self.on_error(task)
                        except Exception:
                            pass
                    if self.verbose:
                        print(f"[pool] worker {worker_id} FAIL {task.url[:80]} -> {e}")

            finally:
                info.status = WorkerStatus.IDLE
                info.current_task = None
                info.last_heartbeat = time.time()

        info.status = WorkerStatus.STOPPED
        if self.verbose:
            print(f"[pool] worker {worker_id} arrêté ({info.tasks_done} ok / {info.tasks_failed} ko)")

    # ------------------------------------------------------------------
    # Lancement du pool
    # ------------------------------------------------------------------
    def run(self, progress_every: float = 5.0) -> Dict[str, Any]:
        """
        Lance tous les workers et attend la fin.
        Retourne un dict avec résultats / statistiques.
        """
        self._stop_event.clear()
        self.results.clear()
        self.failures.clear()
        self.workers.clear()

        # Monitoring périodique
        stop_monitor = threading.Event()
        monitor_thread = None
        if self.verbose:
            monitor_thread = threading.Thread(
                target=self._monitor, args=(stop_monitor, progress_every), daemon=True
            )
            monitor_thread.start()

        t0 = time.time()

        try:
            if self.mode == "thread":
                self._run_threads()
            elif self.mode == "process":
                self._run_processes()
            elif self.mode == "async":
                self._run_async()
            else:
                raise ValueError(f"Mode inconnu : {self.mode}")
        except KeyboardInterrupt:
            print("\n[pool] interruption reçue, arrêt en cours...")
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
            t = threading.Thread(target=self._worker_loop, args=(wid,), daemon=False)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

    def _run_processes(self) -> None:
        # Note : avec process, les objets comme ProxyRotator ne sont pas
        # partagés entre processus. Pour du multi-processus, utiliser Redis
        # et des instances recréées localement dans le handler.
        with ProcessPoolExecutor(max_workers=self.num_workers) as ex:
            futures = [
                ex.submit(self._worker_loop, f"p{i+1}")
                for i in range(self.num_workers)
            ]
            for f in as_completed(futures):
                f.result()

    def _run_async(self) -> None:
        try:
            import asyncio
        except ImportError:
            raise RuntimeError("asyncio indisponible")

        async def wrapper():
            loop = asyncio.get_event_loop()
            await asyncio.gather(*[
                loop.run_in_executor(None, self._worker_loop, f"a{i+1}")
                for i in range(self.num_workers)
            ])

        asyncio.run(wrapper())

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------
    def _monitor(self, stop_event: threading.Event, every: float) -> None:
        while not stop_event.wait(every):
            with self._lock:
                active = sum(1 for w in self.workers.values() if w.status == WorkerStatus.BUSY)
                done = len(self.results)
                failed = len(self.failures)
            print(f"[monitor] file={self.queue.qsize():5d}  "
                  f"actifs={active}/{self.num_workers}  "
                  f"ok={done}  ko={failed}")

    # ------------------------------------------------------------------
    # Arrêt propre
    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop_event.set()

    def install_signal_handlers(self) -> None:
        """Arrête proprement le pool sur Ctrl+C / SIGTERM."""
        def handler(signum, frame):
            print(f"\n[pool] signal {signum} reçu")
            self.stop()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    # ------------------------------------------------------------------
    # Statistiques
    # ------------------------------------------------------------------
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
# Contexte fourni au handler
# ---------------------------------------------------------------------------
class BotContext:
    """
    Contexte passé à chaque handler de tâche.
    Donne accès aux outils partagés et facilite l'exécution d'une requête.
    """

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

    def get_proxies(self) -> Optional[Dict[str, str]]:
        return self.proxy_rotator.get_proxy() if self.proxy_rotator else None

    def get_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = self.ua_rotator.get_headers() if self.ua_rotator else {}
        if extra:
            headers.update(extra)
        return headers

    def request(self, session, method: str, url: str, **kwargs):
        """
        Effectue une requête en appliquant automatiquement :
        proxy rotatif + headers UA + retry/backoff adaptatif.
        """
        if "headers" not in kwargs:
            kwargs["headers"] = self.get_headers()
        if "proxies" not in kwargs and self.proxy_rotator is not None:
            kwargs["proxies"] = self.get_proxies()

        if self.backoff is not None:
            return self.backoff.request(session, method, url, **kwargs)

        # Sinon, simple requête
        return session.request(method, url, **kwargs)

    def sleep_jitter(self) -> float:
        return self.jitter.sleep() if self.jitter else 0.0

    def mark_proxy_bad(self) -> None:
        """Marque le proxy courant comme mauvais (à appeler après un 403/429)."""
        if self.proxy_rotator is None:
            return
        proxy = self.proxy_rotator.get_proxy()
        if proxy:
            self.proxy_rotator.mark_bad(proxy["http"])


# ---------------------------------------------------------------------------
# Démo complète
# ---------------------------------------------------------------------------
def _demo_handler(task: Task, ctx: BotContext) -> Dict[str, Any]:
    """
    Handler de démo : fait une requête GET sur task.url en utilisant tous les outils.
    """
    import requests
    sess = requests.Session()
    try:
        resp = ctx.request(sess, "GET", task.url, timeout=10)
        return {
            "status": resp.status_code,
            "size": len(resp.text),
            "url": resp.url,
            "worker": ctx.worker_id,
        }
    finally:
        sess.close()


def demo():
    from user_agent_rotator import UserAgentRotator
    from delay_jitter import DelayJitter, RateLimiter
    from adaptive_backoff import AdaptiveBackoff

    ua = UserAgentRotator()
    jitter = DelayJitter(min_delay=0.2, max_delay=1.0, mode="human")
    limiter = RateLimiter(max_calls=5, period=1.0)
    backoff = AdaptiveBackoff(strategy="adaptive", verbose=False)

    urls = [f"https://httpbin.org/anything/{i}" for i in range(20)]

    pool = DistributedBotPool(
        tasks=urls,
        handler=_demo_handler,
        num_workers=4,
        mode="thread",
        shard_by="host",
        ua_rotator=ua,
        jitter=jitter,
        rate_limiter=limiter,
        backoff=backoff,
        verbose=True,
    )
    pool.install_signal_handlers()

    stats = pool.run(progress_every=3.0)
    print("\n=== Résumé ===")
    print(f"Durée    : {stats['duration']:.2f}s")
    print(f"Réussis  : {stats['total_done']}")
    print(f"Échoués  : {stats['total_failed']}")
    for wid, s in stats["workers"].items():
        print(f"  {wid}: {s}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Orchestrateur de bots distribués.")
    parser.add_argument("--urls", nargs="*", help="Liste d'URLs à traiter")
    parser.add_argument("--urls-file", help="Fichier texte contenant une URL par ligne")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--mode", choices=["thread", "process", "async"], default="thread")
    parser.add_argument("--shard-by", choices=["host", "hash", "none"], default="host")
    parser.add_argument("--redis", action="store_true",
                        help="Utiliser Redis comme file de tâches distribuée")
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()

    if args.demo:
        demo()
        raise SystemExit(0)

    # Charge les URLs
    urls: List[str] = []
    if args.urls:
        urls.extend(args.urls)
    if args.urls_file:
        with open(args.urls_file) as f:
            urls.extend(line.strip() for line in f if line.strip() and not line.startswith("#"))

    if not urls:
        print("Aucune URL fournie. Utilise --urls ou --urls-file, ou --demo.")
        raise SystemExit(1)

    # File de tâches
    if args.redis:
        queue_obj = RedisTaskQueue(host=args.redis_host, port=args.redis_port)
    else:
        queue_obj = None

    # Instances compagnons (imports déjà faits en haut du module)
    ua = UserAgentRotator() if UserAgentRotator else None
    jitter = DelayJitter(min_delay=0.3, max_delay=1.5, mode="human") if DelayJitter else None
    limiter = RateLimiter(max_calls=5, period=1.0) if RateLimiter else None
    backoff = AdaptiveBackoff(strategy="adaptive") if AdaptiveBackoff else None

    pool = DistributedBotPool(
        tasks=urls,
        handler=_demo_handler,
        num_workers=args.workers,
        mode=args.mode,
        shard_by=args.shard_by,
        ua_rotator=ua,
        jitter=jitter,
        rate_limiter=limiter,
        backoff=backoff,
        task_queue=queue_obj,
        verbose=args.verbose,
    )
    pool.install_signal_handlers()

    stats = pool.run(progress_every=3.0)
    print("\n=== Résumé ===")
    print(f"Durée    : {stats['duration']:.2f}s")
    print(f"Réussis  : {stats['total_done']}")
    print(f"Échoués  : {stats['total_failed']}")
