from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, Center
from textual.widgets import Static, Footer
from textual.binding import Binding
from textual.screen import Screen

from config import APP_NAME, VERSION, GITHUB_NAME


BANNER = r'''
   ______      _ __             
  / __/ /_____(_) /_____ ____   
 _\ \/ __/ __/ /  '_/ -_) __/   
/___/\__/_/ /_/_/\_\\__/_/      
                                
'''


class Home(Screen):
    BINDINGS = [
        Binding("1", "striker", "STRIKER"),
        Binding("2", "settings", "SETTINGS"),
        Binding("3", "quit_app", "QUIT"),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(BANNER, id="banner"),
            Static(f"// BY Aegon    // v{VERSION}", id="byline"),
            Horizontal(
                Vertical(
                    Static(
                        "[ SYSTEM ]\n"
                        "> INITIALIZING...\n"
                        "> MODULES LOADED\n"
                        "> NETWORK READY\n"
                        "> STATUS: ONLINE",
                        id="system_panel",
                    ),
                    Static(
                        "[ GLOBAL STATUS ]\n"
                        "IP      : 127.0.0.1\n"
                        "PORT    : LOCAL\n"
                        "STATUS  : ONLINE",
                        id="status_panel",
                    ),
                    id="left",
                ),
                Vertical(
                    Static("[ STRIKER CONTROL PANEL ]", classes="panel_title"),
                    Static("┌──────────────────────────────────────────┐\n│  [1]  OPEN STRIKER PAGE                  │\n└──────────────────────────────────────────┘", id="menu1"),
                    Static("┌──────────────────────────────────────────┐\n│  [2]  SETTINGS                            │\n└──────────────────────────────────────────┘", id="menu2"),
                    Static("┌──────────────────────────────────────────┐\n│  [3]  QUIT                               │\n└──────────────────────────────────────────┘", id="menu3"),
                    Static("\n[ SELECT AN OPTION ]  1 / 2 / 3", id="prompt"),
                    id="center",
                ),
                Vertical(
                    Static(
                        "[ STRIKER ]\n"
                        "> DDOS MODULE\n"
                        "> MULTI-THREAD\n"
                        "> HIGH PERFORMANCE\n"
                        "> CUSTOM CONFIG",
                        id="modules",
                    ),
                    Static(
                        "[ WARNING ]\n"
                        "UI / CONTROL LAYER ONLY\n"
                        "No attack implementation\n"
                        "is included in this project.",
                        id="warning",
                    ),
                    id="right",
                ),
                id="columns",
            ),
            Static(f"GITHUB // BY {GITHUB_NAME} // JUST CODE", id="footer"),
            Footer(),
        )

    def action_striker(self) -> None:
        self.app.push_screen(StrikerScreen())

    def action_settings(self) -> None:
        self.app.push_screen(SettingsScreen())

    def action_quit_app(self) -> None:
        self.app.exit()


class StrikerScreen(Screen):
    BINDINGS = [Binding("escape", "back", "BACK"), Binding("3", "back", "BACK")]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(BANNER, id="banner"),
            Static(f"// STRIKER // BY {GITHUB_NAME}", id="byline"),
            Static(
                "[ STRIKER PAGE ]\n\n"
                "SYSTEM      : READY\n"
                "INTERFACE   : LOCAL TUI\n"
                "NETWORK     : NOT CONFIGURED\n"
                "ENGINE      : UI PLACEHOLDER\n\n"
                "[ ESC ] BACK",
                id="detail",
            ),
            Footer(),
        )

    def action_back(self) -> None:
        self.app.pop_screen()


class SettingsScreen(Screen):
    BINDINGS = [Binding("escape", "back", "BACK"), Binding("3", "back", "BACK")]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(BANNER, id="banner"),
            Static(f"// SETTINGS // BY {GITHUB_NAME}", id="byline"),
            Static(
                "[ SETTINGS ]\n\n"
                "HOST        : 127.0.0.1\n"
                "WEB PORT    : 8080\n"
                "THEME       : DEFAULT\n"
                "ACCENT      : CYAN / PURPLE / GREEN\n"
                "ERROR       : RED\n\n"
                "These settings currently control the UI only.\n\n"
                "[ ESC ] BACK",
                id="detail",
            ),
            Footer(),
        )

    def action_back(self) -> None:
        self.app.pop_screen()


class StrikerTUI(App):
    CSS_PATH = "tui.tcss"
    TITLE = "STRIKER"

    def on_mount(self) -> None:
        self.push_screen(Home())


def run_tui():
    StrikerTUI().run()
