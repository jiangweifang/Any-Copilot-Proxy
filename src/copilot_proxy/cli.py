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
    result = argparse.ArgumentParser(description="Copilot Proxy: desktop and headless multi-channel AI gateway")
    result.add_argument("command", nargs="?", default="status", choices=("serve", "login", "logout", "status", "start", "stop", "shutdown", "models",
                                                                      "proxy-get", "proxy-set", "proxy-test", "install", "uninstall",
                                                                      "agy-login", "agy-logout", "agy-status", "agy-install"))
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


async def agy_login_local(runtime: Runtime):
    print("Starting Antigravity OAuth sign-in (oauth-personal)...", flush=True)
    await runtime.begin_agy_login()
    if runtime.agy_login_task is not None:
        await runtime.agy_login_task
    agy = runtime.status()["antigravity"]
    if agy["loginState"] != "complete":
        raise ValueError(agy["lastError"] or "Antigravity login failed.")
    account = agy.get("account")
    print(f"Signed in to Antigravity as {account}." if account else "Antigravity login completed.", flush=True)


def _format_install_line(progress: dict) -> str:
    stage = progress.get("stage", "")
    pct = progress.get("percent", 0)
    downloaded = progress.get("downloadedBytes", 0)
    total = progress.get("totalBytes", 0)
    if stage == "downloading" and total > 0:
        return f"[{pct:3d}%] Downloading {progress.get('version', '')} ({downloaded / 1048576:.1f} / {total / 1048576:.1f} MB)"
    if stage == "downloading":
        return f"Downloading {progress.get('version', '')} ({downloaded / 1048576:.1f} MB)"
    return f"[{pct:3d}%] {stage}..."


async def agy_install_local(runtime: Runtime):
    await runtime.begin_agy_install()
    last = ""
    while runtime.agy_install_task is not None and not runtime.agy_install_task.done():
        line = _format_install_line(runtime.agy_installer.status())
        if line != last:
            print(line, flush=True)
            last = line
        await asyncio.sleep(0.25)
    if runtime.agy_install_task is not None:
        await runtime.agy_install_task
    info = runtime.agy_installer.status()
    if info["state"] != "complete":
        raise ValueError(info.get("error") or "Antigravity CLI installation failed.")
    print(f"Antigravity CLI installed at {info['installedPath']}.", flush=True)


async def dispatch(command: str, store: UserStore, request: dict):
    if command in ("logout", "agy-logout"):
        prompt = ("Sign out of Antigravity and stop its active requests? [y/N]: "
                  if command == "agy-logout" else
                  "Sign out of GitHub and stop its active requests? [y/N]: ")
        try:
            answer = input(prompt)
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
        elif command == "agy-login":
            print("Waiting for Antigravity OAuth sign-in (oauth-personal)...", flush=True)
            try:
                while result["antigravity"]["loginState"] == "pending":
                    await asyncio.sleep(0.5)
                    result = await asyncio.to_thread(control_request, store, {"command": "status"})
                if result["antigravity"]["loginState"] != "complete":
                    raise ValueError(result["antigravity"]["lastError"] or "Antigravity login failed.")
            except asyncio.CancelledError:
                await asyncio.to_thread(control_request, store, {"command": "agy-cancel-login"})
                raise
        elif command == "agy-install":
            last = ""
            try:
                while result["antigravity"]["install"]["state"] == "running":
                    line = _format_install_line(result["antigravity"]["install"])
                    if line != last:
                        print(line, flush=True)
                        last = line
                    await asyncio.sleep(0.25)
                    result = await asyncio.to_thread(control_request, store, {"command": "status"})
                info = result["antigravity"]["install"]
                if info["state"] != "complete":
                    raise ValueError(info.get("error") or "Antigravity CLI installation failed.")
            except asyncio.CancelledError:
                await asyncio.to_thread(control_request, store, {"command": "agy-cancel-install"})
                raise
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if command in ("start", "shutdown"):
        raise ConnectionError("Run 'serve' or open the desktop application first.")
    if command not in ("status", "proxy-get", "agy-status"):
        store.ensure()
    runtime = Runtime(store)
    try:
        if command in ("login", "install"):
            if remote:
                raise RuntimeError("Stop Copilot Proxy before installing again.")
            await login_local(runtime)
            if command == "install":
                print(await asyncio.to_thread(install_startup, store, False))
                print("Starting headless service. Ctrl+C stops this instance.")
                await serve(store)
        elif command == "agy-login":
            await agy_login_local(runtime)
        elif command == "agy-install":
            await agy_install_local(runtime)
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
