from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict

from fasthtml.common import (
    A,
    Button,
    Div,
    Form,
    H2,
    H3,
    Img,
    P,
    Section,
    Small,
    Span,
    Titled,
    fast_app,
    serve,
    Input,
    H1,
)
from starlette.requests import Request

from openstack_unshelver_webapp.config import (
    ButtonSettings,
    ConfigurationError,
    Settings,
    load_settings,
)
from openstack_unshelver_webapp.activity import CaddyActivityMonitor
from openstack_unshelver_webapp.event_logger import EventLogger
from openstack_unshelver_webapp.openstack_client import OpenStackClient
from openstack_unshelver_webapp.unshelve_manager import ButtonStatus, InstanceActionManager


logging.basicConfig(level=logging.INFO)

try:
    SETTINGS: Settings = load_settings()
except ConfigurationError as exc:  # pragma: no cover - configuration must load at startup
    raise SystemExit(str(exc)) from exc

OPENSTACK_CLIENT = OpenStackClient(SETTINGS.openstack)
BUTTON_MAP: Dict[str, ButtonSettings] = {button.id: button for button in SETTINGS.buttons}
DEFAULT_BUTTON_ID = SETTINGS.buttons[0].id

EVENT_LOGGER = EventLogger(
    local_path=SETTINGS.local_event_log,
    openstack_settings=SETTINGS.openstack,
    swift_container=SETTINGS.swift_event_container,
    swift_prefix=SETTINGS.swift_event_prefix,
)

MANAGER = InstanceActionManager(SETTINGS.app, BUTTON_MAP, OPENSTACK_CLIENT, event_logger=EVENT_LOGGER)


async def _idle_shutdown() -> None:
    status = MANAGER.get_status(DEFAULT_BUTTON_ID)
    if status.state in {"shelved", "idle"}:
        return
    await MANAGER.start_shelve(DEFAULT_BUTTON_ID, actor="idle-monitor", reason="idle-timeout")


ACTIVITY_MONITOR = CaddyActivityMonitor(
    log_path=SETTINGS.activity_log_path,
    upstream_label=SETTINGS.caddy_upstream_label,
    idle_timeout=timedelta(minutes=SETTINGS.idle_timeout_minutes),
    poll_interval=SETTINGS.idle_poll_interval_seconds,
    on_idle=_idle_shutdown,
)

app, rt = fast_app(title=SETTINGS.app.title, secret_key=SETTINGS.app.secret_key)


@app.on_event("startup")
async def _startup_event() -> None:
    await ACTIVITY_MONITOR.start()


@app.on_event("shutdown")
async def _shutdown_event() -> None:
    await ACTIVITY_MONITOR.stop()


@rt("/")
async def home(request: Request):
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
        cards.append(
            Div(
                *children,
                cls="card",
                style="display:flex;flex-direction:column;gap:0.75rem;padding:1rem;border:1px solid #d1d5db;border-radius:12px;min-width:260px;max-width:320px;flex:1 1 260px;box-shadow:0 1px 2px rgba(0,0,0,0.05);background-color:#fff;",
            )
        )

    cards_container = Div(
        *cards,
        cls="card-grid",
        style="display:flex;flex-wrap:wrap;gap:1.5rem;align-items:stretch;margin-top:1.5rem;",
    )

    hero = Div(
        H2("Cosmosage Chat Launcher"),
        P(
            "This tiny computer stays on 24/7 so visitors can wake the powerful AI workstation only when someone is ready to chat."
        ),
        A(
            Img(
                src="https://cosmosage.online/cosmosage.jpg",
                alt="Cosmosage logo",
                referrerpolicy="no-referrer",
                loading="lazy",
                style="max-width:280px;width:100%;border-radius:12px;box-shadow:0 8px 20px rgba(0,0,0,0.08);margin:0 auto;",
            ),
            href="https://cosmosage.online/",
            target="_blank",
            rel="noopener",
        ),
        P(
            "Click the button below when the status says “shelved”. The page will keep you updated while the big machine boots up "
            "(it can take a few minutes). When it is ready, just visit /chat on this same address and the AI interface will appear."
        ),
        P(
            "After your session, return here to put Cosmosage back to sleep so we are not wasting energy or GPU hours."
        ),
        cls="user-header",
        style="display:flex;flex-direction:column;gap:0.5rem;",
    )

    content = Section(hero, cards_container)
    return Titled(SETTINGS.app.title, content)


def _format_timestamp(status: ButtonStatus) -> str:
    return status.last_updated.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _status_fragment(button_id: str, status: ButtonStatus) -> Div:
    last_updated = _format_timestamp(status)
    pieces = [
        Small(f"Instance: `{status.instance_name}`"),
        P(status.message),
        Small(f"Last updated: {last_updated}"),
    ]
    if status.state in {"active", "ready"} and status.url:
        pieces.append(
            Span(
                "Cosmosage is already awake—head to the chat link below.",
                cls="status-note",
                style="display:block;margin-bottom:0.5rem;color:#065f46;font-weight:500;",
            )
        )
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

    if status.running:
        button_label = "Working…"
        button_disabled = True
        button_cls = "btn disabled"
    elif status.state in {"active", "ready"}:
        button_label = "Cosmosage is awake"
        button_disabled = True
        button_cls = "btn disabled"
    else:
        button_label = "Wake Cosmosage"
        button_disabled = False
        button_cls = "btn btn-primary"

    pieces.append(
        Button(
            button_label,
            hx_post=f"/action/{button_id}",
            hx_target=f"#status-{button_id}",
            hx_swap="outerHTML",
            hx_disabled_elt="this",
            disabled=button_disabled,
            cls=button_cls,
            style="margin-top:auto;font-weight:600;",
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
    try:
        status = await MANAGER.refresh_openstack_status(button_id)
    except KeyError:
        return Div("Unknown action", id=f"status-{button_id}")
    return _status_fragment(button_id, status)


@rt("/action/{button_id}", methods=["POST"])
async def trigger_unshelve(request: Request, button_id: str):
    try:
        status = await MANAGER.start_unshelve(button_id, actor="public-button", reason="web-request")
    except KeyError:
        return Div("Unknown action", id=f"status-{button_id}")
    return _status_fragment(button_id, status)


def _verify_token(request: Request) -> bool:
    token = request.query_params.get("token") or request.headers.get("x-control-token")
    return bool(token and token == SETTINGS.app.control_token)


def _control_denied() -> Titled:
    return Titled(SETTINGS.app.title, Section(H2("Control Locked"), P("Provide the correct token to access this page.")))


@rt("/control")
async def control_panel(request: Request):
    if not _verify_token(request):
        return _control_denied()

    status = MANAGER.get_status(DEFAULT_BUTTON_ID)
    last_activity = ACTIVITY_MONITOR.last_activity()
    idle_timeout = timedelta(minutes=SETTINGS.idle_timeout_minutes)
    deadline = last_activity + idle_timeout if last_activity else None
    activity_info = [
        P(f"Last proxied request: {_format_dt(last_activity) if last_activity else 'unknown'}"),
        P(f"Idle timeout: {idle_timeout}"),
        P(f"Next auto-shelve: {_format_dt(deadline) if deadline else 'unknown'}"),
    ]
    token = request.query_params.get("token")
    shelve_form = Form(
        Input(type="hidden", name="token", value=token),
        Button("Shelve GPU now", type="submit", cls="btn btn-danger"),
        method="post",
        action=SETTINGS.app.manual_shelve_path,
        style="margin-top:1rem;",
        **{
            "hx-post": SETTINGS.app.manual_shelve_path,
            "hx-target": f"#status-{DEFAULT_BUTTON_ID}",
            "hx-swap": "outerHTML",
        },
    )
    unshelve_form = Form(
        Input(type="hidden", name="token", value=token),
        Button("Force Unshelve", type="submit", cls="btn btn-primary"),
        method="post",
        action=f"/admin-unshelve/{DEFAULT_BUTTON_ID}",
        style="margin-top:1rem;",
        **{
            "hx-post": f"/admin-unshelve/{DEFAULT_BUTTON_ID}",
            "hx-target": f"#status-{DEFAULT_BUTTON_ID}",
            "hx-swap": "outerHTML",
        },
    )
    content = Section(
        H1("Controller"),
        P("This dashboard remains on the controller VM even when traffic is proxied."),
        Div(*activity_info, cls="activity-card", style="margin-bottom:1rem;"),
        _status_fragment(DEFAULT_BUTTON_ID, status),
        shelve_form,
        unshelve_form,
    )
    return Titled(SETTINGS.app.title, content)


@rt(SETTINGS.app.manual_shelve_path, methods=["POST"])
async def manual_shelve(request: Request):
    form = await request.form()
    token = form.get("token") or request.query_params.get("token")
    if token != SETTINGS.app.control_token:
        return _control_denied()
    status = await MANAGER.start_shelve(DEFAULT_BUTTON_ID, actor="manual-shelve", reason="control-panel")
    return _status_fragment(DEFAULT_BUTTON_ID, status)


@rt("/admin-unshelve/{button_id}", methods=["POST"])
async def manual_unshelve(request: Request, button_id: str):
    form = await request.form()
    token = form.get("token") or request.query_params.get("token")
    if token != SETTINGS.app.control_token:
        return _control_denied()
    try:
        status = await MANAGER.start_unshelve(button_id, actor="manual-unshelve", reason="control-panel")
    except KeyError:
        return Div("Unknown action", id=f"status-{button_id}")
    return _status_fragment(button_id, status)


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


if __name__ == "__main__":
    serve()
