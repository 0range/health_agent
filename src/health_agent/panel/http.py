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
        self._authority = _canonical_authority(port)
        self._origin = f"http://{self._authority}"

    def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PanelResponse:
        """Dispatch one bounded request without depending on a live HTTP server."""
        if not _exact_header(_header(headers, "host"), self._authority):
            return self._html(400, _message_page("Запрос отклонён проверкой адреса."))
        parsed = urlsplit(target)
        if target != parsed.path or not target.startswith("/"):
            return self._not_found()
        method = method.upper()
        path = parsed.path
        if method == "GET":
            if path == "/":
                return self._html(
                    200, _render_home(self._service.list_profiles(), self._csrf_token)
                )
            profile_id = _profile_id_from_path(path)
            if profile_id is not None:
                try:
                    panel = self._service.profile(profile_id)
                except ProfileNotFoundError:
                    return self._not_found()
                return self._html(200, _render_profile(panel, self._csrf_token))
            saved_profile_id = _profile_action_id_from_path(path, "drive-saved")
            if saved_profile_id is not None:
                try:
                    panel = self._service.profile(saved_profile_id)
                except ProfileNotFoundError:
                    return self._not_found()
                return self._html(
                    200,
                    _render_profile(
                        panel,
                        self._csrf_token,
                        notice="Настройка Google Drive сохранена.",
                    ),
                )
            if path in {"/profiles", "/profiles/"}:
                return self._not_found()
            return self._not_found()
        if method == "POST":
            if path == "/profiles":
                return self._create_profile(headers, body)
            profile_id = _profile_action_id_from_path(path, "drive")
            if profile_id is not None:
                return self._configure_drive(profile_id, headers, body)
            return self._not_found()
        if path == "/":
            return self._method_not_allowed("GET")
        if _profile_id_from_path(path) is not None:
            return self._method_not_allowed("GET")
        if path == "/profiles":
            return self._method_not_allowed("POST")
        if _profile_action_id_from_path(path, "drive") is not None:
            return self._method_not_allowed("POST")
        if _profile_action_id_from_path(path, "drive-saved") is not None:
            return self._method_not_allowed("GET")
        return self._not_found()

    def _create_profile(self, headers: Mapping[str, str], body: bytes) -> PanelResponse:
        if len(body) > MAX_FORM_BYTES:
            return self._html(413, _message_page("Слишком большой запрос."))
        if not _same_origin(_header(headers, "origin"), self._origin):
            return self._html(
                403, _message_page("Запрос отклонён проверкой источника.")
            )
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

    def _configure_drive(
        self,
        profile_id: UUID,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PanelResponse:
        if len(body) > MAX_FORM_BYTES:
            return self._html(413, _message_page("Слишком большой запрос."))
        if not _same_origin(_header(headers, "origin"), self._origin):
            return self._html(
                403, _message_page("Запрос отклонён проверкой источника.")
            )
        try:
            pairs = parse_qsl(
                body.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=2,
            )
        except (UnicodeDecodeError, ValueError):
            return self._html(400, _message_page("Некорректная форма."))
        if len(pairs) != 2 or len({name for name, _ in pairs}) != 2:
            return self._html(400, _message_page("Некорректная форма."))
        fields = dict(pairs)
        if set(fields) != {"folders", "csrf_token"}:
            return self._html(403, _message_page("Запрос отклонён защитой формы."))
        if not compare_digest(fields["csrf_token"], self._csrf_token):
            return self._html(403, _message_page("Запрос отклонён защитой формы."))
        folders = [
            line.strip() for line in fields["folders"].splitlines() if line.strip()
        ]
        try:
            self._service.configure_drive(profile_id, folders)
        except ProfileNotFoundError:
            return self._not_found()
        except (TypeError, ValueError):
            try:
                panel = self._service.profile(profile_id)
            except ProfileNotFoundError:
                return self._not_found()
            return self._html(
                400,
                _render_profile(
                    panel,
                    self._csrf_token,
                    notice="Проверьте ссылки на папки Google Drive.",
                    notice_is_error=True,
                ),
            )
        except (OSError, RuntimeError):
            return self._html(
                500,
                _message_page(
                    "Не удалось сохранить настройку Google Drive. Попробуйте ещё раз."
                ),
            )
        return PanelResponse(
            303,
            {
                **_SECURITY_HEADERS,
                "Location": f"/profiles/{profile_id}/drive-saved",
            },
            b"",
        )

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
        return PanelResponse(
            response.status, {**response.headers, "Allow": allow}, response.body
        )


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
                self._send(
                    self.application._html(400, _message_page("Некорректный запрос."))
                )
                return
            if length < 0:
                self._send(
                    self.application._html(400, _message_page("Некорректный запрос."))
                )
                return
            if length > MAX_FORM_BYTES:
                self._send(
                    self.application.handle(
                        "POST",
                        self.path,
                        self._application_headers(),
                        b"x" * (MAX_FORM_BYTES + 1),
                    )
                )
                return
            self._dispatch(self.rfile.read(length))

        def _dispatch(self, body: bytes) -> None:
            self._send(
                self.application.handle(
                    self.command, self.path, self._application_headers(), body
                )
            )

        def _application_headers(self) -> dict[str, str]:
            headers = dict(self.headers.items())
            host_values = self.headers.get_all("Host", failobj=[])
            if len(host_values) != 1:
                headers["Host"] = ""
            return headers

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
    PanelRequestHandler.application = PanelApplication(
        service, port=server.server_address[1]
    )
    return server


def _header(headers: Mapping[str, str], name: str) -> str | None:
    values = tuple(value for key, value in headers.items() if key.lower() == name)
    return values[0] if len(values) == 1 else None


def _same_origin(origin: str | None, expected_origin: str) -> bool:
    return _exact_header(origin, expected_origin)


def _exact_header(value: str | None, expected: str) -> bool:
    return value is not None and compare_digest(value, expected)


def _canonical_authority(port: int) -> str:
    return "127.0.0.1" if port == 80 else f"127.0.0.1:{port}"


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


def _profile_action_id_from_path(path: str, action: str) -> UUID | None:
    parts = path.split("/")
    if len(parts) != 4 or parts[:2] != ["", "profiles"] or parts[3] != action:
        return None
    try:
        profile_id = UUID(parts[2])
    except ValueError:
        return None
    return profile_id if str(profile_id) == parts[2] else None


def _page(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>
body{{font-family:system-ui,sans-serif;margin:0;background:#f5f7fa;color:#172033}} main{{max-width:960px;margin:auto;padding:2rem 1rem}}
a{{color:#075985}} .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem}} .card,form{{background:#fff;border-radius:.6rem;padding:1rem;box-shadow:0 1px 3px #0002}}
.status{{font-weight:700}} label,input,textarea,button{{display:block;font:inherit}} input,textarea{{box-sizing:border-box;margin:.4rem 0 1rem;padding:.5rem;width:min(100%,40rem)}} textarea{{min-height:7rem}} button{{padding:.5rem .8rem}} .muted{{color:#536174}} .notice{{background:#dcfce7;border-radius:.4rem;padding:.8rem}} .notice.error{{background:#fee2e2}}
</style></head><body><main>{content}</main></body></html>"""


def _render_home(profiles: tuple[ProfileSummary, ...], csrf_token: str) -> str:
    profile_items = (
        "".join(
            f'<li><a href="/profiles/{profile.id}">{escape(profile.name)}</a></li>'
            for profile in profiles
        )
        or "<li>Профилей пока нет.</li>"
    )
    content = f"""<h1>Health Agent</h1><p class="muted">Локальная панель управления профилями и подключениями.</p>
<section aria-labelledby="profiles"><h2 id="profiles">Профили</h2><ul>{profile_items}</ul></section>
<form method="post" action="/profiles"><h2>Создать профиль</h2><label for="name">Имя</label><input id="name" name="name" aria-label="Имя нового профиля" required maxlength="255">
<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}"><button type="submit">Создать</button></form>"""
    return _page("Health Agent — профили", content)


def _render_profile(
    panel: ProfilePanel,
    csrf_token: str,
    *,
    notice: str | None = None,
    notice_is_error: bool = False,
) -> str:
    cards = "".join(_render_card(card, panel.profile.id) for card in panel.connectors)
    notice_html = ""
    if notice:
        notice_class = "notice error" if notice_is_error else "notice"
        notice_html = f'<p class="{notice_class}" role="status">{escape(notice)}</p>'
    folders = "\n".join(panel.drive_folder_ids)
    content = f"""<p><a href="/">← Все профили</a></p><h1>Профиль: {escape(panel.profile.name)}</h1>
{notice_html}<section aria-labelledby="connectors"><h2 id="connectors">Подключения</h2><div class="cards">{cards}</div></section>
<form method="post" action="/profiles/{panel.profile.id}/drive"><h2>Настроить Google Drive</h2>
<p class="muted">Одна или несколько папок, по одной ссылке или ID на строке. Сохранение заменит текущий список.</p>
<label for="drive-folders">Ссылки на папки</label><textarea id="drive-folders" name="folders" required maxlength="3000" autocomplete="off" spellcheck="false">{escape(folders)}</textarea>
<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}"><button type="submit">Сохранить папки</button></form>"""
    return _page(f"Health Agent — {panel.profile.name}", content)


def _render_card(card: ConnectorCard, profile_id: UUID) -> str:
    label = _STATUS_LABELS.get(card.status, "Статус неизвестен")
    last_success = (
        card.last_success_at.isoformat() if card.last_success_at else "ещё не было"
    )
    error = f"<p>Код ошибки: {escape(card.error_code)}</p>" if card.error_code else ""
    accounts = ""
    if card.account_ids:
        account_label = "Аккаунт" if len(card.account_ids) == 1 else "Аккаунты"
        account_values = ", ".join(
            escape(account_id) for account_id in card.account_ids
        )
        accounts = f"<p>{account_label}: {account_values}</p>"
    return f"""<article class="card"><h3>{escape(card.connector.upper())}</h3><p class="status">{label}</p>
<p>{escape(card.detail)}</p>{accounts}<p>Последняя успешная операция: {escape(last_success)}</p>{error}
<p class="muted">Следующее действие: {escape(_cli_guidance(card, profile_id))}</p></article>"""


def _cli_guidance(card: ConnectorCard, profile_id: UUID) -> str:
    healthy = card.status in {"ready", "configured"} and card.error_code is None
    if healthy:
        return "действий не требуется."
    if card.connector == "drive":
        if card.status == "not_configured":
            return "укажите папку Google Drive в форме ниже."
        if card.status == "needs_authorization":
            return f"выполните в Terminal: health-agent drive auth {profile_id}"
        return f"проверьте в Terminal: health-agent drive status {profile_id}"
    if card.connector == "whoop":
        if len(card.account_ids) > 1:
            return (
                "проверьте каждый аккаунт отдельно в Terminal: health-agent whoop "
                f"status --profile-id {profile_id} --account <account>"
            )
        account = card.account_ids[0] if card.account_ids else "<account>"
        command = (
            "auth" if card.status in {"not_connected", "reauth_required"} else "status"
        )
        return (
            f"выполните в Terminal: health-agent whoop {command} --profile-id "
            f"{profile_id} --account {account}"
        )
    if card.connector == "gmail":
        if len(card.account_ids) > 1:
            return (
                f"проверьте аккаунты в Terminal: health-agent gmail status {profile_id}"
            )
        if card.status == "not_configured":
            return (
                "выполните в Terminal: health-agent gmail configure "
                f"{profile_id} <account-id>"
            )
        account = card.account_ids[0] if card.account_ids else "<account-id>"
        if card.status in {"needs_authorization", "reauth_required"}:
            return (
                f"выполните в Terminal: health-agent gmail auth {profile_id} {account}"
            )
        return (
            f"выполните в Terminal: health-agent gmail status {profile_id} "
            f"--account-id {account}"
        )
    if card.connector == "telegram":
        if card.status in {"not_configured", "credential_invalid"}:
            return "выполните в Terminal: health-agent telegram configure-token"
        if card.status == "not_bound":
            return f"выполните в Terminal: health-agent telegram bind {profile_id} <telegram-user-id>"
        return f"выполните в Terminal: health-agent telegram status --profile-id {profile_id}"
    return "проверьте локальную конфигурацию через CLI."


def _message_page(message: str) -> str:
    return _page(
        "Health Agent",
        f'<h1>Health Agent</h1><p>{escape(message)}</p><p><a href="/">К профилям</a></p>',
    )
