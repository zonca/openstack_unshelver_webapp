import httpx
import pytest

from openstack_unshelver_webapp.config import GitHubSettings
from openstack_unshelver_webapp.github import (
    GITHUB_API_BASE,
    GITHUB_AUTHORIZE_URL,
    GitHubOAuth,
    GitHubOAuthError,
    GitHubToken,
)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="OK"):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class StubAsyncClient:
    def __init__(self, responses):
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *_, **__):
        return self._responses[("POST", url)]

    async def get(self, url, *_, **__):
        return self._responses[("GET", url)]


@pytest.fixture
def github_settings():
    return GitHubSettings(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost/callback",
        organization="acme",
    )


def test_authorization_url_contains_required_params(github_settings):
    oauth = GitHubOAuth(github_settings)
    url = oauth.authorization_url("state123")

    assert url.startswith(GITHUB_AUTHORIZE_URL)
    assert "client_id=cid" in url
    assert "state=state123" in url


@pytest.mark.asyncio
async def test_exchange_code_for_token_success(monkeypatch, github_settings):
    responses = {
        ("POST", "https://github.com/login/oauth/access_token"): FakeResponse(
            json_data={"access_token": "token", "token_type": "bearer", "scope": "read:user"}
        )
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: StubAsyncClient(responses))

    oauth = GitHubOAuth(github_settings)
    token = await oauth.exchange_code_for_token("abc")

    assert token.access_token == "token"


@pytest.mark.asyncio
async def test_exchange_code_for_token_failure(monkeypatch, github_settings):
    responses = {
        ("POST", "https://github.com/login/oauth/access_token"): FakeResponse(status_code=400, text="boom")
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: StubAsyncClient(responses))

    oauth = GitHubOAuth(github_settings)
    with pytest.raises(GitHubOAuthError):
        await oauth.exchange_code_for_token("abc")


@pytest.mark.asyncio
async def test_fetch_user(monkeypatch, github_settings):
    token = GitHubToken(access_token="t", token_type="bearer", scope="read:user")
    responses = {
        ("GET", f"{GITHUB_API_BASE}/user"): FakeResponse(
            json_data={"login": "user", "name": "User", "avatar_url": "http://avatar"}
        )
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: StubAsyncClient(responses))

    oauth = GitHubOAuth(github_settings)
    user = await oauth.fetch_user(token)

    assert user.login == "user"
    assert user.display_name == "User"


@pytest.mark.asyncio
async def test_verify_membership(monkeypatch, github_settings):
    token = GitHubToken(access_token="t", token_type="bearer", scope="read:org")
    responses = {
        ("GET", f"{GITHUB_API_BASE}/user/memberships/orgs/{github_settings.organization}"): FakeResponse(
            json_data={"state": "active"}
        )
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: StubAsyncClient(responses))

    oauth = GitHubOAuth(github_settings)
    assert await oauth.verify_membership(token)

    responses[(
        "GET",
        f"{GITHUB_API_BASE}/user/memberships/orgs/{github_settings.organization}",
    )] = FakeResponse(status_code=404)
    assert not await oauth.verify_membership(token)
