# Ouroboros — Hackathon Snake Bot

A bot for a multiplayer **Snake** competition. Each team runs a client that polls a
shared game server over HTTP and POSTs a direction every tick. The board is a
**torus** (it wraps at every edge) and your score is your snake's **length**.

The competitive bot lives in [`SnakePythonClient/`](SnakePythonClient/). It runs an
**anytime iterative-deepening paranoid minimax** with alpha-beta pruning and a
flood-fill heuristic, and it calibrates itself to the server's quirks at runtime.

## Quick start

```bash
pip install -r requirements.txt

cd SnakePythonClient/src
python main.py \
  --team_name Ouroboros \
  --game_name Ouroboros \
  --password hermeticism \
  --base_url http://192.168.7.211:3030
```

The `--base_url` points at the organizers' LAN server and changes per event — set it
to whatever they announce.

### Useful flags

| Flag | Purpose |
| --- | --- |
| `--base_url URL` | Game server address (default `http://192.168.7.211:3030`). |
| `--team_name` / `--game_name` | Your snake's name and the game to join. |
| `--password` | Server password (HTTP basic auth, with the team name). |
| `--opp_k N` | How many nearest opponents minimax models (search branching cost, default 2). |
| `--reset` | Reset the game once before joining. |
| `--auto-reset` | Hands-free testing: restart the round on death or when stuck joining. |
| `-v` / `--verbose` | DEBUG logging on the console (per-POST detail). |

Every run also writes a full log to `SnakePythonClient/logs/ouroboros-<timestamp>.log`
(board snapshot, all snakes, the chosen move/depth/score, and POST status per tick).

## Tests

The engine has an offline sanity suite that runs the pure simulation/search with no
server:

```bash
cd SnakePythonClient/src
python _sanity.py
```

## How it works

The bot is split into layers so the strategy keeps working even if the server's JSON
shape or timing misbehaves (both did, during the event):

- **`api.py`** — thin HTTP wrapper over the server endpoints (`/state`,
  `/snake/direction`, `/snake/activate`, `/reset`).
- **`Field.py`** — tolerant parser that turns the server's JSON into dataclasses.
- **`engine.py`** — the game logic on plain state objects: torus tick simulation
  (simultaneous move → grow → collide), the position heuristic, and the minimax search.
- **`main.py`** — the tick loop, plus two workarounds for server quirks:
  - **Runtime direction calibration** — the NORTH/SOUTH/EAST/WEST → movement mapping
    is only a guess at startup; the bot watches how its head actually moves and
    rewrites the mapping if they disagree.
  - **Throttled background poster** — a separate thread re-POSTs the current best move
    while the search keeps deepening. The server rate-limits bursts (HTTP 429), so
    posts are throttled and only the last one before each tick counts.

See [`CLAUDE.md`](CLAUDE.md) for deeper architecture notes and gotchas.

## Repository layout

| Path | What it is |
| --- | --- |
| `SnakePythonClient/` | **The competitive bot.** This is the code that matters. |
| `pythonClient/`, `javaClient/`, `test_client.py` | Starter stubs that only `POST /connect` to prove connectivity. |
| `server/server.exe` | The game server binary (run by the organizers). |
| `Game.pdf`, `Hackathon_Introduction.pdf` | The official rules and game spec. |

## Requirements

Python 3 and `requests` (`pip install -r requirements.txt`).


# Hackathon Game:



Turnier der Bots ▪ Ihr programmiert Bots für das Spiel Snake ▪ Eure Bots treten in Turnieren gegeneinander an ▪ Zusätzlich zum klassischen Snake wird es später verschiedene Items geben 



Test Server bieten verschiedene Spiele an ▪ Wir hosten verschiedene Server ▪ Ihr könnt euch gegen die IPs verbinden ▪ Die Server bieten eine UI an, auf der ihr die laufenden Spiele anschauen und auch zurücksetzen könnt  ▪ Ihr bekommt auch die Möglichkeit Server selbstständig zu hosten



Ziel ▪ Sammelt Punkte in den Turnieren ▪ Das Team am Ende mit den meisten Punkten gewinnt 



Turnier ▪ Pro Turnier treten alle Teams in einem Match mit 4 Schlangen an ▪ Die Siegerteams (längste Schlange) spielen ein Match gegeneinander





Spielende: Wenn eins der folgenden Szenarien eintritt, endet das Spiel sofort: ▪ Keine Schlange lebt mehr ▪ Genau eine Schlange lebt noch ▪ Wenn das Spiel stagniert (z.B. zwei Schlangen laufen im Kreis, etc.) 



Punktesystem ▪ Die Basis Punkte pro Runde entsprechen der Länge eurer Schlange am Ende eines Spieles ▪ Es gewinnt die Schlange mit den meisten Punkten ▪ Die Schlange die am längsten überlebt bekommt einen Bonus, hat aber nicht automatisch gewonnen ▪ Die späteren Spiele geben extra Punkte • Grand Finale (Sonntag 10:00) 



Server▪ Der Spielserver tickt 1 Mal pro Sekunde* zu festen Intervallen▪ Pro Tick wird das Spielfeld aktualisiert▪ Das aktuelle Spielfeld kann am Server angefragt werden



Clients▪ Ihr fragt einmal pro Tick das Spielfeld ab▪ Ihr berechnet euren nächsten Schritt▪ Ihr postet die Richtung, in der sich eure Schlange im nächsten Tick bewegen soll • Norden, Westen, Süden, Osten▪ Es wird nur der letzte Post für den nächsten Tick berücksichtigt



Auslesen der Richtung▪ Für jede Schlange wird die zuletzt gepostete Richtung ausgewertet



Movement Phase▪ Alle Schlangen bewegen sich gleichzeitig ein Feld in die gesetzte Richtung▪ Das Spielfeld kann in alle Richtungen verlassen werden, die Schlange betritt es erneut am gegenüberliegenden Rand



Kollisionen▪ Berührt der Kopf einer Schlange einen beliebigen Körper:• Die Schlange stirbt▪ Alle Kollisionen werden gleichzeitig abgehandelt



Items Aufsammeln ▪ Jedes Item, was von einer Schlange mit dem Kopf berührt wird, wird aufgesammelt ▪ Äpfel sorgen dafür, dass die Schlange direkt um ein Feld wächst • Das letzte Feld der Schlange wird dupliziert 



Neue Items spawnen ▪ Nachdem alle Aktionen der Schlangen abgehandelt wurden spawnen neue Items ▪ Items spawnen zufällig, aber nicht auf bereits besetzten Feldern.



Bereitgestellte Clients▪ Minimale Gerüste in Java und Python▪ Ihr findet die Clients in Nextcloud▪ Benutzt gerne jede Sprache die ihr verwenden wollt!



Debugging und Testing▪ Testet eure Implementierung gegen andere Teams auf den Testservern▪ Testet gegen eure lokalen Server▪ Ihr könnt eigene Testszenarien erstellen



Verwendung von KI▪ Verwendet gerne jegliche Form von KI Unterstützung, die euch zur Verfügung steht▪ Vorsicht! Euer Code muss zu jedem Zeitpunkt an Änderungen des Spiels angepasst werden können!