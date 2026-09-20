from __future__ import annotations
import os
import threading
import time
from pathlib import Path
from typing import Optional
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static, Footer, Input, Label, Button
from textual.binding import Binding
from textual.screen import Screen


APP_NAME = os.getenv("APP_NAME", "STRIKER")
VERSION = os.getenv("VERSION", "0.1.0")
GITHUB_NAME = os.getenv("GITHUB_NAME", "Aegon")

_CSS_PATH = Path(__file__).with_name("tui.tcss")
CSS_PATH = str(_CSS_PATH) if _CSS_PATH.exists() else None


BANNER = r"""
   ______      _ __
  / __/ /_____(_) /_____ ____
 _\ \/ __/ __/ /  '_/ -_) __/
/___/\__/_/ /_/_/\_\\__/_/
"""


class OffensiveState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running: bool = False
        self.target: str = ""
        self.port: int = 80
        self.threads: int = 10
        self.sockets_per_thread: int = 50
        self.keepalive_interval: float = 15.0
        self.alive_sockets: int = 0
        self.opened: int = 0
        self.closed: int = 0
        self.errors: dict = {}
        self.started_at: Optional[float] = None
        self.last_message: str = ""
        self.last_error: str = ""

    def update(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "target": self.target,
                "port": self.port,
                "threads": self.threads,
                "sockets_per_thread": self.sockets_per_thread,
                "keepalive_interval": self.keepalive_interval,
                "alive_sockets": self.alive_sockets,
                "opened": self.opened,
                "closed": self.closed,
                "errors": self.errors.copy(),
                "started_at": self.started_at,
                "last_message": self.last_message,
                "last_error": self.last_error,
            }


class MainScreen(Screen):
    BINDINGS = [
        Binding("s", "start", "START"),
        Binding("x", "stop", "STOP"),
        Binding("c", "clear", "CLEAR"),
        Binding("q", "quit_app", "QUIT"),
        Binding("escape", "quit_app", "QUIT"),
    ]

    def compose(self) -> ComposeResult:
        st = self.app.offensive_state.snapshot()
        yield Vertical(
            Static(BANNER, id="banner"),
            Static(f"// OFFENSIVE MODULE   // BY {GITHUB_NAME}   // v{VERSION}", id="byline"),
            Static(
                "⚠  MODULE OFFENSIF — SLOWLORIS  ⚠\n"
                "Utilisation sur cibles autorisées uniquement.\n"
                "Vérifie le cadre légal et le périmètre avant de lancer.",
                id="warning_banner",
            ),

            Horizontal(
                Vertical(
                    Label("CIBLE (IP)", classes="field_label"),
                    Input(value=st["target"], placeholder="192.168.56.20", id="inp_target"),
                    Label("PORT", classes="field_label"),
                    Input(value=str(st["port"]), placeholder="80", id="inp_port"),
                    Label("THREADS", classes="field_label"),
                    Input(value=str(st["threads"]), placeholder="10", id="inp_threads"),
                    id="form_left",
                ),
                Vertical(
                    Label("SOCKETS / THREAD", classes="field_label"),
                    Input(value=str(st["sockets_per_thread"]),
                          placeholder="50", id="inp_spt"),
                    Label("KEEPALIVE (s)", classes="field_label"),
                    Input(value=str(st["keepalive_interval"]),
                          placeholder="15", id="inp_interval"),
                    Label("", classes="field_label"),
                    Static("", id="spacer"),
                    id="form_right",
                ),
                id="form_row",
            ),

            Horizontal(
                Button("DÉMARRER", id="btn_start", variant="success"),
                Button("ARRÊTER", id="btn_stop", variant="error"),
                Button("EFFACER", id="btn_clear"),
                Button("QUITTER", id="btn_quit"),
                id="buttons",
            ),

            Static(self._render_status(), id="live"),
            Static("", id="flash"),

            Static(
                "\n[S] DÉMARRER   [X] ARRÊTER   [C] EFFACER   [Q] QUITTER",
                id="help",
            ),
            Footer(),
        )

    def on_mount(self) -> None:
        self._dos = None
        self._timer = self.set_interval(0.5, self._refresh)

    def _render_status(self) -> str:
        st = self.app.offensive_state.snapshot()
        state = "RUNNING" if st["running"] else "IDLE"
        uptime = ""
        if st["started_at"] and st["running"]:
            uptime = f"   UPTIME: {int(time.time() - st['started_at'])}s"

        errors = st.get("errors", {}) or {}
        errors_str = " ".join(f"{k}={v}" for k, v in sorted(errors.items())) or "none"

        return (
            "┌─[ STATUS ]──────────────────────────────────────────┐\n"
            f"│  STATE         : {state}{uptime}\n"
            f"│  TARGET        : {st['target']}:{st['port']}\n"
            f"│  THREADS       : {st['threads']}\n"
            f"│  SOCKETS/WORKER: {st['sockets_per_thread']}\n"
            f"│  ALIVE         : {st['alive_sockets']}\n"
            f"│  OPENED        : {st['opened']}\n"
            f"│  CLOSED        : {st['closed']}\n"
            f"│  ERRORS        : {errors_str}\n"
            "└─────────────────────────────────────────────────────┘"
        )

    def _refresh(self) -> None:
        if self._dos is not None:
            try:
                s = self._dos.stats()
                self.app.offensive_state.update(
                    alive_sockets=s["alive_sockets"],
                    opened=s["opened"],
                    closed=s["closed"],
                    errors=s.get("errors", {}),
                )
                if not s["running"]:
                    self.app.offensive_state.update(running=False)
            except Exception:
                pass

        try:
            self.query_one("#live", Static).update(self._render_status())
        except Exception:
            pass

    def _read_form(self) -> dict:
        def _int(wid: str, default: int) -> int:
            try:
                return int(self.query_one(wid, Input).value or default)
            except ValueError:
                return default

        def _float(wid: str, default: float) -> float:
            try:
                return float(self.query_one(wid, Input).value or default)
            except ValueError:
                return default

        return {
            "target": self.query_one("#inp_target", Input).value.strip(),
            "port": _int("#inp_port", 80),
            "threads": _int("#inp_threads", 10),
            "sockets_per_thread": _int("#inp_spt", 50),
            "keepalive_interval": _float("#inp_interval", 15.0),
        }

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn_start":
            self.action_start()
        elif bid == "btn_stop":
            self.action_stop()
        elif bid == "btn_clear":
            self.action_clear()
        elif bid == "btn_quit":
            self.action_quit_app()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_start()

    def action_start(self) -> None:
        form = self._read_form()
        if not form["target"]:
            self._flash("ERREUR : cible vide", level="error")
            return

        try:
            from application.dos import Dos  
        except ImportError as e:
            self._flash(f"ERREUR import : {e}", level="error")
            return

        try:
            self._dos = Dos(
                target_ip=form["target"],
                target_port=form["port"],
                threads=form["threads"],
                sockets_per_worker=form["sockets_per_thread"],
                keepalive_interval=form["keepalive_interval"],
            )
            self._dos.run()
        except Exception as e:
            self._flash(f"ERREUR lancement : {type(e).__name__}: {e}", level="error")
            self._dos = None
            return

        self.app.offensive_state.update(
            running=True,
            target=form["target"],
            port=form["port"],
            threads=form["threads"],
            sockets_per_thread=form["sockets_per_thread"],
            keepalive_interval=form["keepalive_interval"],
            started_at=time.time(),
            last_message="running",
            last_error="",
        )
        self._flash("DÉMARRÉ", level="ok")

    def action_stop(self) -> None:
        if self._dos is not None:
            try:
                self._dos.stop()
            except Exception as e:
                self._flash(f"ERREUR arrêt : {e}", level="error")
        self.app.offensive_state.update(
            running=False,
            alive_sockets=0,
            last_message="stopped",
        )
        self._flash("ARRÊTÉ", level="warn")

    def action_clear(self) -> None:
        for wid in ("#inp_target", "#inp_port", "#inp_threads",
                    "#inp_spt", "#inp_interval"):
            try:
                self.query_one(wid, Input).value = ""
            except Exception:
                pass
        self._flash("CHAMPS EFFACÉS", level="info")

    def action_quit_app(self) -> None:
        self.action_stop()
        self.app.exit()

    def _flash(self, message: str, level: str = "info") -> None:
        try:
            widget = self.query_one("#flash", Static)
            widget.update(message)
            widget.remove_class("flash-ok", "flash-warn",
                                "flash-error", "flash-info")
            widget.add_class(f"flash-{level}")
        except Exception:
            pass


class StrikerTUI(App):
    CSS_PATH = CSS_PATH
    TITLE = "STRIKER"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.offensive_state = OffensiveState()

    def on_mount(self) -> None:
        self.push_screen(MainScreen())


def run_tui() -> None:
    StrikerTUI().run()


if __name__ == "__main__":
    run_tui()