"""Read public event-specific evidence; never infer registration from account UI."""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

RUNC = "https://runc.run/"
IRON = "https://iron-star.com/event"
Fetch = Callable[[str], str]
KM = re.compile(r"(?<![\d.,])([\d]+(?:[.,]\d+)?)\s*(?:км|km)\b", re.IGNORECASE)
REG = re.compile(
    r"^(?:зарегистрироваться|регистрация|register|registration)(?:\s|$)", re.IGNORECASE
)


class SourceChanged(ValueError):
    """The source no longer supplies the evidence required by its adapter."""


@dataclass(frozen=True)
class Observation:
    key: str
    name: str
    url: str
    status: str
    date: str = ""
    registration_url: str = ""


def text(node: Tag | BeautifulSoup | None) -> str:
    return node.get_text(" ", strip=True) if node else ""


def distances(value: str) -> set[float]:
    return {float(x.replace(",", ".")) for x in KM.findall(value)}


def key_for(url: str, km: int) -> str:
    p = urlparse(url)
    return f"runc:{p.netloc}{p.path.rstrip('/')}:{km}"


def official_url(url: str) -> bool:
    p = urlparse(url)
    return p.scheme == "https" and (
        p.hostname == "iron-star.com"
        or p.hostname == "runc.run"
        or (p.hostname or "").endswith(".runc.run")
    )


def active(node: Tag) -> bool:
    for el in [node, *node.parents]:
        if not isinstance(el, Tag):
            continue
        style = str(el.get("style", "")).replace(" ", "").lower()
        if (
            el.has_attr("disabled")
            or el.has_attr("hidden")
            or el.get("aria-disabled") == "true"
            or el.get("aria-hidden") == "true"
            or "disabled" in el.get("class", [])
            or "display:none" in style
        ):
            return False
    return True


def discover_running(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".big-slider-item")
    if not cards:
        raise SourceChanged("running_calendar_layout")
    found = {}
    for card in cards:
        link = card.select_one(".big-slider-item__buttons a[href]")
        if not link:
            continue
        url = urljoin(RUNC, str(link["href"]))
        if not official_url(url):
            continue
        for km in (5, 10):
            if km not in distances(text(card.select_one(".distances"))):
                continue
            target = url
            # These distances have their own pages and own registration actions.
            if urlparse(url).hostname == "moscowhalf.runc.run" and km == 5:
                target = "https://moscowhalf5km.runc.run/"
            if urlparse(url).hostname == "moscowmarathon.runc.run" and km == 10:
                target = "https://moscowmarathon10km.runc.run/"
            found[key_for(target, km)] = {
                "name": text(card.select_one(".big-slider-item__info-name")),
                "km": km,
                "url": target,
            }
    return list(found.values())


def parse_running(html: str, race: dict, year: int, fetch: Fetch) -> Observation:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one("h1.run-intro__heading")
    date = text(soup.select_one(".run-intro__date"))
    year_match = re.search(r"\b(20\d{2})\b", date)
    if title is None or year_match is None:
        raise SourceChanged("running_event_date_or_title")
    name = f"{race['name']} · {race['km']} км"
    base = {"key": key_for(race["url"], race["km"]), "name": name, "url": race["url"]}
    if int(year_match[1]) != year:
        return Observation(**base, status="not_announced")
    offered = distances(text(soup.select_one(".run-intro-distances")))
    if race["km"] not in offered:
        return Observation(**base, status="distance_not_listed", date=date)
    buttons = soup.select(".run-intro-botton-buttons a[href]")
    for button in buttons:
        if not active(button) or not REG.search(text(button)):
            continue
        link = urljoin(race["url"], str(button["href"]))
        if not official_url(link) or not re.search(r"/race/\d+/?", urlparse(link).path):
            continue
        labeled = distances(text(button))
        if labeled and race["km"] not in labeled:
            continue
        if offered == {float(race["km"])} or labeled == {float(race["km"])}:
            return Observation(**base, status="open", date=date, registration_url=link)
        # Shared registration can be open for just one distance. A login wall
        # supplies no distance evidence; surface the new link with that caveat.
        registration = BeautifulSoup(fetch(link), "html.parser")
        for el in registration.select("option, label"):
            if distances(text(el)) == {float(race["km"])} and active(el):
                if el.name == "label":
                    control = registration.find(id=el.get("for", ""))
                    if not control or not active(control):
                        continue
                return Observation(
                    **base, status="open", date=date, registration_url=link
                )
        return Observation(
            **base, status="verify_distance", date=date, registration_url=link
        )
    # A recognized current-year hero with no actionable registration is evidence
    # of no public registration link, not proof of a specific future opening date.
    return Observation(**base, status="not_open", date=date)


def parse_iron(
    html: str, year: int, locations: list[str], fetch: Fetch
) -> list[Observation]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".event-item-wrap")
    if not cards:
        raise SourceChanged("ironstar_calendar_layout")
    names = {
        "sochi": "IRONSTAR 1/8 · Сочи / Сириус",
        "luzhniki": "IRONSTAR 1/8 · Лужники",
    }
    found = []
    seen_locations = set()
    for card in cards:
        if text(card.select_one(".title")).upper() != "IRONSTAR 1/8":
            continue
        date = text(card.select_one(".date"))
        if not re.search(rf"\b{year}\b", date):
            continue
        anchor = card.select_one("a.event-item[href]")
        if anchor is None:
            raise SourceChanged("ironstar_event_link")
        url = urljoin(IRON, str(anchor["href"]))
        if not official_url(url):
            raise SourceChanged("ironstar_nonofficial_link")
        venue = (
            text(card.select_one(".place"))
            + " "
            + str(anchor.get("title", ""))
            + " "
            + url
        ).lower()
        if re.search(r"сочи|сириус|sochi|sirius", venue):
            location = "sochi"
        elif re.search(r"лужник|luzhniki", venue):
            location = "luzhniki"
        elif re.search(r"москва|moscow", venue) and "luzhniki" in locations:
            detail = BeautifulSoup(fetch(url), "html.parser")
            for el in detail.select("header,footer,nav,script,style,.event-item-wrap"):
                el.decompose()
            if not re.search(r"лужник|luzhniki", text(detail), re.IGNORECASE):
                continue
            location = "luzhniki"
        else:
            continue
        if location not in locations:
            continue
        seen_locations.add(location)
        closed = re.search(
            r"скоро|закрыта|распродан|нет мест|sold out|soon|closed",
            text(card),
            re.IGNORECASE,
        )
        registration = next(
            (
                b
                for b in card.select(".hover-buttons a, .hover-buttons button")
                if REG.search(text(b)) and active(b)
            ),
            None,
        )
        # IRONSTAR uses a JS modal (href="") for its event registration. The
        # event detail URL is the stable public entry point to that modal.
        opened = registration is not None and closed is None
        found.append(
            Observation(
                key=f"iron:{urlparse(url).path.rstrip('/')}",
                name=names[location],
                url=url,
                status="open" if opened else "not_open",
                date=date,
                registration_url=url if opened else "",
            )
        )
    for location in locations:
        if location not in seen_locations:
            found.append(
                Observation(
                    f"iron:pending:{location}", names[location], IRON, "not_announced"
                )
            )
    return found


def collect(config: dict, fetch: Fetch) -> tuple[list[Observation], list[str]]:
    errors = []
    races = {key_for(r["url"], r["km"]): r for r in config["running"]}
    if config.get("discover_running", True):
        try:
            for race in discover_running(fetch(RUNC)):
                races.setdefault(key_for(race["url"], race["km"]), race)
        except Exception as exc:  # noqa: BLE001 -- source failure must be reported without losing other checks
            errors.append(f"Календарь Бегового сообщества: {type(exc).__name__}")
    excluded = set(config.get("excluded_keys", []))
    races = {k: v for k, v in races.items() if k not in excluded}
    # Fetch each official page once even when two distances use it.
    pages = {}

    def get(url: str) -> tuple[str, str | Exception]:
        try:
            return url, fetch(url)
        except Exception as exc:  # noqa: BLE001 -- source failure must be reported without losing other checks
            return url, exc

    with ThreadPoolExecutor(max_workers=4) as pool:
        for url, result in pool.map(get, sorted({r["url"] for r in races.values()})):
            pages[url] = result
    observations = []
    for race in races.values():
        try:
            html = pages[race["url"]]
            if isinstance(html, Exception):
                raise html
            observations.append(parse_running(html, race, config["year"], fetch))
        except Exception as exc:  # noqa: BLE001 -- source failure must be reported without losing other checks
            errors.append(f"{race['name']} · {race['km']} км: {type(exc).__name__}")
    try:
        observations.extend(
            o
            for o in parse_iron(
                fetch(IRON),
                config["year"],
                config.get("triathlon_locations", []),
                fetch,
            )
            if o.key not in excluded
        )
    except Exception as exc:  # noqa: BLE001 -- source failure must be reported without losing other checks
        errors.append(f"IRONSTAR: {type(exc).__name__}")
    return observations, errors
