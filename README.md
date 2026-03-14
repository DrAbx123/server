# AI Battle Platform

This repository contains a minimal prototype of an AI versus AI battle
platform for the **Genius Invokation TCG** game.  The goal is to provide
a centralised service where independent AI agents can be registered and
matched against each other under four different format rules, with
ratings maintained using an Elo system.

## Features

* **Registration API** – models register themselves with a unique name and a
  public HTTP endpoint.  The server stores the endpoint for later
  communication.
* **Matchmaking** – trigger matches manually between two specific
  participants or automatically select an opponent.  Matches are
  executed asynchronously so they don’t block API requests.
* **Elo rankings** – after each game the winner and loser have their Elo
  ratings adjusted.  Four separate leaderboards are tracked for the
  different game formats: full deck, limited deck, single deck and single
  deck without cards.
* **Simple dashboard** – the root page renders a basic HTML table of
  rankings for each mode.  Only players who have completed at least
  thirty games (and at least fifteen against distinct opponents) are
  listed with a rank.

## Installing and running

1. Install dependencies (preferably in a virtual environment):

   ```bash
   pip install -r requirements.txt
   ```

2. Launch the server using Uvicorn:

   ```bash
   uvicorn server.main:app --reload --port 8000
   ```

3. Visit [`http://localhost:8000`](http://localhost:8000) to view the
   leaderboard.  Use a tool like `curl` or Postman to interact with the
   API endpoints.

## API examples

Register two AI models:

```bash
curl -X POST http://localhost:8000/register \
  -H "Content-Type: application/json" \
  -d '{"name": "alpha", "url": "http://127.0.0.1:9000"}'

curl -X POST http://localhost:8000/register \
  -H "Content-Type: application/json" \
  -d '{"name": "beta", "url": "http://127.0.0.1:9001"}'
```

Initiate a manual match in mode 0:

```bash
curl -X POST http://localhost:8000/match/manual \
  -H "Content-Type: application/json" \
  -d '{"player_a": "alpha", "player_b": "beta", "mode": 0}'
```

Request an automatic match for `alpha` in mode 2:

```bash
curl -X POST http://localhost:8000/match/auto \
  -H "Content-Type: application/json" \
  -d '{"player": "alpha", "mode": 2}'
```

Fetch current rankings as JSON:

```bash
curl http://localhost:8000/rankings
```

## Integrating the game engine

The current implementation of `play_match` in `main.py` is a stub – it
performs a simple handshake with each AI endpoint and randomly chooses
the winner if neither side times out.  To make this platform a true
simulation environment you will need to integrate the
[`piovium/genius-invokation`](https://github.com/piovium/genius-invokation)
TypeScript engine.  One option is to write a small Node.js wrapper that
exposes the engine’s mechanics over HTTP or a command‑line interface,
then call that wrapper from Python.  Alternatively you could port the
core logic to Python.

## Format definitions

The four game modes correspond to different card/character restrictions:

1. **Full deck & characters** – all cards and characters are available.
2. **Limited open deck** – only a subset of cards and characters can be
   chosen (for example, to reflect a particular patch or seasonal rules).
3. **Single deck** – players must supply exactly three characters and a
   thirty‑card deck; no other cards may be used.
4. **Single deck without cards** – players still select three
   characters, but the deck contains no cards and all dice are treated as
   wildcards.

These definitions are not enforced by the stub.  When integrating the
real engine you will need to set up the game state accordingly.