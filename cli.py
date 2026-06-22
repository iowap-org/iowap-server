"""Recovery CLI for AI-Relay-Service.

This tool is intentionally minimal and only touches the local SQLite database.
It never accepts passwords on the command line to avoid leaking them into
shell history or process listings.

Typical recovery flow when a human admin forgot their password:

1. Stop the relay server.
2. Run: python -m relay_server.cli --db-path ~/.relay/server.db enable-recovery
3. Deactivate the admin user(s) so no active human admin exists.
4. Start the relay server again.
5. Open the dashboard and log in with the master seed.
6. Create a new human admin via the dashboard UI.
7. Once the new admin exists, master-seed login closes automatically again.
"""

import argparse
import sys
from pathlib import Path

from relay_server.core.db import init_db_for_path
from relay_server.core.users import list_users, set_user_active


def _list_human_admins():
    """Return active human users that belong to the admin group."""
    admins = []
    for user in list_users():
        if "admin" in user.get("groups", []) and user.get("is_active"):
            admins.append(user)
    return admins


def _prompt_yes_no(question: str, default: bool = False) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{question} [{default_text}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer 'yes' or 'no'.")


def _cmd_enable_recovery(args) -> int:
    init_db_for_path(args.db_path)
    admins = _list_human_admins()
    if not admins:
        print("No active human admin users found. Master-seed login should already be enabled.")
        return 0

    print(f"Found {len(admins)} active human admin user(s):")
    for admin in admins:
        print(f"  - {admin['username']} ({admin['user_id']})")

    if args.all:
        for admin in admins:
            set_user_active(admin["user_id"], False)
            print(f"Deactivated {admin['username']}.")
        print("Recovery mode enabled. Start the server and log in with the master seed.")
        return 0

    for admin in admins:
        if _prompt_yes_no(f"Deactivate admin user '{admin['username']}'?", default=False):
            set_user_active(admin["user_id"], False)
            print(f"Deactivated {admin['username']}.")
        else:
            print(f"Kept {admin['username']} active.")

    remaining = _list_human_admins()
    if remaining:
        print(f"\nWarning: {len(remaining)} admin(s) still active; master-seed login remains blocked.")
        return 1
    print("\nRecovery mode enabled. Start the server and log in with the master seed.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AI-Relay-Service recovery CLI")
    parser.add_argument("--db-path", required=True, type=Path, help="Path to the SQLite database file")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("enable-recovery", help="Deactivate human admins to enable master-seed login")
    rec.add_argument("--all", action="store_true", help="Deactivate all human admins without prompting")
    rec.set_defaults(func=_cmd_enable_recovery)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
