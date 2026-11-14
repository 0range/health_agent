import pytest

from health_agent.races.sources import (
    SourceChanged,
    collect,
    discover_running,
    parse_iron,
    parse_running,
)

RACE = {"name": "Апрель", "km": 5, "url": "https://aprilrun5km.runc.run/"}


def event(year=2027, distance="5 км", buttons=""):
    return f"""<header>Регистрация 2027 <a href="/race/999/">Зарегистрироваться</a></header>
    <h1 class="run-intro__heading">Апрель</h1>
    <div class="run-intro__date">4 апреля {year}</div>
    <div class="run-intro-distances">{distance}</div>
    <div class="run-intro-botton-buttons">{buttons}</div>
    <form><a href="#" data-tab-target="#secondTab">Зарегистрироваться</a></form>"""


REGISTER = '<a href="https://runc.run/race/123/">Зарегистрироваться</a>'


def test_old_season_does_not_open_even_with_registration_and_new_year_in_navigation():
    obs = parse_running(event(2026, buttons=REGISTER), RACE, 2027, lambda _: "")
    assert obs.status == "not_announced"
    assert obs.date == ""
    assert obs.registration_url == ""


def test_account_signup_and_header_links_do_not_open_event():
    assert parse_running(event(), RACE, 2027, lambda _: "").status == "not_open"


def test_current_single_distance_has_actionable_registration_link():
    obs = parse_running(event(buttons=REGISTER), RACE, 2027, lambda _: "")
    assert obs.status == "open"
    assert obs.registration_url == "https://runc.run/race/123/"


@pytest.mark.parametrize(
    "attrs", ["disabled", 'aria-disabled="true"', "hidden", 'style="display: none"']
)
def test_disabled_or_hidden_action_does_not_open_event(attrs):
    html = event(buttons=f"<div {attrs}>{REGISTER}</div>")
    assert parse_running(html, RACE, 2027, lambda _: "").status == "not_open"


def test_other_distance_or_shared_login_wall_cannot_prove_target_open():
    html = event(distance="5 км • 10 км", buttons=REGISTER)
    assert (
        parse_running(
            html, RACE, 2027, lambda _: "<select><option>10 км</option></select>"
        ).status
        == "verify_distance"
    )
    assert (
        parse_running(html, RACE, 2027, lambda _: "<h1>Войти</h1>").status
        == "verify_distance"
    )
    disabled = "<select><option disabled>5 км</option></select>"
    assert (
        parse_running(html, RACE, 2027, lambda _: disabled).status == "verify_distance"
    )


def test_enabled_target_option_proves_shared_registration():
    html = event(distance="5 км • 10 км", buttons=REGISTER)
    form = '<select name="distance"><option value="5">5 км</option></select>'
    assert parse_running(html, RACE, 2027, lambda _: form).status == "open"


def test_explicit_other_distance_button_is_ignored():
    html = event(
        distance="5 км • 10 км",
        buttons=REGISTER.replace(">Зарегистрироваться<", ">Зарегистрироваться 10 км<"),
    )
    assert parse_running(html, RACE, 2027, lambda _: "").status == "not_open"


def test_distance_boundary_does_not_match_15_or_21_point_5_km():
    assert (
        parse_running(
            event(distance="15 км • 21,5 км"), RACE, 2027, lambda _: ""
        ).status
        == "distance_not_listed"
    )


def test_layout_change_is_error():
    with pytest.raises(SourceChanged):
        parse_running("<h1>Temporarily unavailable</h1>", RACE, 2027, lambda _: "")


def card(
    title="IRONSTAR 1/8",
    city="Сириус (Сочи)",
    slug="sirius-2027",
    year=2027,
    closed=False,
):
    reg = (
        "<span>Регистрация скоро откроется</span>"
        if closed
        else '<a href="">Регистрация</a>'
    )
    return f"""<div class="event-item-wrap"><a class="event-item" href="/event/{slug}">
      <div class="title">{title}</div><div class="date">04.10.{year}</div>
      <div class="place">{city}</div></a><div class="hover-buttons">{reg}</div></div>"""


def test_iron_filters_exact_type_year_and_city():
    html = (
        card()
        + card(title="IRONSTAR Супермикс")
        + card(city="Завидово", slug="zavidovo")
        + card(year=2026)
    )
    result = parse_iron(html, 2027, ["sochi", "luzhniki"], lambda _: "")
    assert len(result) == 2
    assert result[0].status == "open"
    assert result[0].registration_url == "https://iron-star.com/event/sirius-2027"
    assert result[1].key == "iron:pending:luzhniki"
    assert result[1].status == "not_announced"


def test_moscow_requires_luzhniki_evidence_and_soon_does_not_open():
    html = card(city="Москва", slug="moscow-2027", closed=True)
    result = parse_iron(
        html, 2027, ["luzhniki"], lambda _: "<main>Место старта: Лужники</main>"
    )
    assert result[0].status == "not_open"
    assert result[0].date == "04.10.2027"
    result = parse_iron(
        html,
        2027,
        ["luzhniki"],
        lambda _: "<main>Крылатское</main><footer>Лужники</footer>",
    )
    assert result[0].status == "not_announced"


def test_discovery_maps_short_distance_pages_and_deduplicates():
    html = """<div class="big-slider-item"><h2 class="big-slider-item__info-name">Московский полумарафон</h2>
    <div class="distances">21,1 км • 5 км</div><div class="big-slider-item__buttons">
    <a href="https://moscowhalf.runc.run/">Подробнее</a></div></div>"""
    found = discover_running(html + html)
    assert len(found) == 1
    assert found[0]["url"] == "https://moscowhalf5km.runc.run/"
    assert found[0]["km"] == 5


def test_partial_failure_preserves_other_observations_and_returns_error():
    def fetch(url):
        if "iron-star" in url:
            raise TimeoutError()
        return event(2026)

    config = {
        "year": 2027,
        "running": [RACE],
        "discover_running": False,
        "triathlon_locations": ["sochi"],
    }
    observations, errors = collect(config, fetch)
    assert len(observations) == 1
    assert errors == ["IRONSTAR: TimeoutError"]
