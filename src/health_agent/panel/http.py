"""Loopback-only, server-rendered management panel HTTP boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hmac import compare_digest
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from secrets import token_urlsafe
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from health_agent.panel.models import ConnectorCard, ProfilePanel, ProfileSummary
from health_agent.panel.service import PanelService, ProfileNotFoundError

MAX_FORM_BYTES = 4 * 1024
DEFAULT_PANEL_PORT = 8766

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}

_STATUS_LABELS = {
    "ready": "Готово",
    "configured": "Настроено",
    "not_connected": "Не подключено",
    "not_configured": "Не настроено",
    "not_bound": "Не привязано",
    "needs_authorization": "Нужна авторизация",
    "reauth_required": "Нужно переподключение",
    "credential_invalid": "Нужно проверить учётные данные",
    "status_unavailable": "Статус недоступен",
    "not_available": "Пока недоступно",
}


@dataclass(frozen=True, slots=True)
class PanelResponse:
    """A complete deterministic HTTP response for panel route tests."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class PanelApplication:
    """Pure request dispatcher; it does not open sockets or call connectors."""

    def __init__(
        self,
        service: PanelService,
        *,
        csrf_token: str | None = None,
        port: int = DEFAULT_PANEL_PORT,
    ) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("The panel port must be between 1 and 65535")
        self._service = service
        self._csrf_token = csrf_token or token_urlsafe(32)
        self._origin = f"http://127.0.0.1:{port}"

    def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PanelResponse:
        """Dispatch one bounded request without depending on a live HTTP server."""
        parsed = urlsplit(target)
        if target != parsed.path or not target.startswith("/"):
            return self._not_found()
        method = method.upper()
        path = parsed.path
        if method == "GET":
            if path == "/":
                return self._html(200, _render_home(self._service.list_profiles(), self._csrf_token))
            profile_id = _profile_id_from_path(path)
            if profile_id is not None:
                try:
                    panel = self._service.profile(profile_id)
                except ProfileNotFoundError:
                    return self._not_found()
                return self._html(200, _render_profile(panel))
            if path in {"/profiles", "/profiles/"}:
                return self._not_found()
            return self._not_found()
        if method == "POST":
            if path != "/profiles":
                return self._not_found()
            return self._create_profile(headers, body)
        if path == "/":
            return self._method_not_allowed("GET")
        if _profile_id_from_path(path) is not None:
            return self._method_not_allowed("GET")
        if path == "/profiles":
            return self._method_not_allowed("POST")
        return self._not_found()

    def _create_profile(self, headers: Mapping[str, str], body: bytes) -> PanelResponse:
        if len(body) > MAX_FORM_BYTES:
            return self._html(413, _message_page("Слишком большой запрос."))
        if not _same_origin(_header(headers, "origin"), self._origin):
            return self._html(403, _message_page("Запрос отклонён проверкой источника."))
        try:
            fields = dict(
                parse_qsl(
                    body.decode("utf-8"),
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=2,
                )
            )
        except (UnicodeDecodeError, ValueError):
            return self._html(400, _message_page("Некорректная форма."))
        if set(fields) != {"name", "csrf_token"} or not compare_digest(
            fields["csrf_token"], self._csrf_token
        ):
            return self._html(403, _message_page("Запрос отклонён защитой формы."))
        try:
            self._service.create_profile(fields["name"])
        except (TypeError, ValueError):
            return self._html(400, _message_page("Введите имя от 1 до 255 символов."))
        return PanelResponse(303, {**_SECURITY_HEADERS, "Location": "/"}, b"")

    @staticmethod
    def _html(status: int, page: str) -> PanelResponse:
        return PanelResponse(
            status,
            {**_SECURITY_HEADERS, "Content-Type": "text/html; charset=utf-8"},
            page.encode("utf-8"),
        )

    @classmethod
    def _not_found(cls) -> PanelResponse:
        return cls._html(404, _message_page("Страница не найдена."))

    @classmethod
    def _method_not_allowed(cls, allow: str) -> PanelResponse:
        response = cls._html(405, _message_page("Этот метод не поддерживается."))
        return PanelResponse(response.status, {**response.headers, "Allow": allow}, response.body)


def serve_panel(service: PanelService, *, host: str, port: int) -> ThreadingHTTPServer:
    """Create a panel server that can bind only the IPv4 loopback address."""
    if host != "127.0.0.1":
        raise ValueError("The management panel may bind only to 127.0.0.1")
    class PanelRequestHandler(BaseHTTPRequestHandler):
        application: PanelApplication

        def do_GET(self) -> None:
            self._dispatch(b"")

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(self.application._html(400, _message_page("Некорректный запрос.")))
                return
            if length < 0:
                self._send(self.application._html(400, _message_page("Некорректный запрос.")))
                return
            if length > MAX_FORM_BYTES:
                self._send(
                    self.application.handle(
                        "POST",
                        self.path,
                        dict(self.headers.items()),
                        b"x" * (MAX_FORM_BYTES + 1),
                    )
                )
                return
            self._dispatch(self.rfile.read(length))

        def _dispatch(self, body: bytes) -> None:
            self._send(
                self.application.handle(self.command, self.path, dict(self.headers.items()), body)
            )

        def handle_one_request(self) -> None:
            """Use application 405 handling for every unsupported HTTP verb."""
            try:
                self.raw_requestline = self.rfile.readline(65537)
                if len(self.raw_requestline) > 65536:
                    self.requestline = ""
                    self.request_version = ""
                    self.command = ""
                    self.send_error(414)
                    return
                if not self.raw_requestline:
                    self.close_connection = True
                    return
                if not self.parse_request():
                    return
                handler = getattr(self, f"do_{self.command}", None)
                if handler is None:
                    self._dispatch(b"")
                else:
                    handler()
                self.wfile.flush()
            except TimeoutError as error:
                self.log_error("Request timed out: %r", error)
                self.close_connection = True

        def _send(self, response: PanelResponse) -> None:
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, _format: str, *_args: object) -> None:
            """Do not write request metadata to the user's terminal."""

    server = ThreadingHTTPServer(("127.0.0.1", port), PanelRequestHandler)
    PanelRequestHandler.application = PanelApplication(service, port=server.server_address[1])
    return server


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return next((value for key, value in headers.items() if key.lower() == name), None)


def _same_origin(origin: str | None, expected_origin: str) -> bool:
    return origin is not None and compare_digest(origin, expected_origin)


def _profile_id_from_path(path: str) -> UUID | None:
    prefix = "/profiles/"
    if not path.startswith(prefix) or path.count("/") != 2:
        return None
    value = path.removeprefix(prefix)
    try:
        profile_id = UUID(value)
    except ValueError:
        return None
    return profile_id if str(profile_id) == value else None


def _page(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>
body{{font-family:system-ui,sans-serif;margin:0;background:#f5f7fa;color:#172033}} main{{max-width:960px;margin:auto;padding:2rem 1rem}}
a{{color:#075985}} .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem}} .card,form{{background:#fff;border-radius:.6rem;padding:1rem;box-shadow:0 1px 3px #0002}}
.status{{font-weight:700}} label,input,button{{display:block;font:inherit}} input{{margin:.4rem 0 1rem;padding:.5rem;width:min(100%,28rem)}} button{{padding:.5rem .8rem}} .muted{{color:#536174}}
</style></head><body><main>{content}</main></body></html>"""


def _render_home(profiles: tuple[ProfileSummary, ...], csrf_token: str) -> str:
    profile_items = "".join(
        f'<li><a href="/profiles/{profile.id}">{escape(profile.name)}</a></li>' for profile in profiles
    ) or "<li>Профилей пока нет.</li>"
    content = f"""<h1>Health Agent</h1><p class="muted">Локальная панель управления профилями и подключениями.</p>
<section aria-labelledby="profiles"><h2 id="profiles">Профили</h2><ul>{profile_items}</ul></section>
<form method="post" action="/profiles"><h2>Создать профиль</h2><label for="name">Имя</label><input id="name" name="name" aria-label="Имя нового профиля" required maxlength="255">
<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}"><button type="submit">Создать</button></form>"""
    return _page("Health Agent — профили", content)


def _render_profile(panel: ProfilePanel) -> str:
    cards = "".join(_render_card(card, panel.profile.id) for card in panel.connectors)
    content = f"""<p><a href="/">← Все профили</a></p><h1>Профиль: {escape(panel.profile.name)}</h1>
<section aria-labelledby="connectors"><h2 id="connectors">Подключения</h2><div class="cards">{cards}</div></section>"""
    return _page(f"Health Agent — {panel.profile.name}", content)


def _render_card(card: ConnectorCard, profile_id: UUID) -> str:
    label = _STATUS_LABELS.get(card.status, "Статус неизвестен")
    last_success = card.last_success_at.isoformat() if card.last_success_at else "ещё не было"
    error = f"<p>Код ошибки: {escape(card.error_code)}</p>" if card.error_code else ""
    return f"""<article class="card"><h3>{escape(card.connector.upper())}</h3><p class="status">{label}</p>
<p>{escape(card.detail)}</p><p>Последняя успешная операция: {escape(last_success)}</p>{error}
<p class="muted">Следующее действие: {escape(_cli_guidance(card.connector, profile_id))}</p></article>"""


def _cli_guidance(connector: str, profile_id: UUID) -> str:
    commands = {
        "whoop": f"выполните в Terminal: health-agent whoop auth {profile_id}",
        "gmail": f"выполните в Terminal: health-agent gmail configure {profile_id} <account-id>",
        "telegram": f"выполните в Terminal: health-agent telegram --help для профиля {profile_id}",
        "drive": "интеграция Google Drive пока недоступна.",
    }
    return commands.get(connector, "проверьте локальную конфигурацию через CLI.")


def _message_page(message: str) -> str:
    return _page("Health Agent", f"<h1>Health Agent</h1><p>{escape(message)}</p><p><a href=\"/\">К профилям</a></p>")
