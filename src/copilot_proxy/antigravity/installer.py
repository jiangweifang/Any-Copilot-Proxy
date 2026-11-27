from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict, dataclass
import hashlib
import logging
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from ..network import Network, UpstreamError

log = logging.getLogger(__name__)

MANIFEST_BASE_URL = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"
CHUNK_SIZE = 65536


def detect_platform() -> str:
    system = sys.platform
    machine = (os.environ.get("PROCESSOR_ARCHITEW6432") or platform.machine() or "").lower()
    if machine in ("amd64", "x86_64", "x64"):
        arch = "amd64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        raise UpstreamError(400, f"Unsupported CPU architecture for Antigravity CLI: {machine or 'unknown'}.")
    if system == "win32":
        os_name = "windows"
    elif system == "darwin":
        os_name = "darwin"
    elif system.startswith("linux"):
        os_name = "linux"
    else:
        raise UpstreamError(400, f"Unsupported operating system for Antigravity CLI: {system}.")
    return f"{os_name}_{arch}"


def default_install_dir() -> Path:
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else (Path.home() / "AppData" / "Local")
        return base / "agy" / "bin"
    return Path.home() / ".local" / "bin"


def default_binary_path(install_dir: Path | None = None) -> Path:
    directory = install_dir or default_install_dir()
    return directory / ("agy.exe" if sys.platform == "win32" else "agy")


@dataclass
class InstallProgress:
    state: str = "idle"  # idle | running | complete | failed | cancelled
    stage: str = "idle"  # idle | manifest | downloading | verifying | extracting | installing | configuring | complete | failed | cancelled
    version: str = ""
    downloadedBytes: int = 0
    totalBytes: int = 0
    percent: int = 0
    installedPath: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AntigravityInstaller:
    def __init__(
        self,
        *,
        manifest_base_url: str = MANIFEST_BASE_URL,
        install_dir: Path | None = None,
        run_post_install: bool = True,
    ):
        self.manifest_base_url = manifest_base_url.rstrip("/")
        self.install_dir = install_dir
        self.run_post_install = run_post_install
        self.progress = InstallProgress()

    def status(self) -> dict[str, Any]:
        return self.progress.to_dict()

    def _update(self, **fields: Any) -> None:
        for key, value in fields.items():
            setattr(self.progress, key, value)

    async def _open_following_redirects(self, network: Network, url: str, *, total_timeout: float, max_redirects: int = 5):
        current = url
        initial_host = urlparse(url).hostname
        timeout = aiohttp.ClientTimeout(total=total_timeout, sock_connect=20)
        for _ in range(max_redirects + 1):
            cm = network.request("GET", current, timeout=timeout)
            response = await cm.__aenter__()
            if response.status in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                next_url = urljoin(current, response.headers["Location"])
                await cm.__aexit__(None, None, None)
                parsed = urlparse(next_url)
                if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname == initial_host):
                    raise UpstreamError(502, "Antigravity CLI download redirected to an insecure URL.")
                current = next_url
                continue
            return cm, response
        raise UpstreamError(502, "Antigravity CLI download exceeded maximum redirects.")

    async def install(self, network: Network, *, target_dir: Path | None = None) -> dict[str, Any]:
        dest_dir = target_dir or self.install_dir or default_install_dir()
        dest_binary = default_binary_path(dest_dir)
        self.progress = InstallProgress(state="running", stage="manifest", percent=0)

        platform_id = detect_platform()
        manifest_url = f"{self.manifest_base_url}/manifests/{platform_id}.json"

        staging_dir = Path(tempfile.mkdtemp(prefix="agy-install-"))
        try:
            log.info("Fetching Antigravity CLI manifest: platform=%s", platform_id)
            cm, response = await self._open_following_redirects(network, manifest_url, total_timeout=30)
            try:
                if response.status >= 300:
                    raise UpstreamError(response.status, f"Failed to fetch Antigravity CLI manifest (HTTP {response.status}).")
                manifest = await response.json(content_type=None)
            finally:
                await cm.__aexit__(None, None, None)

            if not isinstance(manifest, dict):
                raise UpstreamError(502, "Antigravity CLI release manifest is invalid.")
            version = str(manifest.get("version") or "").strip()
            url = str(manifest.get("url") or "").strip()
            expected_sha512 = str(manifest.get("sha512") or "").strip().lower()
            if not url or not expected_sha512 or not url.startswith(("https://", "http://")):
                raise UpstreamError(502, "Antigravity CLI release manifest is missing download URL or SHA-512 checksum.")

            is_tar_gz = ".tar.gz" in url.lower()
            staged_download = staging_dir / ("agy.tar.gz" if is_tar_gz else dest_binary.name)
            self._update(stage="downloading", version=version, downloadedBytes=0, totalBytes=0, percent=0)

            log.info("Downloading Antigravity CLI version=%s", version or "unknown")
            hasher = hashlib.sha512()
            downloaded = 0
            cm, response = await self._open_following_redirects(network, url, total_timeout=600)
            try:
                if response.status >= 300:
                    raise UpstreamError(response.status, f"Failed to download Antigravity CLI (HTTP {response.status}).")
                raw_len = response.headers.get("Content-Length")
                total = int(raw_len) if raw_len and raw_len.isdigit() else 0
                self._update(totalBytes=total)
                with staged_download.open("wb") as output:
                    async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                        if not chunk:
                            continue
                        output.write(chunk)
                        hasher.update(chunk)
                        downloaded += len(chunk)
                        percent = min(90, int(downloaded * 90 / total)) if total > 0 else 0
                        self._update(downloadedBytes=downloaded, percent=percent)
            finally:
                await cm.__aexit__(None, None, None)

            self._update(stage="verifying", percent=92)
            actual_sha512 = hasher.hexdigest().lower()
            if actual_sha512 != expected_sha512:
                raise UpstreamError(502, "Downloaded Antigravity CLI failed SHA-512 checksum verification.")

            extracted_binary = staged_download
            if is_tar_gz:
                self._update(stage="extracting", percent=94)
                extracted_binary = await asyncio.to_thread(self._extract_tar_gz, staged_download, staging_dir)

            self._update(stage="installing", percent=96)
            dest_dir.mkdir(parents=True, exist_ok=True)
            staged_final = dest_dir / f".{dest_binary.name}.tmp.{os.getpid()}"
            try:
                await asyncio.to_thread(shutil.copy2, extracted_binary, staged_final)
                if sys.platform != "win32":
                    with suppress(OSError):
                        mode = staged_final.stat().st_mode
                        staged_final.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                os.replace(staged_final, dest_binary)
            finally:
                staged_final.unlink(missing_ok=True)

            if sys.platform == "darwin":
                with suppress(OSError):
                    proc = await asyncio.create_subprocess_exec(
                        "xattr", "-d", "com.apple.quarantine", str(dest_binary),
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    with suppress(TimeoutError):
                        async with asyncio.timeout(5):
                            await proc.wait()

            if self.run_post_install:
                self._update(stage="configuring", percent=98)
                await self._run_native_setup(dest_binary, target_dir)

            self._update(
                state="complete",
                stage="complete",
                percent=100,
                installedPath=str(dest_binary.resolve()),
                error="",
            )
            log.info("Antigravity CLI installed successfully: version=%s path=%s", version or "unknown", dest_binary)
            return self.status()
        except asyncio.CancelledError:
            self._update(state="cancelled", stage="cancelled", error="Installation cancelled.")
            raise
        except Exception as error:
            self._update(state="failed", stage="failed", error=str(error) or type(error).__name__)
            raise
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    @staticmethod
    def _extract_tar_gz(archive_path: Path, staging_dir: Path) -> Path:
        with tarfile.open(archive_path, "r:gz") as tar:
            target_member = None
            for member in tar.getmembers():
                base = Path(member.name).name
                if member.isfile() and base in ("antigravity", "agy"):
                    target_member = member
                    break
            if target_member is None:
                raise UpstreamError(502, "Antigravity CLI archive did not contain the executable binary.")
            extracted_path = staging_dir / "agy_extracted"
            source = tar.extractfile(target_member)
            if source is None:
                raise UpstreamError(502, "Failed to read binary from Antigravity CLI archive.")
            with source, extracted_path.open("wb") as output:
                shutil.copyfileobj(source, output)
            return extracted_path

    @staticmethod
    async def _run_native_setup(binary: Path, custom_dir: Path | None) -> None:
        cmd = [str(binary), "install"]
        if custom_dir is not None:
            cmd.extend(["--dir", str(custom_dir)])
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
            async with asyncio.timeout(20):
                await proc.wait()
        except (OSError, TimeoutError):
            log.debug("Native 'agy install' setup step skipped or timed out; binary is already placed on disk.")
