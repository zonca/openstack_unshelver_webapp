from __future__ import annotations

import logging
from typing import Dict

from fasthtml.common import (
    A,
    Button,
    Div,
    Form,
    H2,
    H3,
    P,
    Section,
    Small,
    Span,
    Titled,
    fast_app,
    Redirect,
    serve,
)
from starlette.requests import Request

from openstack_unshelver_webapp.config import (
    ButtonSettings,
    ConfigurationError,
    Settings,
    load_settings,
)
from openstack_unshelver_webapp.github import GitHubOAuth, GitHubOAuthError
from openstack_unshelver_webapp.openstack_client import OpenStackClient
from openstack_unshelver_webapp.unshelve_manager import ButtonStatus, InstanceActionManager


logging.basicConfig(level=logging.INFO)

try:
    SETTINGS: Settings = load_settings()
except ConfigurationError as exc:  # pragma: no cover - configuration must load at startup
    raise SystemExit(str(exc)) from exc

GITHUB = GitHubOAuth(SETTINGS.github)
OPENSTACK_CLIENT = OpenStackClient(SETTINGS.openstack)
BUTTON_MAP: Dict[str, ButtonSettings] = {button.id: button for button in SETTINGS.buttons}
MANAGER = InstanceActionManager(SETTINGS.app, BUTTON_MAP, OPENSTACK_CLIENT)

app, rt = fast_app(title=SETTINGS.app.title, secret_key=SETTINGS.app.secret_key)


def _user_from_session(request: Request) -> dict | None:
    return request.session.get("user")


@rt("/")
async def home(request: Request):
    user = _user_from_session(request)
    if not user:
        return Titled(
            SETTINGS.app.title,
            Section(
                H2("OpenStack Unshelver"),
                P("Login with GitHub (org membership required) to manage instances."),
                A("Login with GitHub", href="/login", cls="btn btn-primary"),
            ),
        )

    cards = []
    for button in SETTINGS.buttons:
        children = [H3(button.label)]
        if button.description:
            children.append(P(button.description))
        children.append(
            Div(
                "Loading status…",
                id=f"status-{button.id}",
                **{
                    "hx-get": f"/status/{button.id}",
                    "hx-trigger": f"load, every {SETTINGS.app.poll_interval_seconds}s",
                    "hx-swap": "outerHTML",
                },
            )
        )
        cards.append(Div(*children, cls="card"))

    content = Section(
        H2(f"Welcome {user.get('display_name', user['login'])}"),
        P("Select an instance below to unshelve and monitor."),
        *cards,
        Form(
            Button("Logout", type="submit", cls="btn"),
            method="post",
            action="/logout",
        ),
    )
    return Titled(SETTINGS.app.title, content)


@rt("/login")
async def login(request: Request):
    state = GITHUB.build_state()
    request.session["oauth_state"] = state
    return Redirect(GITHUB.authorization_url(state))


@rt("/logout", methods=["POST"])
async def logout(request: Request):
    request.session.clear()
    return Redirect("/")


@rt("/auth/callback")
async def auth_callback(request: Request):
    params = request.query_params
    error = params.get("error")
    if error:
        return Titled(SETTINGS.app.title, P(f"GitHub login failed: {error}"))

    state = params.get("state")
    if not state or state != request.session.get("oauth_state"):
        return Titled(SETTINGS.app.title, P("Invalid OAuth state"))

    code = params.get("code")
    if not code:
        return Titled(SETTINGS.app.title, P("Missing OAuth code"))

    try:
        token = await GITHUB.exchange_code_for_token(code)
        if not await GITHUB.verify_membership(token):
            return Titled(
                SETTINGS.app.title,
                Section(
                    H2("Access Denied"),
                    P("GitHub user is not a member of the required organisation."),
                    A("Try another account", href="/login", cls="btn"),
                ),
            )
        user = await GITHUB.fetch_user(token)
    except GitHubOAuthError as exc:
        return Titled(SETTINGS.app.title, P(f"GitHub authentication failed: {exc}"))

    request.session.pop("oauth_state", None)
    request.session["user"] = {
        "login": user.login,
        "name": user.name,
        "display_name": user.display_name,
        "profile_url": user.profile_url,
    }
    request.session["github_token"] = token.access_token
    return Redirect("/")


def _format_timestamp(status: ButtonStatus) -> str:
    return status.last_updated.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _status_fragment(button_id: str, status: ButtonStatus) -> Div:
    last_updated = _format_timestamp(status)
    pieces = [
        Small(f"Instance: `{status.instance_name}`"),
        P(status.message),
        Small(f"Last updated: {last_updated}"),
    ]
    if status.url:
        link_text = "Open web app" if status.http_ready else "Open anyway"
        pieces.append(
            A(
                link_text,
                href=status.url,
                target="_blank",
                rel="noopener",
                cls="btn-link",
            )
        )
    if status.error:
        pieces.append(Span(f"Error: {status.error}", cls="error"))

    pieces.append(
        Button(
            "Unshelve & start" if not status.running else "Working…",
            hx_post=f"/action/{button_id}",
            hx_target=f"#status-{button_id}",
            hx_swap="outerHTML",
            hx_disabled_elt="this",
            disabled=status.running,
            cls="btn btn-primary" if not status.running else "btn disabled",
        )
    )

    return Div(
        *pieces,
        id=f"status-{button_id}",
        **{
            "hx-get": f"/status/{button_id}",
            "hx-trigger": f"load, every {SETTINGS.app.poll_interval_seconds}s",
            "hx-swap": "outerHTML",
        },
    )


@rt("/status/{button_id}")
async def status_view(request: Request, button_id: str):
    if not _user_from_session(request):
        return Div("Login required", id=f"status-{button_id}")
    try:
        status = await MANAGER.refresh_openstack_status(button_id)
    except KeyError:
        return Div("Unknown action", id=f"status-{button_id}")
    return _status_fragment(button_id, status)


@rt("/action/{button_id}", methods=["POST"])
async def trigger_unshelve(request: Request, button_id: str):
    if not _user_from_session(request):
        return Div("Login required", id=f"status-{button_id}")
    try:
        status = await MANAGER.start_unshelve(button_id)
    except KeyError:
        return Div("Unknown action", id=f"status-{button_id}")
    return _status_fragment(button_id, status)


if __name__ == "__main__":
    serve()
