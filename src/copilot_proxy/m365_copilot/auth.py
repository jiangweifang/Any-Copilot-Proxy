from __future__ import annotations

import base64
import binascii
from dataclasses import asdict, dataclass, field
import json
import logging
import re
import secrets
import time
from urllib.parse import parse_qs, unquote, urlsplit

import aiohttp

from ..config import M365Settings, UserStore
from ..network import Network, UpstreamError

log = logging.getLogger(__name__)

MAX_CAPTURE_BYTES = 32 * 1024
CHAT_HOSTS = frozenset(("substrate.office.com", "substrate.svc.cloud.microsoft"))
SCENARIOS = frozenset(("OfficeWebIncludedCopilot", "OfficeWebPaidCopilot", "OfficeWebPremiumConsumerCopilot"))
ORIGINS = frozenset(("https://m365.cloud.microsoft", "https://copilot.cloud.microsoft", "https://copilot.com", "https://www.copilot.com",
                     "https://copilot.microsoft.com", "https://www.office.com", "https://www.microsoft365.com"))
PERSONAL_TENANTS = frozenset(("9188040d-6c67-4c5b-b112-36a304b66dad", "84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa"))


def decode_part(part: str) -> dict:
    try:
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, UnicodeError, binascii.Error):
        raise ValueError("Invalid M365 token encoding.") from None
    if not isinstance(value, dict):
        raise ValueError("Invalid M365 token claims.")
    return value


def account_part(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("M365 capture is missing a valid account identity.")
    return value


@dataclass(frozen=True)
class M365Profile:
    access_token: str = field(repr=False)
    oid: str
    tid: str
    kind: str
    expires_at: int | None
    captured_at: int
    origin: str
    user_agent: str
    scenario: str
    username: str | None

    @property
    def account(self) -> tuple[str, str]:
        return self.oid, self.tid

    @classmethod
    def parse(cls, data: object) -> M365Profile:
        if not isinstance(data, dict):
            raise ValueError("M365 capture must be an object.")
        if len(json.dumps(data).encode("utf-8")) > MAX_CAPTURE_BYTES:
            raise ValueError("M365 capture exceeds 32 KiB.")
        if set(data) - {"accessToken", "endpoint", "origin", "userAgent", "capturedAt"}:
            raise ValueError("Unexpected M365 capture fields.")
        token = data.get("accessToken")
        if not isinstance(token, str) or len(token) > 16384:
            raise ValueError("Invalid M365 token length.")
        parts = token.split(".")
        if len(parts) not in (3, 5) or any(
                not re.fullmatch(r"[A-Za-z0-9_-]*" if len(parts) == 5 and index == 1 else r"[A-Za-z0-9_-]+", part)
                for index, part in enumerate(parts)):
            raise ValueError("Expected a JWT or an encrypted M365 chat token.")
        encrypted = len(parts) == 5
        header = decode_part(parts[0])
        if not isinstance(header.get("alg"), str) or header["alg"].lower() == "none":
            raise ValueError("Invalid M365 token algorithm.")
        if encrypted and not isinstance(header.get("enc"), str):
            raise ValueError("Invalid encrypted M365 token.")
        claims = {} if encrypted else decode_part(parts[1])
        endpoint = data.get("endpoint")
        socket_account = None
        scenario = None
        if endpoint is not None:
            if not isinstance(endpoint, str) or len(endpoint) > 2048:
                raise ValueError("Invalid M365 chat endpoint.")
            try:
                uri = urlsplit(endpoint)
                valid = (uri.scheme == "wss" and uri.hostname in CHAT_HOSTS and uri.port in (None, 443)
                         and not uri.username and not uri.password and not uri.fragment)
            except ValueError:
                raise ValueError("Invalid M365 chat endpoint.") from None
            match = re.fullmatch(r"/m365Copilot/Chathub\w*/([A-Za-z0-9_-]{1,128})@([A-Za-z0-9_-]{1,128})/?",
                                 unquote(uri.path), re.IGNORECASE)
            if not valid or not match:
                raise ValueError("Capture did not come from an allowed M365 chat endpoint.")
            query = parse_qs(uri.query, keep_blank_values=True)
            if set(query) - {"scenario"} or any(len(values) != 1 for values in query.values()):
                raise ValueError("Only the chat scenario may be retained in the captured endpoint.")
            scenario = query.get("scenario", [None])[0]
            if scenario is not None and scenario not in SCENARIOS:
                raise ValueError("Unsupported M365 chat scenario.")
            socket_account = match.group(1), match.group(2)
        if encrypted:
            if not socket_account:
                raise ValueError("An encrypted token requires its captured chat account address.")
            oid, tid = socket_account
            expires = None
        else:
            audience = claims.get("aud")
            scope = claims.get("scp", claims.get("scope", ""))
            substrate = isinstance(audience, str) and audience.rstrip("/") in {
                "https://substrate.office.com", "https://substrate.svc.cloud.microsoft"}
            sydney = isinstance(scope, str) and any(re.search(r"sydney|m365chat", item, re.IGNORECASE) for item in scope.split())
            if audience in ("https://graph.microsoft.com", "00000003-0000-0000-c000-000000000000") or not (substrate or sydney):
                raise ValueError("This is not a Substrate/Sydney chat token.")
            oid, tid = account_part(claims.get("oid")), account_part(claims.get("tid"))
            if socket_account and socket_account != (oid, tid):
                raise ValueError("Token identity does not match the chat address.")
            expires = claims.get("exp")
            issued = claims.get("iat")
            if type(expires) is not int or expires <= time.time() + 30:
                raise ValueError("The captured M365 token has expired or is about to expire.")
            if issued is not None and (type(issued) is not int or issued > expires):
                raise ValueError("Invalid M365 token lifetime.")
        captured = data.get("capturedAt")
        if type(captured) is not int or captured <= 0 or captured > int(time.time() * 1000) + 60000:
            raise ValueError("Invalid M365 capture timestamp.")
        kind = "personal" if encrypted or tid.lower() in PERSONAL_TENANTS else "enterprise"
        origin = data.get("origin") or ("https://copilot.com" if kind == "personal" else "https://m365.cloud.microsoft")
        if not isinstance(origin, str) or origin not in ORIGINS:
            raise ValueError("Unsupported M365 page origin.")
        agent = data.get("userAgent")
        if not isinstance(agent, str) or not 1 <= len(agent) <= 512 or any(ord(char) < 32 or ord(char) > 126 for char in agent):
            raise ValueError("Invalid browser user agent.")
        username = next((claims.get(key) for key in ("upn", "preferred_username", "unique_name")
                         if isinstance(claims.get(key), str) and 0 < len(claims[key]) <= 256), None)
        return cls(token, oid, tid, kind, expires, captured, origin, agent,
                   scenario or ("OfficeWebPremiumConsumerCopilot" if kind == "personal" else "OfficeWebIncludedCopilot"), username)


def menu_models(data: object) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("gptList"), list):
        raise ValueError("M365 did not return a model menu.")
    pending = []
    for item in data["gptList"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid M365 model menu.")
        preferences = item.get("clientPreferences", {})
        metadata = preferences.get("modelSelectorMetadata", {}) if isinstance(preferences, dict) else {}
        options = metadata.get("availableModelSelectionOptions") if isinstance(metadata, dict) else None
        if options is not None:
            if not isinstance(options, list):
                raise ValueError("Invalid M365 model options.")
            pending.extend((option, 0) for option in options)
    models = {}
    visited = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if visited > 1000 or depth > 8 or not isinstance(item, dict):
            raise ValueError("Invalid or oversized M365 model menu.")
        if "itemGroup" in item:
            if not isinstance(item["itemGroup"], list):
                raise ValueError("Invalid M365 model group.")
            pending.extend((child, depth + 1) for child in item["itemGroup"])
        else:
            identity = item.get("id")
            if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", identity):
                raise ValueError("Invalid M365 model identifier.")
            title = item.get("menuItemTitle", identity)
            if not isinstance(title, str) or not 1 <= len(title) <= 256:
                raise ValueError("Invalid M365 model title.")
            models[identity] = {"id": identity, "name": title}
    if not models:
        raise ValueError("No account models were returned by M365.")
    return sorted(models.values(), key=lambda model: model["id"])


async def probe_models(network: Network, profile: M365Profile) -> list[dict]:
    headers = {"Authorization": f"Bearer {profile.access_token}", "Origin": profile.origin,
               "Referer": profile.origin + "/", "User-Agent": profile.user_agent, "X-Scenario": profile.scenario}
    async with network.request("GET", "https://substrate.office.com/m365Copilot/getgptlist",
                               params={"request": "{}"}, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=30, sock_connect=15)) as response:
        if response.status >= 300:
            raise UpstreamError(response.status, f"M365 model verification returned HTTP {response.status}.")
        body = bytearray()
        async for chunk in response.content.iter_chunked(16384):
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                raise ValueError("M365 model response exceeds 1 MiB.")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            raise ValueError("M365 model response is not valid JSON.") from None
    return menu_models(data)


class M365Auth:
    def __init__(self, store: UserStore):
        self.store = store
        saved = store.read("m365-auth", {})
        if not isinstance(saved, dict):
            raise ValueError("Invalid M365 authorization storage.")
        self.enabled = saved.get("enabled") is True
        self.link_id = saved.get("linkId", "")
        if self.enabled and (not isinstance(self.link_id, str) or not re.fullmatch(r"[a-f0-9]{32}", self.link_id)):
            raise ValueError("Invalid M365 connection storage. Disconnect and connect again.")
        self.profile = M365Profile(**saved["profile"]) if saved.get("profile") else None
        self.rejected = saved.get("rejected") is True
        self.generation = 0
        self.verification = "not-checked"
        self.last_error = ""

    def save(self, enabled: bool, profile: M365Profile | None, rejected: bool = False):
        self.store.write("m365-auth", {"enabled": enabled, "linkId": self.link_id,
                                      "profile": asdict(profile) if profile else None, "rejected": rejected})
        self.enabled, self.profile, self.rejected = enabled, profile, rejected
        self.generation += 1
        self.verification, self.last_error = "not-checked", ""

    def status(self) -> dict:
        expired = bool(self.profile and self.profile.expires_at and self.profile.expires_at <= time.time() + 30)
        state = ("disconnected" if not self.enabled else "waiting-for-capture" if not self.profile
                 else "needs-login" if expired or self.rejected else "captured")
        return {"state": state, "linked": self.enabled, "linkId": self.link_id if self.enabled else None,
                "syncEnabled": M365Settings.load(self.store.root).enabled,
                "verification": self.verification, "message": self.last_error,
                "accountKind": self.profile.kind if self.profile else None,
                "accountId": f"{self.profile.oid}@{self.profile.tid}" if self.profile else None,
                "username": self.profile.username if self.profile else None,
                "expiresAt": self.profile.expires_at if self.profile else None,
                "capturedAt": self.profile.captured_at if self.profile else None,
                "chatReady": False}

    def connect(self) -> dict:
        self.check_sync_enabled()
        if self.enabled:
            return self.status()
        old_link = self.link_id
        self.link_id = secrets.token_hex(16)
        try:
            self.save(True, None)
        except OSError:
            self.link_id = old_link
            raise
        return self.status()

    def check_link(self, link_id: object):
        if not isinstance(link_id, str) or not self.enabled or not secrets.compare_digest(link_id, self.link_id):
            raise ValueError("This browser connection is disconnected or outdated. Click Connect again.")

    def check_sync_enabled(self):
        if not M365Settings.load(self.store.root).enabled:
            raise ValueError("M365 synchronization is paused in the desktop application.")

    def capture(self, data: object, link_id: object) -> dict:
        self.check_sync_enabled()
        if not self.enabled:
            raise ValueError("M365 synchronization is disconnected. Click Connect in the extension first.")
        self.check_link(link_id)
        profile = M365Profile.parse(data)
        if self.profile:
            if profile.account != self.profile.account:
                raise ValueError("A different M365 account was captured. Disconnect and connect the new account.")
            if profile.access_token == self.profile.access_token:
                return self.status()
            if profile.captured_at < self.profile.captured_at or (
                    profile.expires_at and self.profile.expires_at and profile.expires_at < self.profile.expires_at):
                raise ValueError("An older M365 capture cannot replace the current authorization.")
        self.save(True, profile)
        return self.status()

    def disconnect(self) -> dict:
        self.store.delete("m365-auth")
        self.enabled, self.profile, self.rejected = False, None, False
        self.link_id = ""
        self.generation += 1
        self.verification, self.last_error = "not-checked", ""
        return self.status()

    async def verify(self, network: Network) -> dict:
        if self.status()["state"] != "captured" or self.profile is None:
            raise ValueError("Capture a usable M365 session before verifying its model directory.")
        generation, network_generation = self.generation, network.generation
        try:
            models = await probe_models(network, self.profile)
        except (UpstreamError, aiohttp.ClientError, TimeoutError, ValueError) as error:
            if generation == self.generation:
                if isinstance(error, UpstreamError) and error.status == 401:
                    self.save(self.enabled, self.profile, True)
                self.verification = "failed"
                self.last_error = (str(error) if isinstance(error, (UpstreamError, ValueError))
                                   else "M365 connection failed. Check proxy, certificates and organization policy.")
                log.warning("%s", self.last_error)
            raise ValueError("M365 verification failed; inspect this installation's M365 status for a redacted diagnostic.") from None
        if generation != self.generation or network_generation != network.generation:
            raise ValueError("Authorization or network changed during M365 verification; retry explicitly.")
        self.verification, self.last_error = "catalog-verified", ""
        return {**self.status(), "scenario": self.profile.scenario, "models": models}
