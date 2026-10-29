# SportBit MCP-server

Een [MCP](https://modelcontextprotocol.io)-server voor [SportBit](https://www.sportbitmanager.nl/), het rooster- en inschrijfsysteem van veel CrossFit- en sportscholen. Hiermee kan Claude, of een andere MCP-client, het rooster bekijken, je inschrijven en afmelden voor trainingen, en je eigen trainingshistorie opvragen.

> Dit is een onofficieel project en is niet verbonden aan SportBit. Het gebruikt dezelfde REST-API als de SportBit-webapp. Als die API verandert, kan de server stoppen met werken.

## Tools

| Tool | Wat het doet |
|---|---|
| `lijst_trainingen` | Rooster voor één of meer dagen (`datum`, `dagen`), optioneel gefilterd op `tijd` (HH:MM) en `titel`. Geeft per training het `id` en je `status` (`aangemeld`, `wachtlijst` of leeg). |
| `mijn_trainingen` | Jouw trainingen uit het verleden. Met `dagen_terug` (1-365, standaard 30) bepaal je hoe ver je terugkijkt. Optioneel zijn `tot` (einddatum), `titel` en `inclusief_wachtlijst`. Geeft de lijst plus een telling per titel en per trainer. |
| `training_details` | Details van één training, inclusief `kan_aanmelden` en locatie. |
| `deelnemers` | Wie er is ingeschreven en wie op de wachtlijst staat. |
| `boek_training` | Schrijft je in voor een training. Controleert eerst of aanmelden mogelijk is. |
| `afmelden_training` | Meldt je af voor een training. |

Datums mogen als `JJJJ-MM-DD` of als `vandaag`, `morgen` of `gisteren`.

Voorbeelden van vragen aan Claude:

- *"Welke trainingen zijn er morgenochtend?"*
- *"Schrijf me in voor de CrossFit van 19:00 op donderdag."*
- *"Hoe vaak heb ik de afgelopen drie maanden getraind, en bij welke trainers?"*

## Installeren

Het makkelijkst gaat het met [uv](https://docs.astral.sh/uv/). De afhankelijkheden staan in de kop van het script, dus `uv` installeert ze zelf:

```bash
uv run sportbit_mcp.py
```

Zonder uv:

```bash
pip install "mcp>=1.2" httpx tzdata
python sportbit_mcp.py
```

Python 3.10 of nieuwer is vereist.

## Configureren

De server leest je inloggegevens uit environment variables. Je wachtwoord komt zo niet in de chat terecht.

| Variabele | Verplicht | Omschrijving |
|---|---|---|
| `SPORTBIT_USERNAME` | ja | Je SportBit-gebruikersnaam, meestal je e-mailadres |
| `SPORTBIT_PASSWORD` | ja | Je SportBit-wachtwoord |
| `SPORTBIT_HOST` | ja | Het SportBit-adres van je sportschool, bijvoorbeeld `mijnbox.sportbitapp.nl` |
| `SPORTBIT_MIN_INTERVAL` | nee | Seconden tussen verzoeken (standaard `0.5`) |
| `SPORTBIT_CACHE_DIR` | nee | Map voor de historie-cache (standaard `~/.cache/sportbit-mcp`) |
| `SPORTBIT_USER_AGENT` | nee | Eigen User-Agent voor de verzoeken |

Het adres van je sportschool vind je in de adresbalk als je inlogt op de SportBit-webapp.

### Claude Desktop

Voeg dit toe aan `claude_desktop_config.json` en herstart Claude Desktop:

```json
{
  "mcpServers": {
    "sportbit": {
      "command": "uv",
      "args": ["run", "/volledig/pad/naar/sportbit_mcp.py"],
      "env": {
        "SPORTBIT_USERNAME": "jouw@email.nl",
        "SPORTBIT_PASSWORD": "jouw-wachtwoord",
        "SPORTBIT_HOST": "mijnbox.sportbitapp.nl"
      }
    }
  }
}
```

Vindt Claude Desktop `uv` niet? Zet dan het volledige pad bij `command`. Dat vraag je op met `which uv` op macOS/Linux of `where uv` op Windows.

Zet dit configuratiebestand nooit in een openbare repository.

## Testen

Met de MCP Inspector kun je de tools los uitproberen, zonder Claude:

```bash
SPORTBIT_USERNAME=... SPORTBIT_PASSWORD=... SPORTBIT_HOST=... \
  npx @modelcontextprotocol/inspector uv run sportbit_mcp.py
```

## Hoe het werkt

**Sessie.** De server logt zelf in: eerst de loginpagina en een heartbeat voor de sessie-cookies, dan de login met de XSRF-token, en daarna een heartbeat ter controle. De sessie wordt na 15 minuten vernieuwd, en ook als SportBit laat weten dat ze verlopen is. Tussen verzoeken zit standaard een halve seconde, zodat de server SportBit niet overbelast.

**Gebruikte endpoints:**

| Methode | Pad | Doel |
|---|---|---|
| `GET` | `/web/nl/login` | Sessie-cookie ophalen |
| `GET` | `/cbm/api/data/heartbeat/` | Sessie en XSRF-token |
| `POST` | `/cbm/api/data/inloggen/` | Inloggen |
| `GET` | `/cbm/api/data/events/?datum=JJJJ-MM-DD` | Rooster van één dag |
| `GET` | `/cbm/api/web/event/{id}/` | Details van een training |
| `GET` | `/cbm/api/data/events/{id}/deelnemers/` | Deelnemers |
| `POST` | `/cbm/api/data/events/{id}/deelname/` | Inschrijven |
| `DELETE` | `/cbm/api/data/events/{id}/deelname/` | Afmelden |

### Over `mijn_trainingen`

SportBit heeft geen aparte endpoint voor je historie. Deze tool vraagt daarom het rooster per dag op en houdt de trainingen over waarvoor je ingeschreven was. Dat kost één verzoek per dag: 30 dagen duurt ongeveer 15 seconden, een heel jaar ongeveer 3 minuten.

Afgelopen dagen veranderen niet meer en worden daarom lokaal bewaard in `historie.json` in de cachemap. Dezelfde periode nog eens opvragen is dan direct klaar. Gooi dat bestand weg als je de cache wilt legen.

SportBit registreert inschrijvingen, geen aanwezigheid. Een training waarvoor je ingeschreven stond maar waar je niet was, telt dus mee.

## Dank

De afmeld-endpoint is gevonden dankzij [jeweet/sportbit](https://github.com/jeweet/sportbit).
