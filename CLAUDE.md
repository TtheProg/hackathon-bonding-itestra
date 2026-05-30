# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A hackathon multiplayer **Snake** competition. Teams run a bot that polls a shared
game server over HTTP and POSTs a direction each tick. The board is a **torus**
(wraps at all edges); the scoreboard metric is **snake length**. The real,
maintained bot lives in `SnakePythonClient/`. The other directories are starter
scaffolding (see below).

## Layout

- `SnakePythonClient/src/` — **the actual bot ("Ouroboros").** This is what you edit.
- `pythonClient/`, `javaClient/`, `test_client.py` — throwaway starter clients that
  only `POST /connect` to prove connectivity. Not used by the bot; safe to ignore.
- `server/server.exe` — the game server binary (Windows). Run by the organizers, not us.
- `Game.pdf`, `Hackathon_Introduction.pdf` — the official rules/spec (cannot be read by tooling here).
- `SnakePythonClient/logs/` — per-run logs, `ouroboros-<timestamp>.log`. Gitignored.

## Run / develop

```bash
cd SnakePythonClient/src
python main.py --team_name Ouroboros --game_name Ouroboros \
  --base_url http://192.168.7.211:3030 --password hermeticism
```
The `--base_url` is the organizers' LAN server and changes per event; override it.
Useful flags: `--auto-reset` (hands-free testing: restarts the round on death / when
stuck joining), `--reset` (reset once at startup), `-v` (per-POST DEBUG to console),
`--opp_k N` (how many nearest opponents minimax models — branching cost, default 2).

Offline engine tests (no server, pure simulation/search assertions):
```bash
cd SnakePythonClient/src && python _sanity.py
```
There is no test runner/lint config; `_sanity.py` is the whole test suite. Only
dependency is `requests` (`pip install -r requirements.txt`).

## Architecture (SnakePythonClient/src)

The design deliberately **decouples strategy from the wire format and from the
server's timing**, because both proved unreliable during the event.

- **`api.py`** — thin HTTP wrapper. Endpoints used: `GET /games/{game}/state`,
  `POST /games/{game}/snake/direction`, `POST /games/{game}/snake/activate`,
  `POST /games/{game}/reset`. Auth is HTTP basic `(team_name, password)`.
  `get_field` raises `ApiError` (carrying `.status`); `set_direction` returns the
  raw status code so the caller can detect 429s.
- **`Field.py`** — tolerant JSON → dataclass parser. Accepts `snake` *or* `snakes`,
  items as `[[x,y],"Kind"]` pairs *or* dicts. Apples vs. hazards split by `kind`.
- **`engine.py`** — all game logic on plain `SimState`/`SimSnake` objects:
  - `simulate()` advances one tick with **simultaneous** movement → growth →
    collision resolution, matching the server's rules (head-to-head = mutual death,
    tail-chase survives because tails vacate).
  - `evaluate()` heuristic: length dominates (×1000), then flood-fill free space
    (don't get boxed in), then apple proximity, with penalties for being adjacent
    to a longer enemy head. Death is `-1e9` scaled by plies survived.
  - `choose_direction()` — **anytime iterative-deepening paranoid minimax** with
    alpha-beta. "Paranoid" = modeled opponents jointly pick the worst-for-us reply.
    Calls `on_improve(dir)` each time a deeper search improves the best move.
- **`main.py`** — the tick loop and the two pieces that work around server quirks:
  1. **Runtime delta calibration.** `DEFAULT_DELTAS` (NORTH=y-1 etc.) is only a
     *guess*. Each tick it compares the commanded direction against how our head
     actually moved and **rewrites the direction→(dx,dy) map** if they disagree.
     Never hard-code the compass mapping as ground truth.
  2. **Background `Poster` thread.** Search keeps deepening while a separate thread
     re-POSTs the current best move. The server token-buckets requests
     (**HTTP 429 on bursts**, >~3/s), so posts are throttled to `POST_INTERVAL`
     (0.45s) and only the last post before a tick counts. Timing constants live at
     the top of `main.py`: `TICK_SECONDS`, `POST_INTERVAL`, `SEARCH_BUDGET`.

## Gotchas learned during the event

- **429 rate limiting is the main failure mode.** Keep GET to 1/tick and posts
  throttled. On a 429 from GET, back off ~0.3s and retry; don't tighten the cadence.
- A finished/stuck game **refuses joins until reset** — that's what `reset_game()` /
  `--auto-reset` are for. If `state_from_field` returns `None` we're not in the game
  yet (registration still pending); keep posting to retry.
- "Snake just goes straight" is usually **correct** behavior, not a bug: straight is
  the tie-break preference in `choose_direction` so the snake glides in open space.

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