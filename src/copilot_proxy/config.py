from __future__ import annotations

import csv
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from typing import Any


def atomic_write(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_project_config(root: Path) -> dict:
    path = root / "adapter-config.json"
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("Listener configuration must be a JSON object.")
    return data


@dataclass(frozen=True)
class ListenerSettings:
    host: str = "127.0.0.1"
    port: int = 4140
    bridge_port: int = 4141
    models: tuple[dict, ...] = ()

    @classmethod
    def load(cls, root: Path) -> ListenerSettings:
        return cls.from_data(read_project_config(root))

    @classmethod
    def from_data(cls, data: dict) -> ListenerSettings:
        if not isinstance(data, dict):
            raise ValueError("Listener configuration must be a JSON object.")
        host = data.get("host", "127.0.0.1")
        try:
            valid_host = isinstance(host, str) and str(ipaddress.IPv4Address(host)) == host
        except ipaddress.AddressValueError:
            valid_host = False
        if not valid_host:
            raise ValueError("Host must be a dotted IPv4 address.")
        ports = [data.get("port", 4140), data.get("bridgePort", 4141)]
        if any(type(p) is not int or not 1 <= p <= 65535 for p in ports) or ports[0] == ports[1]:
            raise ValueError("Ports must be distinct integers between 1 and 65535.")
        M365Settings.from_data(data)
        AntigravitySettings.from_data(data)
        LoggingSettings.from_data(data)
        models = data.get("models", [])
        if not isinstance(models, list) or any(not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"].strip() for m in models):
            raise ValueError("Model overrides must be objects with an id.")
        return cls(host, ports[0], ports[1], tuple(models))

    @property
    def address(self) -> str:
        return "127.0.0.1" if self.host == "0.0.0.0" else self.host

    @property
    def base_url(self) -> str:
        return f"http://{self.address}:{self.port}/v1"


@dataclass(frozen=True)
class LoggingSettings:
    enabled: bool = True

    @classmethod
    def load(cls, root: Path) -> LoggingSettings:
        return cls.from_data(read_project_config(root))

    @classmethod
    def from_data(cls, document: dict) -> LoggingSettings:
        data = document.get("logging", {})
        if not isinstance(data, dict) or type(data.get("enabled", True)) is not bool:
            raise ValueError("Logging settings require a boolean enabled field.")
        return cls(data.get("enabled", True))


@dataclass(frozen=True)
class M365Settings:
    enabled: bool = True
    control_port: int = 0

    @classmethod
    def load(cls, root: Path) -> M365Settings:
        return cls.from_data(read_project_config(root))

    @classmethod
    def from_data(cls, document: dict) -> M365Settings:
        data = document.get("m365", {})
        if not isinstance(data, dict) or type(data.get("enabled", True)) is not bool:
            raise ValueError("M365 synchronization settings require a boolean enabled field.")
        port = data.get("controlPort", 0)
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Control port must be 0 (automatic) or an integer between 1 and 65535.")
        if port and port in (document.get("port", 4140), document.get("bridgePort", 4141)):
            raise ValueError("Control port must differ from the API ports.")
        return cls(data.get("enabled", True), port)


@dataclass(frozen=True)
class AntigravitySettings:
    enabled: bool = False
    binary_path: str = ""
    profile_mode: str = "shared"

    @classmethod
    def load(cls, root: Path) -> AntigravitySettings:
        return cls.from_data(read_project_config(root))

    @classmethod
    def from_data(cls, document: dict) -> AntigravitySettings:
        data = document.get("antigravity", {})
        if not isinstance(data, dict):
            raise ValueError("Antigravity settings must be an object.")
        enabled = data.get("enabled", False)
        if type(enabled) is not bool:
            raise ValueError("Antigravity settings require a boolean enabled field.")
        binary_path = data.get("binaryPath", "")
        if not isinstance(binary_path, str) or "\x00" in binary_path:
            raise ValueError("Antigravity binaryPath must be a valid path string.")
        profile_mode = data.get("profileMode", "shared")
        if profile_mode not in ("shared", "isolated"):
            raise ValueError("Antigravity profileMode must be 'shared' or 'isolated'.")
        return cls(enabled, binary_path.strip(), profile_mode)


@dataclass(frozen=True)
class ProxySettings:
    type: str = "none"
    host: str = ""
    port: int = 8080
    username: str = ""
    password: str = ""

    @classmethod
    def parse(cls, data: dict[str, Any]) -> ProxySettings:
        if not isinstance(data, dict):
            raise ValueError("Proxy settings must be an object.")
        if data.get("type", "none") == "none":
            return cls()
        if data.get("type") not in ("http", "socks5"):
            raise ValueError("Select direct, HTTP or SOCKS5.")
        host, port = data.get("host"), data.get("port")
        if not isinstance(host, str) or not host or re.search(r"[\s/@?#\\]", host):
            raise ValueError("Enter a proxy hostname or IP without a URL scheme.")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Proxy port must be between 1 and 65535.")
        username, password = data.get("username", ""), data.get("password", "")
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError("Proxy credentials must be text.")
        if any(len(value.encode()) > 255 for value in (username, password)):
            raise ValueError("Proxy credentials must not exceed 255 bytes.")
        return cls(data["type"], host, port, username, password)

    def public(self) -> dict[str, Any]:
        result = asdict(self)
        del result["password"]
        result["hasPassword"] = bool(self.password)
        return result


class UserStore:
    def __init__(self, root: Path, data_root: Path | None = None):
        self.root = root.resolve()
        identity = str(self.root).lower() if sys.platform == "win32" else str(self.root)
        self.id = hashlib.sha256(identity.encode()).hexdigest()
        if data_root is None:
            if sys.platform == "win32":
                data_root = Path(os.environ["LOCALAPPDATA"])
            elif sys.platform == "darwin":
                data_root = Path.home() / "Library" / "Application Support"
            else:
                data_root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        self.directory = data_root / "GithubCopilotProxy" / self.id

    def ensure(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if sys.platform == "win32":
            result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            sid = next(csv.reader(result.stdout.strip().splitlines()))[-1]
            if not re.fullmatch(r"S-1-[\d-]+", sid):
                raise RuntimeError("Cannot determine the current user SID.")
            subprocess.run(["icacls", str(self.directory), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                           capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            self.directory.chmod(0o700)
        self.write("installation", {"project": str(self.root)})

    def read(self, name: str, default: Any = None) -> Any:
        path = self.directory / f"{name}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return default
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid {name} configuration; file was not overwritten.") from error

    def write(self, name: str, value: Any) -> None:
        atomic_write(self.directory / f"{name}.json", (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def delete(self, name: str) -> None:
        (self.directory / f"{name}.json").unlink(missing_ok=True)

    def remove_all(self) -> None:
        if not self.directory.exists():
            return
        if self.read("installation", {}).get("project") != str(self.root):
            raise ValueError("Cannot verify ownership of this user directory.")
        shutil.rmtree(self.directory)
        if not any(self.directory.parent.iterdir()):
            self.directory.parent.rmdir()


class InstanceLock:
    def __init__(self, store: UserStore, name: str = "service"):
        self.path = store.directory / f"{name}.lock"
        self.file = None

    def acquire(self) -> bool:
        self.file = self.path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.file.close()
            self.file = None
            return False

    def close(self) -> None:
        if self.file:
            self.file.close()
            self.file = None
