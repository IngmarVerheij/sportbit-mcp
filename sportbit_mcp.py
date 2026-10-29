# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2", "httpx>=0.27", "tzdata"]
# ///
"""SportBit MCP-server.

MCP-server voor SportBit (sportbitapp.nl): inloggen, rooster opvragen,
trainingdetails, deelnemers, inschrijven, afmelden en je eigen trainingshistorie.

Configuratie via environment variables:
  SPORTBIT_USERNAME     (verplicht, meestal je e-mailadres)
  SPORTBIT_PASSWORD     (verplicht)
  SPORTBIT_HOST         (verplicht, het SportBit-adres van je sportschool,
                         bijvoorbeeld mijnbox.sportbitapp.nl)
  SPORTBIT_USER_AGENT   (optioneel)
  SPORTBIT_MIN_INTERVAL (optioneel, seconden tussen verzoeken, standaard 0.5)
  SPORTBIT_CACHE_DIR    (optioneel, standaard ~/.cache/sportbit-mcp)
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

TZ = ZoneInfo("Europe/Amsterdam")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0"
)
SESSION_MAX_AGE = 15 * 60  # sessie na 15 minuten vernieuwen
MIN_INTERVAL = float(os.environ.get("SPORTBIT_MIN_INTERVAL", "0.5"))
MAX_DAGEN_TERUG = 365
DAGDELEN = ("ochtend", "middag", "avond")


class SportBitError(Exception):
    """Fout bij de communicatie met SportBit."""


class SportBitClient:
    def __init__(self, host: str, username: str, password: str, user_agent: str):
        host = host.removeprefix("https://").removeprefix("http://").strip("/")
        self.host = host
        self.base_url = f"https://{host}"
        self.username = username
        self.password = password
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json, text/plain, */*",
            },
            timeout=30.0,
            follow_redirects=True,
        )
        self._logged_in_at = 0.0
        self._last_request = 0.0
        self._lock = threading.Lock()

    # --- laag niveau -------------------------------------------------------

    def _throttle(self) -> None:
        wait = MIN_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _raw(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self._throttle()
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SportBitError(f"Geen verbinding met SportBit: {exc}") from exc

    def _xsrf(self) -> str:
        token = None
        for cookie in self._http.cookies.jar:
            if cookie.name == "XSRF-TOKEN":
                token = cookie.value
        if not token:
            raise SportBitError("Geen XSRF-TOKEN cookie ontvangen van SportBit.")
        return token

    def _write_headers(self) -> dict[str, str]:
        """Headers voor POST/DELETE: altijd de meest recente XSRF-token."""
        return {
            "X-XSRF-TOKEN": self._xsrf(),
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/web/nl/",
        }

    @staticmethod
    def _check(resp: httpx.Response, stap: str) -> None:
        if resp.status_code != 200:
            raise SportBitError(f"{stap} mislukt (HTTP {resp.status_code}).")

    # --- sessie ------------------------------------------------------------

    def login(self) -> None:
        """Loginpagina, heartbeat, inloggen, en een heartbeat ter controle."""
        self._http.cookies.clear()
        self._check(self._raw("GET", "/web/nl/login"), "Loginpagina openen")
        self._check(
            self._raw("GET", "/cbm/api/data/heartbeat/", params={"taalIso": "nl"}),
            "Heartbeat",
        )
        resp = self._raw(
            "POST",
            "/cbm/api/data/inloggen/",
            json={
                "username": self.username,
                "password": self.password,
                "remember": False,
            },
            headers=self._write_headers(),
        )
        if resp.status_code != 200:
            raise SportBitError(
                f"Inloggen mislukt (HTTP {resp.status_code}). "
                "Controleer SPORTBIT_USERNAME en SPORTBIT_PASSWORD."
            )
        self._check(
            self._raw("GET", "/cbm/api/data/heartbeat/", params={"taalIso": "nl"}),
            "Heartbeat na inloggen",
        )
        self._logged_in_at = time.monotonic()

    def request(
        self, method: str, path: str, *, expect: tuple[int, ...] = (200,), **kwargs: Any
    ) -> Any:
        with self._lock:
            if (
                not self._logged_in_at
                or time.monotonic() - self._logged_in_at > SESSION_MAX_AGE
            ):
                self.login()

            headers = dict(kwargs.pop("headers", None) or {})
            if method != "GET":
                headers.update(self._write_headers())
            resp = self._raw(method, path, headers=headers, **kwargs)

            # Sessie verlopen: één keer opnieuw inloggen en het verzoek herhalen.
            if resp.status_code in (401, 403, 419):
                self.login()
                if method != "GET":
                    headers.update(self._write_headers())
                resp = self._raw(method, path, headers=headers, **kwargs)

            if resp.status_code not in expect:
                raise SportBitError(
                    f"{method} {path} gaf HTTP {resp.status_code}: {resp.text[:300]}"
                )
            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text

    # --- SportBit API ------------------------------------------------------

    def rooster(self, dag: date) -> dict:
        return self.request(
            "GET", "/cbm/api/data/events/", params={"datum": dag.isoformat()}
        )

    def event_details(self, training_id: int) -> dict:
        return self.request("GET", f"/cbm/api/web/event/{training_id}/")

    def deelnemers(self, training_id: int) -> list:
        return self.request("GET", f"/cbm/api/data/events/{training_id}/deelnemers/")

    def aanmelden(self, training_id: int) -> None:
        self.request(
            "POST",
            f"/cbm/api/data/events/{training_id}/deelname/",
            json={},
            expect=(200, 201, 204),
        )

    def afmelden(self, training_id: int) -> None:
        self.request(
            "DELETE",
            f"/cbm/api/data/events/{training_id}/deelname/",
            expect=(200, 204),
        )


_client: SportBitClient | None = None


def client() -> SportBitClient:
    global _client
    if _client is None:
        host = os.environ.get("SPORTBIT_HOST")
        username = os.environ.get("SPORTBIT_USERNAME")
        password = os.environ.get("SPORTBIT_PASSWORD")
        ontbrekend = [
            naam
            for naam, waarde in (
                ("SPORTBIT_HOST", host),
                ("SPORTBIT_USERNAME", username),
                ("SPORTBIT_PASSWORD", password),
            )
            if not waarde
        ]
        if ontbrekend:
            raise SportBitError(
                f"{', '.join(ontbrekend)} ontbreekt in de MCP-configuratie."
            )
        _client = SportBitClient(
            host=host,
            username=username,
            password=password,
            user_agent=os.environ.get("SPORTBIT_USER_AGENT", DEFAULT_USER_AGENT),
        )
    return _client


# --- cache voor de historie ----------------------------------------------------
# Afgelopen dagen veranderen niet meer, dus die worden lokaal bewaard. Een tweede
# keer dezelfde periode opvragen is daardoor direct klaar.


class HistorieCache:
    def __init__(self) -> None:
        basis = os.environ.get("SPORTBIT_CACHE_DIR") or Path.home() / ".cache" / "sportbit-mcp"
        self.pad = Path(basis) / "historie.json"
        self._data: dict[str, dict[str, list[dict]]] | None = None

    def _load(self) -> dict[str, dict[str, list[dict]]]:
        if self._data is None:
            try:
                self._data = json.loads(self.pad.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def get(self, sleutel: str, dag: date) -> list[dict] | None:
        return self._load().get(sleutel, {}).get(dag.isoformat())

    def put(self, sleutel: str, dag: date, trainingen: list[dict]) -> None:
        self._load().setdefault(sleutel, {})[dag.isoformat()] = trainingen

    def save(self) -> None:
        try:
            self.pad.parent.mkdir(parents=True, exist_ok=True)
            self.pad.write_text(json.dumps(self._load()), encoding="utf-8")
        except OSError:
            pass  # cache is een optimalisatie; zonder werkt alles ook


_cache = HistorieCache()


# --- helpers -----------------------------------------------------------------


def _vandaag() -> date:
    return datetime.now(TZ).date()


def _parse_datum(datum: str | None) -> date:
    vandaag = _vandaag()
    if not datum or datum.strip().lower() in ("vandaag", "today"):
        return vandaag
    if datum.strip().lower() in ("morgen", "tomorrow"):
        return vandaag + timedelta(days=1)
    if datum.strip().lower() in ("gisteren", "yesterday"):
        return vandaag - timedelta(days=1)
    try:
        return date.fromisoformat(datum.strip())
    except ValueError as exc:
        raise SportBitError(
            f"Ongeldige datum '{datum}'. Gebruik JJJJ-MM-DD, 'vandaag', 'morgen' of 'gisteren'."
        ) from exc


def _naam(obj: Any) -> Any:
    return obj.get("naam") if isinstance(obj, dict) else obj


def _status(t: dict) -> str | None:
    # SportBit kan bij een wachtlijstplek zowel 'aangemeld' als 'opWachtlijst'
    # teruggeven; de wachtlijst heeft dan voorrang.
    if t.get("opWachtlijst"):
        return "wachtlijst"
    if t.get("aangemeld"):
        return "aangemeld"
    return None


def _samenvatting(t: dict, dagdeel: str) -> dict:
    start = t.get("start") or ""
    return {
        "id": t.get("id"),
        "titel": t.get("titel"),
        "datum": start[:10],
        "tijd": start[11:16],
        "start": t.get("start"),
        "eind": t.get("eind"),
        "dagdeel": dagdeel,
        "trainer": _naam(t.get("trainer")),
        "ruimte": _naam(t.get("ruimte")),
        "max_deelnemers": t.get("maxDeelnemers"),
        "aangemeld": bool(t.get("aangemeld")),
        "op_wachtlijst": bool(t.get("opWachtlijst")),
        "status": _status(t),
    }


def _dag_samenvattingen(dag: date) -> list[dict]:
    rooster = client().rooster(dag) or {}
    return [
        _samenvatting(t, dagdeel)
        for dagdeel in DAGDELEN
        for t in rooster.get(dagdeel) or []
    ]


# --- MCP tools ---------------------------------------------------------------

mcp = FastMCP("sportbit")

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


@mcp.tool(annotations=READ_ONLY)
def lijst_trainingen(
    datum: str | None = None,
    dagen: int = 1,
    tijd: str | None = None,
    titel: str | None = None,
    volledig: bool = False,
) -> list[dict]:
    """Haal trainingen uit het SportBit-rooster op.

    Args:
        datum: Startdatum als JJJJ-MM-DD, 'vandaag', 'morgen' of 'gisteren'. Standaard vandaag.
        dagen: Aantal dagen vanaf de startdatum (1-14). Standaard 1.
        tijd: Alleen trainingen die op deze tijd starten, als 'HH:MM'.
        titel: Alleen trainingen waarvan de titel deze tekst bevat (hoofdletterongevoelig).
        volledig: True geeft de ruwe SportBit-gegevens in plaats van een samenvatting.

    Elke training heeft een 'id' voor training_details, deelnemers, boek_training en
    afmelden_training. 'status' is 'aangemeld', 'wachtlijst' of leeg.
    """
    start = _parse_datum(datum)
    dagen = max(1, min(int(dagen), 14))
    resultaat: list[dict] = []
    for i in range(dagen):
        rooster = client().rooster(start + timedelta(days=i)) or {}
        for dagdeel in DAGDELEN:
            for t in rooster.get(dagdeel) or []:
                kort = _samenvatting(t, dagdeel)
                if tijd and kort["tijd"] != tijd.strip():
                    continue
                if titel and titel.strip().lower() not in (kort["titel"] or "").lower():
                    continue
                resultaat.append(t if volledig else kort)
    return resultaat


@mcp.tool(annotations=READ_ONLY)
def mijn_trainingen(
    dagen_terug: int = 30,
    tot: str | None = None,
    titel: str | None = None,
    inclusief_wachtlijst: bool = False,
) -> dict:
    """Geef je eigen trainingen uit het verleden: de trainingen waarvoor je was ingeschreven.

    Args:
        dagen_terug: Hoeveel dagen terug je wilt kijken (1-365). Standaard 30.
        tot: Laatste dag van de periode (JJJJ-MM-DD of 'gisteren'/'vandaag'). Standaard gisteren.
        titel: Alleen trainingen waarvan de titel deze tekst bevat (hoofdletterongevoelig).
        inclusief_wachtlijst: Ook trainingen waarvoor je alleen op de wachtlijst stond.

    Geeft de lijst met trainingen (nieuwste eerst) plus het totaal en een telling per
    titel en per trainer. Afgelopen dagen worden lokaal bewaard, dus een lange periode
    duurt alleen de eerste keer even (ongeveer een halve seconde per dag).
    Let op: SportBit geeft inschrijvingen, geen aanwezigheid. Een training waarvoor je
    was ingeschreven maar niet kwam, telt dus mee.
    """
    sb = client()
    vandaag = _vandaag()
    eind = _parse_datum(tot) if tot else vandaag - timedelta(days=1)
    if eind > vandaag:
        eind = vandaag
    dagen_terug = max(1, min(int(dagen_terug), MAX_DAGEN_TERUG))
    begin = eind - timedelta(days=dagen_terug - 1)
    sleutel = f"{sb.host}|{sb.username.lower()}"

    eigen: list[dict] = []
    opgehaald = 0
    try:
        for i in range(dagen_terug):
            dag = eind - timedelta(days=i)
            trainingen = _cache.get(sleutel, dag) if dag < vandaag else None
            if trainingen is None:
                trainingen = [t for t in _dag_samenvattingen(dag) if t["status"]]
                opgehaald += 1
                if dag < vandaag:
                    _cache.put(sleutel, dag, trainingen)
            eigen.extend(trainingen)
    finally:
        _cache.save()  # ook bij een fout halverwege blijft het opgehaalde bewaard

    if not inclusief_wachtlijst:
        eigen = [t for t in eigen if t["status"] == "aangemeld"]
    if titel:
        eigen = [t for t in eigen if titel.strip().lower() in (t["titel"] or "").lower()]
    eigen.sort(key=lambda t: t.get("start") or "", reverse=True)

    return {
        "periode": {"van": begin.isoformat(), "tot": eind.isoformat()},
        "aantal": len(eigen),
        "per_titel": dict(Counter(t["titel"] for t in eigen).most_common()),
        "per_trainer": dict(Counter(t["trainer"] for t in eigen).most_common()),
        "dagen_opgehaald_bij_sportbit": opgehaald,
        "trainingen": eigen,
    }


@mcp.tool(annotations=READ_ONLY)
def training_details(training_id: int) -> dict:
    """Haal de details van één training op, waaronder of aanmelden mogelijk is
    (toegang.aanmelden) en de locatie."""
    details = client().event_details(training_id) or {}
    toegang = details.get("toegang") or {}
    details["kan_aanmelden"] = bool(toegang.get("aanmelden"))
    return details


@mcp.tool(annotations=READ_ONLY)
def deelnemers(training_id: int) -> dict:
    """Geef de deelnemers van een training, inclusief wie op de wachtlijst staat."""
    lijst = client().deelnemers(training_id) or []
    namen = [
        {"naam": d.get("naam"), "op_wachtlijst": bool(d.get("opWachtlijst"))}
        for d in lijst
    ]
    return {
        "training_id": training_id,
        "aantal_ingeschreven": sum(1 for d in namen if not d["op_wachtlijst"]),
        "aantal_wachtlijst": sum(1 for d in namen if d["op_wachtlijst"]),
        "deelnemers": namen,
    }


def _training_info(details: dict, training_id: int) -> dict:
    return {
        "training_id": training_id,
        "titel": details.get("titel"),
        "start": details.get("start"),
        "eind": details.get("eind"),
        "locatie": details.get("locatie"),
        "trainer": _naam(details.get("trainer")),
    }


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, openWorldHint=True
    )
)
def boek_training(training_id: int) -> dict:
    """Schrijf je in voor een training. Controleert eerst of aanmelden mogelijk is.
    Gebruik lijst_trainingen om het id van de juiste training te vinden."""
    sb = client()
    details = sb.event_details(training_id) or {}
    toegang = details.get("toegang") or {}
    if not toegang.get("aanmelden"):
        return {
            "geboekt": False,
            "reden": "Aanmelden is voor deze training niet mogelijk "
            "(al aangemeld, vol, of nog niet open).",
            "toegang": toegang,
        }
    sb.aanmelden(training_id)
    return {"geboekt": True, **_training_info(details, training_id)}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, openWorldHint=True
    )
)
def afmelden_training(training_id: int) -> dict:
    """Meld je af voor een training waarvoor je bent ingeschreven of op de wachtlijst staat.
    Gebruik lijst_trainingen om het id te vinden; 'status' laat zien of je ingeschreven bent.
    Let op: bij een volle training kan je plek direct naar de wachtlijst gaan."""
    sb = client()
    details = sb.event_details(training_id) or {}
    sb.afmelden(training_id)
    return {"afgemeld": True, **_training_info(details, training_id)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
