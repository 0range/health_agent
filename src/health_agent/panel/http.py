"""Loopback-only, server-rendered management panel HTTP boundary."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hmac import compare_digest
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from secrets import token_urlsafe
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from health_agent.panel.models import (
    ConnectorCard,
    PanelDestination,
    ProfilePanel,
    ProfileSummary,
)
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

_PRODUCT_STATUS_LABELS = {
    "connected": "Подключено",
    "not_synced": "Синхронизация ещё не запускалась",
    "action_required": "Нужно действие",
}
_CONNECTOR_LABELS = {
    "whoop": "WHOOP",
    "drive": "Google Drive",
    "gmail": "Gmail",
    "telegram": "Telegram",
    "reminders": "Напоминания",
    "database": "Локальная база",
}
_CONNECTOR_ORDER = {
    connector: index
    for index, connector in enumerate(
        ("whoop", "drive", "gmail", "telegram", "reminders", "database")
    )
}
_AUTH_ERROR_CODES = frozenset(
    {"OAuthRequired", "credential_invalid", "reauth_required", "token_not_configured"}
)
_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_GOOGLE_SHEET_PATH = re.compile(r"/spreadsheets/d/[A-Za-z0-9_-]{8,300}(?:/edit)?/?")


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
*{{box-sizing:border-box}} body{{font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;margin:0;background:#f3f6f4;color:#17231d;line-height:1.5}} main{{max-width:1120px;margin:auto;padding:2.5rem 1.25rem 4rem}}
a{{color:#176b45;text-underline-offset:.18em}} a:focus-visible,button:focus-visible,input:focus-visible,textarea:focus-visible,summary:focus-visible{{outline:3px solid #6bbf92;outline-offset:3px}}
h1{{font-size:clamp(2rem,5vw,3.4rem);line-height:1.05;margin:.4rem 0 1rem;letter-spacing:-.035em}} h2{{font-size:1.25rem;margin:0 0 1rem}} h3{{font-size:1.05rem;margin:0}} section{{margin-top:2rem}}
.eyebrow{{color:#176b45;font-size:.78rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase}} .lede,.muted{{color:#53665b}} .lede{{font-size:1.05rem;max-width:44rem}}
.profile-list,.cards,.destinations{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr));gap:1rem;padding:0;list-style:none}}
.profile-link,.card,.destination,form,.settings{{background:#fff;border:1px solid #dce5df;border-radius:1rem;box-shadow:0 8px 28px #1838240b}}
.profile-link{{display:block;padding:1.2rem;text-decoration:none;font-weight:750}} .profile-link:hover{{border-color:#8fb9a2}}
.card{{padding:1.15rem;min-width:0}} .card-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:.75rem}} .card p{{margin:.7rem 0 0}}
.status-pill{{border-radius:999px;display:inline-block;font-size:.74rem;font-weight:800;line-height:1.25;padding:.35rem .6rem;white-space:normal;text-align:center}} [data-state="connected"] .status-pill{{background:#dcf5e6;color:#145c39}} [data-state="not_synced"] .status-pill{{background:#fff0c7;color:#6f4d00}} [data-state="action_required"] .status-pill{{background:#ffe1df;color:#8b2822}}
.rollup{{background:#173d2c;color:#fff;border-radius:1.2rem;padding:1.25rem 1.4rem;margin:1.5rem 0}} .rollup strong{{font-size:1.15rem}} .rollup p{{margin:.3rem 0 0;color:#d8e9df}}
.action{{font-weight:700}} details{{margin-top:1rem}} summary{{cursor:pointer;font-weight:700;min-height:44px;display:flex;align-items:center}} .technical-details{{border-top:1px solid #e1e8e3;padding-top:.25rem;color:#526057;font-size:.88rem;overflow-wrap:anywhere}} .technical-details p{{margin:.45rem 0}}
.profile-details{{display:inline-block;color:#53665b}} .profile-details summary{{font-size:.9rem}} .destination{{padding:1rem}} .destination a{{display:flex;min-height:44px;align-items:center;font-weight:800}} .destination p{{margin:.35rem 0 0;color:#53665b}}
.settings{{padding:0 1.15rem;margin-top:2rem}} .settings>summary{{font-size:1.05rem}} form{{border:0;box-shadow:none;padding:0 0 1.25rem}} label,input,textarea,button{{display:block;font:inherit}} input,textarea{{background:#fbfcfb;border:1px solid #9eada4;border-radius:.55rem;margin:.45rem 0 1rem;padding:.75rem;width:100%;max-width:44rem}} textarea{{min-height:8rem;resize:vertical}} button{{background:#176b45;border:0;border-radius:.55rem;color:#fff;cursor:pointer;font-weight:800;min-height:44px;padding:.65rem 1rem}} .notice{{background:#dcf5e6;border-radius:.7rem;padding:.85rem 1rem}} .notice.error{{background:#ffe1df}} .back{{display:inline-flex;min-height:44px;align-items:center}}
@media (max-width:560px){{main{{padding:1.35rem .9rem 3rem}} .card-head{{display:block}} .status-pill{{margin-top:.65rem}} .rollup{{border-radius:.9rem}}}}
</style></head><body><main>{content}</main></body></html>"""


def _render_home(profiles: tuple[ProfileSummary, ...], csrf_token: str) -> str:
    profile_items = (
        "".join(
            f'<li><a class="profile-link" href="/profiles/{profile.id}">'
            f'{escape(profile.name)}<br><span class="muted">Открыть обзор →</span></a></li>'
            for profile in profiles
        )
        or '<li class="muted">Профилей пока нет.</li>'
    )
    content = f"""<p class="eyebrow">Локально на этом Mac</p><h1>Health Agent</h1>
<p class="lede">Профили, подключения и состояние системы — без медицинских данных на экране.</p>
<section aria-labelledby="profiles"><h2 id="profiles">Профили</h2><ul class="profile-list">{profile_items}</ul></section>
<details class="settings"><summary>Добавить профиль</summary><form method="post" action="/profiles"><h2>Создать профиль</h2><label for="name">Имя</label><input id="name" name="name" aria-label="Имя нового профиля" required maxlength="255">
<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}"><button type="submit">Создать</button></form></details>"""
    return _page("Health Agent — профили", content)


def _render_profile(
    panel: ProfilePanel,
    csrf_token: str,
    *,
    notice: str | None = None,
    notice_is_error: bool = False,
) -> str:
    ordered_cards = tuple(
        sorted(
            panel.connectors,
            key=lambda card: (
                _CONNECTOR_ORDER.get(card.connector, 999),
                card.connector,
            ),
        )
    )
    cards = "".join(
        _render_card(card, panel.profile.id, index)
        for index, card in enumerate(ordered_cards)
    )
    product_states = tuple(_product_status(card) for card in ordered_cards)
    action_count = product_states.count("action_required")
    unsynced_count = product_states.count("not_synced")
    if action_count:
        rollup_title = f"Нужно ваше внимание: {action_count}."
        rollup_detail = (
            "Откройте карточки с красным статусом — там указано следующее действие."
        )
    elif unsynced_count:
        rollup_title = f"Подключено, ждём первую синхронизацию: {unsynced_count}."
        rollup_detail = (
            "После первого успешного запуска статус обновится автоматически."
        )
    else:
        rollup_title = "Всё работает."
        rollup_detail = "Подключения и локальные компоненты доступны."
    notice_html = ""
    if notice:
        notice_class = "notice error" if notice_is_error else "notice"
        notice_html = f'<p class="{notice_class}" role="status">{escape(notice)}</p>'
    folders = "\n".join(panel.drive_folder_ids)
    destinations = (
        "".join(_render_destination(destination) for destination in panel.destinations)
        or '<p class="muted">Быстрых ссылок пока нет.</p>'
    )
    content = f"""<a class="back" href="/">← Все профили</a><p class="eyebrow">Ежедневный обзор</p><h1>Профиль: {escape(panel.profile.name)}</h1>
<details class="profile-details"><summary>Техническая информация профиля</summary><p>ID профиля: {panel.profile.id}</p></details>
{notice_html}<div class="rollup" role="status"><strong>{rollup_title}</strong><p>{rollup_detail}</p></div>
<section aria-labelledby="system-status"><h2 id="system-status">Состояние системы</h2><div class="cards">{cards}</div></section>
<section aria-labelledby="destinations"><h2 id="destinations">Открыть</h2><div class="destinations">{destinations}</div></section>
<details class="settings"><summary>Настройки Google Drive</summary><form method="post" action="/profiles/{panel.profile.id}/drive"><h2>Настроить Google Drive</h2>
<p class="muted">Одна или несколько папок, по одной ссылке или ID на строке. Сохранение заменит текущий список.</p>
<label for="drive-folders">Ссылки на папки</label><textarea id="drive-folders" name="folders" required maxlength="3000" autocomplete="off" spellcheck="false">{escape(folders)}</textarea>
<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}"><button type="submit">Сохранить папки</button></form></details>"""
    return _page(f"Health Agent — {panel.profile.name}", content)


def _render_card(card: ConnectorCard, profile_id: UUID, index: int) -> str:
    product_status = _product_status(card)
    label = _PRODUCT_STATUS_LABELS[product_status]
    connector_label = _CONNECTOR_LABELS.get(card.connector, card.connector)
    heading_id = f"connector-{index}"
    last_success = ""
    if card.last_success_at is not None:
        last_success = (
            f'<p class="muted">Последняя синхронизация: '
            f"{escape(_human_time(card.last_success_at))}</p>"
        )
    elif product_status == "not_synced":
        last_success = '<p class="muted">Успешной синхронизации ещё не было.</p>'
    error = f"<p>Код ошибки: {escape(card.error_code)}</p>" if card.error_code else ""
    accounts = ""
    if card.account_ids:
        account_label = "Аккаунт" if len(card.account_ids) == 1 else "Аккаунты"
        account_values = ", ".join(
            escape(account_id) for account_id in card.account_ids
        )
        accounts = f"<p>{account_label}: {account_values}</p>"
    action = ""
    if product_status == "action_required":
        action = f'<p class="action">{escape(_human_action(card))}</p>'
    raw_time = (
        f"<p>Точное время: {escape(card.last_success_at.isoformat())}</p>"
        if card.last_success_at
        else ""
    )
    guidance = _cli_guidance(card, profile_id)
    return f"""<article class="card" data-state="{product_status}" aria-labelledby="{heading_id}">
<div class="card-head"><h3 id="{heading_id}">{escape(connector_label)}</h3><span class="status-pill">{label}</span></div>
<p>{escape(card.detail)}</p>{last_success}{action}<details class="technical-details"><summary>Подробности</summary>
<p>Технический статус: {escape(card.status)}</p>{accounts}{raw_time}{error}<p>Команда проверки: {escape(guidance)}</p></details></article>"""


def _product_status(card: ConnectorCard) -> str:
    if card.error_code is not None:
        return "action_required"
    if card.status in {"ready", "configured"}:
        if card.last_success_at is not None or card.connector in {
            "telegram",
            "reminders",
            "database",
        }:
            return "connected"
        return "not_synced"
    return "action_required"


def _human_time(value: datetime) -> str:
    return (
        f"{value.day} {_MONTHS[value.month - 1]} {value.year}, "
        f"{value.hour:02d}:{value.minute:02d}"
    )


def _human_action(card: ConnectorCard) -> str:
    status_allows_error_remediation = card.status in {"ready", "configured"}
    if status_allows_error_remediation and card.error_code == "rate_limited":
        return (
            "Подождите следующей автоматической попытки; "
            "переподключение не требуется."
        )
    if (
        status_allows_error_remediation
        and card.error_code is not None
        and card.error_code not in _AUTH_ERROR_CODES
    ):
        return (
            "Повторите синхронизацию позже. Если ошибка повторится, "
            "откройте подробности."
        )
    if card.connector == "drive":
        return (
            "Добавьте папку Google Drive ниже."
            if card.status == "not_configured"
            else "Переподключите Google Drive."
        )
    if card.connector == "whoop":
        return "Подключите или переподключите WHOOP."
    if card.connector == "gmail":
        return "Подключите или переподключите Gmail."
    if card.connector == "telegram":
        return "Завершите подключение Telegram."
    if card.connector == "reminders":
        return "Подтвердите или обработайте напоминания в Telegram."
    if card.connector == "database":
        return "Проверьте, что локальная база запущена."
    return "Откройте подробности и проверьте локальную настройку."


def _render_destination(destination: PanelDestination) -> str:
    label = escape(destination.label)
    safe_url = _safe_destination_url(destination)
    if safe_url is not None:
        return (
            f'<article class="destination"><a href="{escape(safe_url, quote=True)}" '
            f'rel="noreferrer">{label} →</a><p>Открыть локально</p></article>'
        )
    unavailable = destination.unavailable_text or "Сейчас недоступно"
    return (
        f'<article class="destination"><strong>{label}</strong>'
        f"<p>{escape(unavailable)}</p></article>"
    )


def _safe_destination_url(destination: PanelDestination) -> str | None:
    if destination.url is None:
        return None
    try:
        parsed = urlsplit(destination.url)
        _ = parsed.port
    except ValueError:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    if destination.key == "metabase":
        if parsed.scheme not in {"http", "https"}:
            return None
        try:
            is_loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            is_loopback = parsed.hostname == "localhost"
        return destination.url if is_loopback else None
    if (
        destination.key == "google_sheets"
        and parsed.scheme == "https"
        and parsed.hostname == "docs.google.com"
        and _GOOGLE_SHEET_PATH.fullmatch(parsed.path)
    ):
        return destination.url
    return None


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
