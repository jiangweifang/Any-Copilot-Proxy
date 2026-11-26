"""Explicit, non-generating M365 authorization diagnostics for the desktop integration."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from ..config import UserStore
from .native_host import dispatch as native_request, register, unregister
from ..runtime import control_request, safe_error


def main():
    parser = argparse.ArgumentParser(description="M365 authorization and browser bridge management.")
    parser.add_argument("command", choices=("register", "unregister", "status", "verify", "disconnect"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--extension-id", help="ID of this project's unpacked extension, from edge://extensions")
    args = parser.parse_args()
    store = UserStore(args.root)
    try:
        if args.command == "register":
            if not args.extension_id:
                parser.error("register requires --extension-id")
            result = register(store, args.extension_id)
        elif args.command == "unregister":
            result = unregister(store)
        else:
            if args.command == "disconnect":
                print("Remove this tool's M365 authorization? Your browser's Microsoft login is not changed.")
                try:
                    answer = input("Continue? [y/N]: ")
                except EOFError:
                    answer = ""
                if answer.strip().lower() not in ("y", "yes"):
                    print("Cancelled.")
                    return 0
            if args.command in ("status", "disconnect"):
                result = native_request(store, {"action": "status"})
                if args.command == "disconnect" and result["linked"]:
                    result = native_request(store, {"action": "disconnect", "linkId": result["linkId"]})
            else:
                result = control_request(store, {"command": "m365-verify"})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(safe_error(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
