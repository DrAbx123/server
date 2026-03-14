"""
main.py - Core backend for the AI battle platform.

This application provides a minimal FastAPI‑based service to register AI endpoints,
pair them for simulated matches and maintain ELO rankings under four distinct
format rules.  The actual Genius Invokation gameplay is intended to be
implemented via the upstream TypeScript simulator (piovium/genius‑invokation).
For the sake of demonstration this server uses stubbed match outcomes – the
`play_match` coroutine determines a winner arbitrarily if neither side times
out.  To integrate a real engine you should replace the stub with code that
drives the TypeScript simulator and communicates with remote AI services to
request actions.

Endpoints
---------

* ``POST /register`` – Register a new AI model.  Clients supply a unique
  ``name`` and an HTTP URL ``url`` where the platform can reach the AI.  The
  server will reject duplicate names.
* ``GET /rankings`` – Retrieve the current leaderboard as JSON.  Players
  without the minimum number of games (30 total and at least 15 against
  different opponents) will appear without a rank.
* ``POST /match/manual`` – Initiate a match between two specific models.
  Accepts ``mode`` (0–3) and the names of the two players.  Spawns a
  background task that plays the game and updates ratings.
* ``POST /match/auto`` – Request the server to automatically find an
  available opponent for a given model and queue a game.
* ``GET /`` – A simple HTML scoreboard rendered from a Jinja2 template.

The four ranking modes are enumerated as follows:

0. ``full`` – All cards and characters available.
1. ``limited_open`` – Only a subset of cards and characters are unlocked.
2. ``single_deck`` – Each player uses exactly three characters and thirty
   cards; no other selections are permitted.
3. ``single_deck_no_cards`` – Each player uses three characters with no cards;
   all dice are treated as wildcards.

Persistent state is stored in memory in this example.  A production system
should persist data to a database.  Rankings persist across server restarts
only if you hook in a durable storage layer.

Author: OpenAI ChatGPT Agent
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, HttpUrl

app = FastAPI(title="AI Battle Platform", version="0.1.0")

templates = Jinja2Templates(directory="templates")


class RegistrationRequest(BaseModel):
    """Payload for registering a new AI model."""

    name: str
    url: HttpUrl


class ManualMatchRequest(BaseModel):
    """Payload for manually requesting a match between two AI players."""

    player_a: str
    player_b: str
    mode: int

    def validate_mode(self) -> None:
        if self.mode not in range(4):
            raise HTTPException(status_code=400, detail="Invalid mode; must be 0, 1, 2 or 3.")


class AutoMatchRequest(BaseModel):
    """Payload for requesting an automatic match for a player."""

    player: str
    mode: int

    def validate_mode(self) -> None:
        if self.mode not in range(4):
            raise HTTPException(status_code=400, detail="Invalid mode; must be 0, 1, 2 or 3.")


@dataclass
class Player:
    """Represents an AI participant registered with the platform."""

    name: str
    url: str
    ratings: Dict[int, float] = field(default_factory=lambda: defaultdict(lambda: 1000.0))
    games_played: Dict[int, int] = field(default_factory=lambda: defaultdict(int))
    opponents_by_mode: Dict[int, Set[str]] = field(default_factory=lambda: defaultdict(set))

    def record_game(self, mode: int, opponent: str) -> None:
        self.games_played[mode] += 1
        self.opponents_by_mode[mode].add(opponent)

    def meets_threshold(self, mode: int) -> bool:
        """Return True if the player is eligible for ranking in the given mode."""
        return self.games_played[mode] >= 30 and len(self.opponents_by_mode[mode]) >= 15


class RankingManager:
    """Maintains ELO ratings and orchestrates match execution."""

    def __init__(self) -> None:
        self.players: Dict[str, Player] = {}
        # Lock to prevent concurrent updates to rating/state
        self._lock = asyncio.Lock()

    async def register_player(self, name: str, url: str) -> None:
        async with self._lock:
            if name in self.players:
                raise HTTPException(status_code=409, detail=f"Player '{name}' is already registered.")
            self.players[name] = Player(name=name, url=url)

    async def get_rankings(self) -> Dict[int, List[Tuple[int, str, float]]]:
        """Return leaderboard: mode -> list of (rank, player_name, rating)."""
        rankings: Dict[int, List[Tuple[int, str, float]]] = {}
        for mode in range(4):
            # filter players meeting threshold
            eligible = [p for p in self.players.values() if p.meets_threshold(mode)]
            # sort descending by rating
            sorted_players = sorted(eligible, key=lambda p: p.ratings[mode], reverse=True)
            rankings[mode] = [
                (idx + 1, p.name, p.ratings[mode]) for idx, p in enumerate(sorted_players)
            ]
        return rankings

    async def schedule_manual_match(
        self, player_a: str, player_b: str, mode: int, background_tasks: BackgroundTasks
    ) -> None:
        if player_a == player_b:
            raise HTTPException(status_code=400, detail="Cannot match a player against itself.")
        async with self._lock:
            if player_a not in self.players or player_b not in self.players:
                raise HTTPException(status_code=404, detail="One or both players are not registered.")
        # schedule match asynchronously
        background_tasks.add_task(self._play_and_update, player_a, player_b, mode)

    async def schedule_auto_match(self, player: str, mode: int, background_tasks: BackgroundTasks) -> None:
        """Find an opponent for player and schedule a match. Raises if none available."""
        async with self._lock:
            if player not in self.players:
                raise HTTPException(status_code=404, detail=f"Player '{player}' not registered.")
            candidates = [
                p.name
                for p in self.players.values()
                if p.name != player
            ]
        if not candidates:
            raise HTTPException(status_code=400, detail="No available opponents.")
        opponent = random.choice(candidates)
        background_tasks.add_task(self._play_and_update, player, opponent, mode)

    async def _play_and_update(self, player_a: str, player_b: str, mode: int) -> None:
        """Play a single match and update ELO ratings for both players.  This runs outside of the HTTP request context."""
        # Copy references to players for local use
        async with self._lock:
            pa = self.players[player_a]
            pb = self.players[player_b]

        # Play the game (returns 1 if a wins, 0 if b wins, 0.5 for draw)
        result = await play_match(pa, pb, mode)

        # Determine K-factor.  Use higher value for players with few games to allow quick convergence
        def k_factor(p: Player, mode: int) -> float:
            return 40.0 if p.games_played[mode] < 30 else 20.0

        async with self._lock:
            ra = pa.ratings[mode]
            rb = pb.ratings[mode]
            ea = 1.0 / (1.0 + 10 ** ((rb - ra) / 400))
            eb = 1.0 / (1.0 + 10 ** ((ra - rb) / 400))
            k_a = k_factor(pa, mode)
            k_b = k_factor(pb, mode)
            # Update ratings
            pa.ratings[mode] = ra + k_a * (result - ea)
            pb.ratings[mode] = rb + k_b * ((1.0 - result) - eb)
            # Record that a game occurred
            pa.record_game(mode, pb.name)
            pb.record_game(mode, pa.name)

        # Optionally log match outcome
        print(
            f"Match finished: {player_a} vs {player_b} (mode {mode}). Result: {result}. Ratings now {player_a}={pa.ratings[mode]:.2f}, {player_b}={pb.ratings[mode]:.2f}"
        )


ranking_manager = RankingManager()


@app.post("/register", status_code=201)
async def register(request: RegistrationRequest) -> Dict[str, str]:
    """Register a new AI model.  Name must be unique."""
    await ranking_manager.register_player(request.name, str(request.url))
    return {"message": f"Registered {request.name}"}


@app.get("/rankings")
async def get_rankings() -> Dict[int, List[Tuple[int, str, float]]]:
    return await ranking_manager.get_rankings()


@app.post("/match/manual")
async def manual_match(req: ManualMatchRequest, background_tasks: BackgroundTasks) -> Dict[str, str]:
    req.validate_mode()
    await ranking_manager.schedule_manual_match(req.player_a, req.player_b, req.mode, background_tasks)
    return {"message": "Match scheduled"}


@app.post("/match/auto")
async def auto_match(req: AutoMatchRequest, background_tasks: BackgroundTasks) -> Dict[str, str]:
    req.validate_mode()
    await ranking_manager.schedule_auto_match(req.player, req.mode, background_tasks)
    return {"message": "Match scheduled"}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    rankings = await ranking_manager.get_rankings()
    return templates.TemplateResponse("index.html", {"request": request, "rankings": rankings})


async def play_match(player_a: Player, player_b: Player, mode: int) -> float:
    """
    Simulate a match between two players and return the outcome for player_a.

    This stub sends a start handshake to each AI endpoint and waits up to
    ``timeout`` seconds for a response.  If one side times out or returns an
    error, the other is declared the winner.  If both respond, the result is
    chosen randomly.  Replace this stub with real game logic by invoking the
    genius‑invokation simulator.

    :param player_a: The first player.
    :param player_b: The second player.
    :param mode: Game mode (0–3).
    :return: 1.0 if A wins, 0.0 if B wins, 0.5 for draw.
    """
    timeout = 10.0  # seconds

    async def handshake(player: Player) -> bool:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    player.url,
                    json={"event": "start", "mode": mode},
                    timeout=timeout,
                )
                return response.status_code == 200
        except Exception:
            return False

    # Launch both handshakes concurrently
    start = time.time()
    res_a, res_b = await asyncio.gather(handshake(player_a), handshake(player_b))
    elapsed = time.time() - start
    # Determine result based on handshake outcomes
    if res_a and not res_b:
        return 1.0
    if res_b and not res_a:
        return 0.0
    if not res_a and not res_b:
        # Both timed out or failed – treat as draw
        return 0.5
    # Both responded in time; choose winner randomly
    return random.choice([1.0, 0.0])