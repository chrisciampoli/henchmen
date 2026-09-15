"""A fake GitHub REST API for GitHub App tests.

``FakeGitHub`` is an ``httpx.MockTransport`` handler. It checks what real
GitHub checks: ``/app/*`` routes need an RS256 JWT signed with the test App's
key (``iss`` = the App ID, lifetime at most ten minutes), and
``/installation/*`` routes need an installation token it minted. Every request
is recorded in ``requests``.

The App key pair is generated at test time; no private key is committed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

DEFAULT_PERMISSIONS: dict[str, str] = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "metadata": "read",
    "checks": "read",
    "actions": "read",
}


@lru_cache(maxsize=1)
def app_key_pair() -> tuple[bytes, bytes]:
    """``(private PEM, public PEM)`` for the test GitHub App, generated once per session."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private_pem, public_pem


class FakeGitHub:
    """In-memory GitHub App endpoints."""

    def __init__(self, app_id: str = "4242", clock: Callable[[], float] = time.time) -> None:
        self.app_id = app_id
        self.app_slug = "henchmen-test"
        self.clock = clock
        self.requests: list[httpx.Request] = []
        self.token_status = 201
        self.token_lifetime_seconds = 3600
        self.minted: list[dict[str, Any]] = []
        self.conversions: dict[str, dict[str, Any]] = {}
        self.installations: dict[str, dict[str, Any]] = {}
        self.repositories: list[dict[str, Any]] = []
        self.users: dict[str, dict[str, Any]] = {}

    # -- builders ------------------------------------------------------------

    @staticmethod
    def installation(
        installation_id: str,
        login: str,
        *,
        account_type: str = "Organization",
        permissions: dict[str, str] | None = None,
        app_slug: str = "henchmen-test",
        app_id: str = "4242",
    ) -> dict[str, Any]:
        """An ``/app/installations/{id}`` response body (``app_id``/``app_slug`` default to the test App)."""
        return {
            "id": int(installation_id),
            "app_id": int(app_id),
            "account": {"login": login, "type": account_type},
            "permissions": dict(DEFAULT_PERMISSIONS if permissions is None else permissions),
            "repository_selection": "selected",
            "app_slug": app_slug,
        }

    @staticmethod
    def conversion(app_id: str, slug: str, pem: str, owner: str = "chris") -> dict[str, Any]:
        """An ``/app-manifests/{code}/conversions`` response body."""
        return {
            "id": int(app_id),
            "slug": slug,
            "name": f"Henchmen ({slug})",
            "client_id": "Iv1.fakeclientid",
            "client_secret": "client-secret-fake",
            "webhook_secret": "whsec-fake",
            "pem": pem,
            "owner": {"login": owner},
            "html_url": f"https://github.com/apps/{slug}",
        }

    @staticmethod
    def repository(full_name: str, *, default_branch: str = "main", private: bool = True) -> dict[str, Any]:
        """An entry of ``/installation/repositories``."""
        return {"full_name": full_name, "default_branch": default_branch, "private": private}

    # -- clients ---------------------------------------------------------------

    def client(self) -> httpx.Client:
        """A sync client routed to this fake."""
        return httpx.Client(transport=httpx.MockTransport(self))

    def async_client(self) -> httpx.AsyncClient:
        """An async client routed to this fake."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    # -- handler ---------------------------------------------------------------

    def _valid_app_jwt(self, request: httpx.Request) -> bool:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or not token:
            return False
        try:
            claims = jwt.decode(
                token,
                app_key_pair()[1],
                algorithms=["RS256"],
                options={"verify_exp": False, "verify_iat": False},
            )
        except jwt.PyJWTError:
            return False
        return str(claims.get("iss")) == self.app_id and 0 < int(claims["exp"]) - int(claims["iat"]) <= 600

    def _valid_installation_token(self, request: httpx.Request) -> bool:
        header = request.headers.get("authorization", "")
        return any(header == f"Bearer {record['token']}" for record in self.minted)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path
        parts = path.strip("/").split("/")

        if method == "POST" and len(parts) == 3 and parts[0] == "app-manifests" and parts[2] == "conversions":
            body = self.conversions.get(parts[1])
            return httpx.Response(201, json=body) if body else httpx.Response(404, json={"message": "Not Found"})

        if parts[0] == "app":
            if not self._valid_app_jwt(request):
                return httpx.Response(401, json={"message": "A JSON web token could not be decoded"})
            if method == "GET" and parts == ["app"]:
                return httpx.Response(200, json={"id": int(self.app_id), "slug": self.app_slug})
            if method == "GET" and parts == ["app", "installations"]:
                return httpx.Response(200, json=list(self.installations.values()))
            if len(parts) >= 3 and parts[1] == "installations":
                installation = self.installations.get(parts[2])
                if installation is None:
                    return httpx.Response(404, json={"message": "Not Found"})
                if method == "GET" and len(parts) == 3:
                    return httpx.Response(200, json=installation)
                if method == "POST" and len(parts) == 4 and parts[3] == "access_tokens":
                    return self._mint(request)
            return httpx.Response(404, json={"message": "Not Found"})

        if method == "GET" and path == "/installation/repositories":
            if not self._valid_installation_token(request):
                return httpx.Response(401, json={"message": "Bad credentials"})
            page = int(request.url.params.get("page", "1"))
            repositories = self.repositories if page == 1 else []
            return httpx.Response(200, json={"total_count": len(self.repositories), "repositories": repositories})

        if method == "GET" and len(parts) == 2 and parts[0] == "users":
            user = self.users.get(parts[1])
            return httpx.Response(200, json=user) if user else httpx.Response(404, json={"message": "Not Found"})

        return httpx.Response(404, json={"message": "Not Found"})

    def _mint(self, request: httpx.Request) -> httpx.Response:
        if self.token_status != 201:
            return httpx.Response(self.token_status, json={"message": "Bad credentials"})
        body = json.loads(request.content or b"{}")
        token = f"ghs_fake{len(self.minted) + 1:04d}"
        names = body.get("repositories")
        self.minted.append({"token": token, "repositories": names})
        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock() + self.token_lifetime_seconds))
        response: dict[str, Any] = {"token": token, "expires_at": expires}
        if names:
            # Like GitHub: the repositories the token is scoped to, owned by the installation's account.
            login = self.installations[request.url.path.strip("/").split("/")[2]]["account"]["login"]
            response["repositories"] = [
                {"name": name, "full_name": f"{login}/{name}", "owner": {"login": login}} for name in names
            ]
        return httpx.Response(201, json=response)
