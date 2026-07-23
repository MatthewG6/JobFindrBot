import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.launch_agent import LaunchAgentManager


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage the local JobFindrBot Discord LaunchAgent.",
    )
    parser.add_argument(
        "action",
        choices=["install", "restart", "status", "uninstall"],
    )
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> None:
    args = parse_args(arguments)
    manager = LaunchAgentManager(PROJECT_ROOT)

    if args.action == "install":
        manager.install()
        print(f"Installed and started {manager.service_target}")
    elif args.action == "restart":
        manager.restart()
        print(f"Restarted {manager.service_target}")
    elif args.action == "status":
        print(manager.status(), end="")
    else:
        manager.uninstall()
        print(f"Uninstalled {manager.service_target}")


if __name__ == "__main__":
    main()
