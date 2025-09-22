from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx

from .config import GitHubSettings


GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_BASE = "https://api.github.com"


class GitHubOAuthError(RuntimeError):
    """Raised when GitHub OAuth flow fails."""


@dataclass(slots=True)
class GitHubToken:
    access_token: str
    token_type: str
    scope: str


@dataclass(slots=True)
class GitHubUser:
    login: str
    name: Optional[str]
    avatar_url: Optional[str]
    profile_url: str

    @property
    def display_name(self) -> str:
        return self.name or self.login


class GitHubOAuth:
    """Small helper around the GitHub OAuth endpoints."""

    def __init__(self, settings: GitHubSettings, http_timeout: float = 15.0) -> None:
        self._settings = settings
        self._timeout = http_timeout

    @staticmethod
    def build_state() -> str:
        return secrets.token_urlsafe(32)

    def authorization_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self._settings.client_id,
                "redirect_uri": str(self._settings.redirect_uri),
                "scope": " ".join(self._settings.scope),
                "state": state,
                "allow_signup": "false",
            }
        )
        return f"{GITHUB_AUTHORIZE_URL}?{query}"

    async def exchange_code_for_token(self, code: str) -> GitHubToken:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                GITHUB_TOKEN_URL,
                headers={"Accept": "application/json"},
                data=
                {
                    "client_id": self._settings.client_id,
                    "client_secret": self._settings.client_secret,
                    "code": code,
                    "redirect_uri": str(self._settings.redirect_uri),
                },
            )
        if response.status_code != 200:
            raise GitHubOAuthError(
                f"Failed to exchange OAuth code. HTTP {response.status_code}: {response.text}"
            )
        payload = response.json()
        token = payload.get("access_token")
        token_type = payload.get("token_type", "bearer")
        scope = payload.get("scope", "")
        if not token:
            raise GitHubOAuthError("GitHub did not return an access token")
        return GitHubToken(access_token=token, token_type=token_type, scope=scope)

    async def fetch_user(self, token: GitHubToken) -> GitHubUser:
        headers = self._auth_headers(token)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(f"{GITHUB_API_BASE}/user", headers=headers)
        if response.status_code != 200:
            raise GitHubOAuthError(
                f"Failed to retrieve GitHub user profile. HTTP {response.status_code}: {response.text}"
            )
        payload = response.json()
        return GitHubUser(
            login=payload["login"],
            name=payload.get("name"),
            avatar_url=payload.get("avatar_url"),
            profile_url=payload.get("html_url", f"https://github.com/{payload['login']}")
        )

    async def verify_membership(self, token: GitHubToken) -> bool:
        headers = self._auth_headers(token)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{GITHUB_API_BASE}/user/memberships/orgs/{self._settings.organization}",
                headers=headers,
            )
        if response.status_code == 200:
            payload = response.json()
            return payload.get("state") == "active"
        if response.status_code == 404:
            return False
        raise GitHubOAuthError(
            f"Unable to verify membership. HTTP {response.status_code}: {response.text}"
        )

    def _auth_headers(self, token: GitHubToken) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token.access_token}",
            "Accept": "application/vnd.github+json",
        }

