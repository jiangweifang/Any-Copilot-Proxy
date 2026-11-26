from __future__ import annotations

import json
import os
from pathlib import Path
import re
import struct
import sys
from typing import BinaryIO

from ..config import InstanceLock, UserStore, atomic_write

HOST_NAME = "io.github.copilot_proxy.m365"
MAX_MESSAGE_BYTES = 32 * 1024
LENGTH = struct.Struct("=I")
REGISTRY_PATH = rf"Software\Microsoft\Edge\NativeMessagingHosts\{HOST_NAME}"


def host_executable() -> Path:
    return Path(sys.executable).with_name("copilot-proxy-native.exe" if sys.platform == "win32" else "copilot-proxy-native")


def metadata_path() -> Path:
    return host_executable().with_name("copilot-proxy-native.json")


def manifest_path(store: UserStore) -> Path:
    if sys.platform == "win32":
        return store.directory / "native-manifest.json"
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Microsoft Edge"
    elif sys.platform == "linux":
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "microsoft-edge"
    else:
        raise ValueError("Native Messaging supports Windows, macOS and Linux desktops only.")
    return base / "NativeMessagingHosts" / f"{HOST_NAME}.json"


def registry_target() -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
            value, kind = winreg.QueryValueEx(key, "")
            if kind != winreg.REG_SZ or not isinstance(value, str):
                raise ValueError("Invalid existing Native Host registration.")
            return value
    except FileNotFoundError:
        return None


def register_registry(path: Path):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(path))


def unregister_registry():
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH)
    except FileNotFoundError:
        pass


def read_document(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError):
        raise ValueError("Invalid Native Host configuration; it was not overwritten.") from None
    if not isinstance(data, dict):
        raise ValueError("Invalid Native Host configuration; it was not overwritten.")
    return data


def register(store: UserStore, extension_id: str) -> dict:
    if not re.fullmatch(r"[a-p]{32}", extension_id):
        raise ValueError("Enter the 32-letter extension ID shown by edge://extensions.")
    executable = host_executable()
    if not executable.is_file():
        raise ValueError("Native Host executable is missing. Reinstall this project's Python package first.")
    store.ensure()
    manifest = manifest_path(store)
    metadata = {"root": str(store.root), "extensionId": extension_id}
    document = {"name": HOST_NAME, "description": "Copilot Proxy M365 authorization bridge",
                "path": str(executable), "type": "stdio",
                "allowed_origins": [f"chrome-extension://{extension_id}/"]}
    record = {"manifest": str(manifest), "document": document, "metadata": metadata}
    for old, expected in ((read_document(manifest), document), (read_document(metadata_path()), metadata),
                          (store.read("native-registration"), record)):
        if old is not None and old != expected:
            raise ValueError("Native Host registration belongs to another installation or was modified. Do not overwrite it.")
    if sys.platform == "win32":
        target = registry_target()
        if target is not None and target != str(manifest):
            raise ValueError("Edge already has a Native Host registered by another installation.")
    # Keep an ownership record even if a later registration step fails.
    store.write("native-registration", record)
    atomic_write(metadata_path(), (json.dumps(metadata) + "\n").encode())
    manifest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(manifest, (json.dumps(document, indent=2) + "\n").encode())
    if sys.platform == "win32":
        register_registry(manifest)
    return {"registered": True, "browser": "Microsoft Edge", "extensionId": extension_id}


def unregister(store: UserStore) -> dict:
    record = store.read("native-registration")
    if record is None:
        return {"registered": False}
    manifest = manifest_path(store)
    if (not isinstance(record, dict) or record.get("manifest") != str(manifest)
            or not isinstance(record.get("metadata"), dict) or record["metadata"].get("root") != str(store.root)):
        raise ValueError("Cannot verify Native Host registration ownership.")
    for path, key in ((manifest, "document"), (metadata_path(), "metadata")):
        actual = read_document(path)
        if actual is not None and actual != record.get(key):
            raise ValueError("Native Host configuration was modified; cleanup was not performed.")
    if sys.platform == "win32":
        target = registry_target()
        if target is not None and target != str(manifest):
            raise ValueError("Native Host registry belongs to another installation; cleanup was not performed.")
        unregister_registry()
    manifest.unlink(missing_ok=True)
    metadata_path().unlink(missing_ok=True)
    store.delete("native-registration")
    return {"registered": False}


def read_exact(source: BinaryIO, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = source.read(size - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def read_message(source: BinaryIO) -> dict | None:
    header = read_exact(source, LENGTH.size)
    if not header:
        return None
    if len(header) != LENGTH.size:
        raise ValueError("Incomplete Native Messaging header.")
    size = LENGTH.unpack(header)[0]
    if not 0 < size <= MAX_MESSAGE_BYTES:
        raise ValueError("Native Messaging payload must be between 1 byte and 32 KiB.")
    body = read_exact(source, size)
    if len(body) != size:
        raise ValueError("Incomplete Native Messaging body.")
    try:
        value = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ValueError("Invalid Native Messaging JSON.") from None
    if not isinstance(value, dict):
        raise ValueError("Native Messaging payload must be an object.")
    return value


def write_message(destination: BinaryIO, value: dict):
    body = json.dumps(value, ensure_ascii=True).encode("utf-8")
    if len(body) > MAX_MESSAGE_BYTES:
        raise ValueError("Native Messaging response exceeds 32 KiB.")
    destination.write(LENGTH.pack(len(body)) + body)
    destination.flush()


def dispatch(store: UserStore, message: dict) -> dict:
    from .auth import M365Auth
    from ..runtime import CONTROL_UNAVAILABLE, control_request
    action = message.get("action")
    fields = {"status": {"action"}, "connect": {"action"}, "capture": {"action", "profile", "linkId"},
              "disconnect": {"action", "linkId"}}
    if not isinstance(action, str) or action not in fields or set(message) - {"port"} != fields[action]:
        raise ValueError("Unsupported Native Messaging operation.")
    port = message.get("port", 0)
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Control port must be 0 (automatic) or an integer between 1 and 65535.")
    request = {"command": {"status": "m365-status", "connect": "m365-connect", "capture": "m365-capture",
                           "disconnect": "m365-disconnect"}[action]}
    if action == "capture":
        request["profile"] = message["profile"]
    if action in ("capture", "disconnect"):
        request["linkId"] = message["linkId"]
    try:
        options = {"expected_port": port} if port else {}
        return {**control_request(store, request, timeout=15, **options), "serviceRunning": True}
    except ConnectionError as error:
        if str(error) != CONTROL_UNAVAILABLE:
            raise
        # Share the service lock so offline writes cannot race a starting gateway.
        lock = InstanceLock(store)
        if not lock.acquire():
            raise ConnectionError(CONTROL_UNAVAILABLE) from None
        try:
            auth = M365Auth(store)
            if action == "connect":
                result = auth.connect()
            elif action == "capture":
                result = auth.capture(message["profile"], message["linkId"])
            elif action == "disconnect":
                auth.check_link(message["linkId"])
                result = auth.disconnect()
            else:
                result = auth.status()
            return {**result, "serviceRunning": False}
        finally:
            lock.close()


def run(source: BinaryIO, destination: BinaryIO, store: UserStore):
    while True:
        message = read_message(source)
        if message is None:
            return
        try:
            result = dispatch(store, message)
            reply = {"ok": True, "result": result}
        except (OSError, ConnectionError):
            reply = {"ok": False, "error": "Cannot reach this installation. Open Copilot Proxy or run serve first."}
        except ValueError as error:
            reply = {"ok": False, "error": str(error)}
        write_message(destination, reply)


def main():
    try:
        metadata = read_document(metadata_path())
        if (not metadata or set(metadata) != {"root", "extensionId"} or not isinstance(metadata["root"], str)
                or not isinstance(metadata["extensionId"], str) or not re.fullmatch(r"[a-p]{32}", metadata["extensionId"])):
            raise ValueError("Native Host installation metadata is missing or invalid.")
        caller = sys.argv[1] if len(sys.argv) > 1 else ""
        if caller != f"chrome-extension://{metadata['extensionId']}/":
            raise ValueError("Native Host caller is not the registered extension.")
        store = UserStore(Path(metadata["root"]))
        record = store.read("native-registration")
        if not record or record.get("metadata") != metadata:
            raise ValueError("Native Host ownership could not be verified.")
        if sys.platform == "win32":
            import msvcrt
            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        run(sys.stdin.buffer, sys.stdout.buffer, store)
        return 0
    except (OSError, ValueError, TypeError):
        print("Native Host stopped: invalid registration, caller, message or pipe. No credentials were logged.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
