from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import signal
import sys
import time

from .config import UserStore
from .platforms import clean_installation, install_startup
from .runtime import Runtime, control_request, safe_error, serve


async def run_server(store: UserStore):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    terminating = False

    def terminate():
        nonlocal terminating
        if not terminating:
            terminating = True
            task.cancel()

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, terminate)
    try:
        await serve(store)
    except asyncio.CancelledError:
        if not terminating:
            raise
    finally:
        if sys.platform != "win32":
            loop.remove_signal_handler(signal.SIGTERM)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="GitHub Copilot Proxy: desktop and headless gateway")
    result.add_argument("command", nargs="?", default="status", choices=("serve", "login", "logout", "status", "start", "stop", "shutdown", "models",
                                                                      "proxy-get", "proxy-set", "proxy-test", "install", "uninstall"))
    result.add_argument("--root", type=Path, default=Path.cwd(), help="Installation directory containing adapter-config.json")
    return result


async def login_local(runtime: Runtime):
    await runtime.begin_login()
    print(f"Open {runtime.device.verification_uri} and enter {runtime.device.user_code}", flush=True)
    await runtime.login_task
    if runtime.login_state != "complete":
        raise ValueError(runtime.message)
    username = runtime.status().get("username")
    print(f"Signed in as {username}." if username else "GitHub Copilot login completed.", flush=True)


async def dispatch(command: str, store: UserStore, request: dict):
    if command == "logout":
        try:
            answer = input("Sign out and stop this tool's active requests? [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Logout cancelled.")
            return
    try:
        await asyncio.to_thread(control_request, store, {"command": "status"}, 3)
        remote = True
    except (ConnectionError, OSError):
        remote = False
    if command == "uninstall":
        print("Remove this installation's login, proxy passwords, startup, cache and logs?")
        if input("Type UNINSTALL to confirm: ") != "UNINSTALL":
            print("Cancelled.")
            return
        if remote:
            await asyncio.to_thread(control_request, store, {"command": "shutdown"})
            for _ in range(30):
                if not store.read("control"):
                    break
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("Service did not stop. Cleanup was not performed.")
        if store.read("control", {}).get("key"):
            raise RuntimeError("Stop the legacy Node service before uninstalling.")
        print(await asyncio.to_thread(clean_installation, store))
        return
    if remote and command != "install":
        result = await asyncio.to_thread(control_request, store, request)
        if command == "login":
            print(f"Open {result['verificationUri']} and enter {result['deviceCode']}", flush=True)
            try:
                while result["loginState"] == "pending":
                    await asyncio.sleep(0.5)
                    result = await asyncio.to_thread(control_request, store, {"command": "status"})
                if result["loginState"] != "complete":
                    raise ValueError(result["message"])
            except asyncio.CancelledError:
                await asyncio.to_thread(control_request, store, {"command": "cancel-login"})
                raise
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if command in ("start", "shutdown"):
        raise ConnectionError("Run 'serve' or open the desktop application first.")
    if command not in ("status", "proxy-get"):
        store.ensure()
    runtime = Runtime(store)
    try:
        if command in ("login", "install"):
            if remote:
                raise RuntimeError("Stop Github Copilot Proxy before installing again.")
            await login_local(runtime)
            if command == "install":
                print(await asyncio.to_thread(install_startup, store, False))
                print("Starting headless service. Ctrl+C stops this instance.")
                await serve(store)
        else:
            print(json.dumps(await runtime.execute(request), ensure_ascii=False, indent=2))
    finally:
        await runtime.close()


def main():
    args = parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = UserStore(args.root)
    try:
        if args.command == "serve":
            asyncio.run(run_server(store))
        else:
            request = {"command": args.command}
            if args.command in ("proxy-set", "proxy-test"):
                value = sys.stdin.read(65537)
                if len(value) > 65536:
                    raise ValueError("Proxy input too large.")
                request["proxy"] = json.loads(value)
            asyncio.run(dispatch(args.command, store, request))
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Stopped.", file=sys.stderr)
        return 130
    except Exception as error:
        print(safe_error(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
