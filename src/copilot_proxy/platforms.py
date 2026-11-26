from __future__ import annotations

import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

from .config import UserStore


def launch_command(root: Path, desktop: bool) -> list[str]:
    executable = Path(sys.executable)
    if desktop and sys.platform == "win32" and executable.with_name("pythonw.exe").exists():
        executable = executable.with_name("pythonw.exe")
    return [str(executable), "-m", "copilot_proxy.desktop" if desktop else "copilot_proxy.cli",
            *([] if desktop else ["serve"]), "--root", str(root)]


def linux_command(command: list[str], desktop: bool) -> str:
    def escape(value):
        if any(c in value for c in "\r\n\0"):
            raise ValueError("Invalid launch argument.")
        value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        if desktop:
            value = value.replace("$", "\\$").replace("`", "\\`")
        return f'"{value}"'
    return " ".join(escape(value) for value in command)


def remove_legacy_shortcut(store: UserStore):
    if sys.platform != "win32":
        return
    script = r"""
    $ErrorActionPreference = 'Stop'
    $path = Join-Path ([Environment]::GetFolderPath('Startup')) 'Github-Copilot-Proxy.lnk'
    if (Test-Path -LiteralPath $path) {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($path)
        if ($link.Arguments.Contains((Join-Path $env:COPILOT_MIGRATION_ROOT 'tray.ps1')) -or
            $link.Arguments.Contains((Join-Path $env:COPILOT_MIGRATION_ROOT 'start.ps1'))) {
            Remove-Item -LiteralPath $path
        }
    }
    """
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                   env={**os.environ, "COPILOT_MIGRATION_ROOT": str(store.root)}, check=True,
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def install_startup(store: UserStore, desktop: bool) -> str:
    store.ensure()
    command = launch_command(store.root, desktop)
    name = "GithubCopilotProxy-" + store.id[:12]
    if sys.platform == "win32":
        import winreg
        content = subprocess.list2cmdline(command)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, content)
        store.write("autostart", {"kind": "windows", "location": name, "content": content})
        remove_legacy_shortcut(store)
        return "Login startup enabled for the current Windows user."
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"com.githubcopilotproxy.{store.id[:12]}.plist"
        content = plistlib.dumps({"Label": f"com.githubcopilotproxy.{store.id[:12]}", "ProgramArguments": command, "RunAtLoad": True}).decode()
        kind = "macos"
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        if desktop:
            path = config / "autostart" / f"{name}.desktop"
            content = f"[Desktop Entry]\nType=Application\nName=Github Copilot Proxy\nExec={linux_command(command, True)}\nTerminal=false\n"
            kind = "linux-desktop"
        else:
            path = config / "systemd" / "user" / f"{name}.service"
            content = f"[Unit]\nDescription=Github Copilot Proxy\nAfter=network-online.target\n\n[Service]\nExecStart={linux_command(command, False)}\nRestart=on-failure\n\n[Install]\nWantedBy=default.target\n"
            kind = "systemd"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    store.write("autostart", {"kind": kind, "location": str(path), "content": content})
    if kind == "systemd":
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
        subprocess.run(["systemctl", "--user", "enable", path.name], check=True, timeout=15)
    return "Login startup enabled for the current user."


def startup_enabled(store: UserStore) -> bool:
    entry = store.read("autostart")
    if not entry:
        return False
    if entry["kind"] == "windows":
        if sys.platform != "win32" or entry["location"] != "GithubCopilotProxy-" + store.id[:12]:
            raise ValueError("Startup ownership mismatch.")
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                value, _ = winreg.QueryValueEx(key, entry["location"])
        except FileNotFoundError:
            return False
        if value != entry["content"]:
            raise ValueError("Startup entry was modified; check its ownership before changing it.")
        return True
    if entry["kind"] not in ("macos", "linux-desktop", "systemd"):
        raise ValueError("Unknown startup registration type.")
    path = Path(entry["location"])
    if not path.exists():
        return False
    if path.read_text(encoding="utf-8") != entry["content"]:
        raise ValueError("Startup entry was modified; check its ownership before changing it.")
    if entry["kind"] == "systemd":
        result = subprocess.run(["systemctl", "--user", "is-enabled", path.name],
                                capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return True
        if result.stdout.strip() in ("disabled", "masked", "not-found"):
            return False
        raise RuntimeError("Cannot read systemd user startup status.")
    return True


def remove_startup(store: UserStore):
    entry = store.read("autostart")
    if entry:
        if entry["kind"] == "windows" and sys.platform == "win32":
            import winreg
            if entry["location"] != "GithubCopilotProxy-" + store.id[:12]:
                raise ValueError("Startup ownership mismatch.")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                try:
                    value, _ = winreg.QueryValueEx(key, entry["location"])
                except FileNotFoundError:
                    value = None
                if value is not None:
                    if value != entry["content"]:
                        raise ValueError("Startup entry was modified; it was not removed.")
                    winreg.DeleteValue(key, entry["location"])
        elif entry["kind"] != "windows":
            path = Path(entry["location"])
            if path.exists():
                if path.read_text(encoding="utf-8") != entry["content"]:
                    raise ValueError("Startup file was modified; it was not removed.")
                if entry["kind"] == "systemd":
                    subprocess.run(["systemctl", "--user", "disable", path.name], check=True, timeout=15)
                path.unlink()
                if entry["kind"] == "systemd":
                    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
        store.delete("autostart")
    remove_legacy_shortcut(store)


def clean_installation(store: UserStore):
    from .m365_copilot.native_host import unregister
    unregister(store)
    remove_startup(store)
    store.remove_all()
    (store.root / "adapter-config.json").unlink(missing_ok=True)
    for name in ("proxy.log", "proxy-error.log", "adapter.log", "adapter-error.log", "service.log", "service-error.log",
                 "launcher.log", "launcher-error.log", "tray-error.log"):
        (store.root / name).unlink(missing_ok=True)
    legacy = store.root / "node_modules"
    if legacy.exists():
        shutil.rmtree(legacy)
    # The active Python environment cannot be removed while its process is running.
    return f"Runtime data and startup removed. Exit this app, then manually delete {store.root} (including .venv, source and .git)."
