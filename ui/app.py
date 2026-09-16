import argparse

from config import HOST, PORT
from tui import run_tui
from web import run_web


def main():
    parser = argparse.ArgumentParser(description="STRIKER cyberpunk UI")
    parser.add_argument("--web", action="store_true", help="Start the local HTTP UI")
    parser.add_argument("--tui", action="store_true", help="Start the terminal UI")
    args = parser.parse_args()

    # Default: TUI
    if args.web:
        run_web()
    else:
        run_tui()


if __name__ == "__main__":
    main()
