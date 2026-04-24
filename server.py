"""
server.py — Πthon Arena authoritative game server.

Usage:
    python server.py <port>

Architecture
------------
 * One thread per connected client, reading protocol messages in a loop.
 * One match runs at a time (per spec); additional players wait in the lobby
   or spectate the active match.
 * A match owns a dedicated game-loop thread running at TICK_RATE Hz. It
   advances snake positions, spawns pies, checks collisions, shrinks the
   arena, and broadcasts STATE to players + spectators.
 * Shared mutable state (players dict, current_match) is guarded by locks.

Design choices
--------------
 * Text-line JSON over TCP: reliable, ordered, trivial to frame and debug.
 * Server is authoritative: clients send only intent (direction); server
   computes positions. Prevents cheating and desyncs.
 * P2P chat: server only tells each player the other's IP + chat port
   during MATCH_START, then stays out of the chat path entirely.
"""

import socket
import threading
import random
import time
import sys
from collections import deque

from protocol import (
    encode, decode, recv_line,
    LOGIN, LOGIN_OK, LOGIN_FAIL, LIST_PLAYERS, LOBBY,
    CHALLENGE, INCOMING_CHAL, CHALLENGE_REPLY, CHAL_DECLINED,
    SPECTATE, LEAVE_MATCH, MOVE, CHAT_INFO,
    MATCH_START, STATE, GAME_OVER, ERROR,
    CHEER, CHEER_FWD,
)

# Game configuration
GRID_W, GRID_H   = 30, 20          # Arena in cells
TICK_RATE        = 10              # game updates per second
COUNTDOWN_SECONDS = 10             # pre-match countdown before snakes can move
START_HEALTH     = 100             # starting score
WIN_SCORE        = 600             # first player to reach this score wins
WALL_THRESHOLD   = 200             # once anyone hits this, walls begin spawning
SAFETY_TIMEOUT   = 600             # hard cap of 10 min so matches can't hang
PIE_SPAWN_EVERY  = 2.0             # seconds
MAX_PIES         = 6
NUM_STATIC_OBST  = 8                # initial obstacles at match start

# Dynamic wall growth: once the highest score passes a threshold,
# a new obstacle block spawns at the given cadence (seconds).
# Thresholds checked in descending order — the first one matched wins.
WALL_GROWTH_STAGES = [
    (500, 2.0),
    (350, 4.0),
    (WALL_THRESHOLD, 6.0),
]
MAX_DYNAMIC_WALLS = 40              # cap so the board doesn't fill completely

NUM_MOVING_OBST  = 3

# Pie types: (name, score_delta, spawn_weight)
PIE_TYPES = [
    ("apple",   10, 4),   # small heal
    ("cherry",  20, 6),   # standard (most common)
    ("golden",  40, 1),   # rare big heal
    ("rotten", -20, 2),   # trap
]

# Collision damage
DMG_WALL        = 30
DMG_OBSTACLE    = 25
DMG_SELF        = 40
DMG_HEAD_BODY   = 35   # you ran into opponent's body
DMG_HEAD_HEAD   = 50   # head-on with opponent

DIRS = {"UP": (0, -1), "DOWN": (0, 1), "LEFT": (-1, 0), "RIGHT": (1, 0)}
OPPOSITE = {"UP": "DOWN", "DOWN": "UP", "LEFT": "RIGHT", "RIGHT": "LEFT"}


# Player: everything the server knows about one connected client
class Player:
    def __init__(self, sock, addr):
        self.sock        = sock
        self.addr        = addr      # (ip, port) of the TCP connection
        self.username    = None
        self.status      = "connecting"   # connecting | lobby | playing | spectating
        self.chat_port   = None      # port they're listening on for P2P chat
        self.send_lock   = threading.Lock()

    def send(self, msg_type, **fields):
        """Thread-safe send. Silently drop if socket is broken."""
        try:
            with self.send_lock:
                self.sock.sendall(encode(msg_type, **fields))
        except (OSError, BrokenPipeError):
            pass


# Snake: per-player state inside an active match
class Snake:
    def __init__(self, player, start_pos, start_dir, color_idx):
        self.player    = player
        self.body      = deque([start_pos])  # head is body[0]
        self.direction = start_dir
        self.pending   = start_dir            # next direction to apply
        self.health    = START_HEALTH
        self.color_idx = color_idx
        self.alive     = True
        self.grow      = 2                    # grow a bit at start

    @property
    def head(self):
        return self.body[0]

    def set_direction(self, new_dir):
        # Ignore reversing into yourself
        if new_dir in DIRS and new_dir != OPPOSITE[self.direction]:
            self.pending = new_dir


# Match: everything that happens during one game
class Match:
    def __init__(self, p1: Player, p2: Player, server):
        self.server = server
        self.id = f"{p1.username}_vs_{p2.username}"
        self.snakes = [
            Snake(p1, (5, GRID_H // 2), "RIGHT", 0),
            Snake(p2, (GRID_W - 6, GRID_H // 2), "LEFT",  1),
        ]
        self.spectators = []           # list of Player
        self.pies = []                 # list of (x, y, type_name, delta)
        self.static_obstacles = list(self._gen_static_obstacles())
        self.moving_obstacles = self._gen_moving_obstacles()
        self.phase = "countdown"       # "countdown" | "playing"
        self.start_time = time.time()  # reset to game-start when countdown ends
        self.countdown_start = time.time()
        self.last_pie_spawn = 0.0
        self.last_wall_spawn = 0.0     # for dynamic wall growth
        self.tick = 0
        self.running = True
        self.lock = threading.Lock()
        self.pending_cheers = []       # (from_user, emoji) drained each tick

    #setup helpers

    def _gen_static_obstacles(self):
        obstacles = []
        forbidden = {s.head for s in self.snakes}
        # avoid spawning right next to snake heads
        for s in self.snakes:
            hx, hy = s.head
            for dx in range(-3, 4):
                forbidden.add((hx + dx, hy))
        while len(obstacles) < NUM_STATIC_OBST:
            x = random.randint(2, GRID_W - 3)
            y = random.randint(2, GRID_H - 3)
            if (x, y) not in forbidden and (x, y) not in obstacles:
                obstacles.append((x, y))
        return obstacles

    def _gen_moving_obstacles(self):
        """Horizontal patrollers with (x, y, dx, range_min, range_max)."""
        movers = []
        for _ in range(NUM_MOVING_OBST):
            y = random.randint(3, GRID_H - 4)
            x0 = random.randint(5, GRID_W - 10)
            x1 = x0 + random.randint(3, 6)
            movers.append({"x": x0, "y": y, "dx": 1, "min": x0, "max": x1})
        return movers

    #runtime 

    def player_by_username(self, name):
        for s in self.snakes:
            if s.player.username == name:
                return s
        return None

    def handle_move(self, username, direction):
        with self.lock:
            s = self.player_by_username(username)
            if s and s.alive:
                s.set_direction(direction)

    def add_spectator(self, player):
        with self.lock:
            if player not in self.spectators:
                self.spectators.append(player)
                player.status = "spectating"

    def remove_spectator(self, player):
        with self.lock:
            if player in self.spectators:
                self.spectators.remove(player)

    def queue_cheer(self, from_user, emoji):
        with self.lock:
            self.pending_cheers.append({"from": from_user, "emoji": emoji})

    #main loop

    def run(self):
        interval = 1.0 / TICK_RATE
        next_tick = time.time()
        try:
            while self.running:
                now = time.time()
                if now < next_tick:
                    time.sleep(next_tick - now)
                next_tick += interval

                self._step()
                self._broadcast_state()

                if self._check_end():
                    break
        finally:
            self._end_match()

    def _step(self):
        with self.lock:
            self.tick += 1

            #countdown phase: freeze everything until the timer runs out
            if self.phase == "countdown":
                elapsed_cd = time.time() - self.countdown_start
                if elapsed_cd >= COUNTDOWN_SECONDS:
                    # transition to live play: reset start_time so the match
                    # clock starts at 0 the moment the countdown hits 0
                    self.phase = "playing"
                    self.start_time = time.time()
                return

            elapsed = time.time() - self.start_time

            #periodic systems
            if elapsed - self.last_pie_spawn >= PIE_SPAWN_EVERY and len(self.pies) < MAX_PIES:
                self._spawn_pie()
                self.last_pie_spawn = elapsed

            # Dynamic wall growth — cadence depends on the leader's score
            cadence = self._wall_spawn_cadence()
            if cadence is not None and (elapsed - self.last_wall_spawn) >= cadence:
                if len(self.static_obstacles) < NUM_STATIC_OBST + MAX_DYNAMIC_WALLS:
                    self._spawn_wall()
                    self.last_wall_spawn = elapsed

            for mo in self.moving_obstacles:
                mo["x"] += mo["dx"]
                if mo["x"] >= mo["max"] or mo["x"] <= mo["min"]:
                    mo["dx"] *= -1

            #advance snakes
            for s in self.snakes:
                if not s.alive:
                    continue
                s.direction = s.pending
                dx, dy = DIRS[s.direction]
                new_head = (s.head[0] + dx, s.head[1] + dy)
                s.body.appendleft(new_head)
                if s.grow > 0:
                    s.grow -= 1
                else:
                    s.body.pop()

            #collisions & pie pickup
            self._resolve_collisions()

    def _spawn_pie(self):
        occupied = set()
        for s in self.snakes:
            occupied.update(s.body)
        occupied.update(self.static_obstacles)
        occupied.update((p[0], p[1]) for p in self.pies)
        for _ in range(30):  # try a few spots
            x = random.randint(1, GRID_W - 2)
            y = random.randint(1, GRID_H - 2)
            if (x, y) in occupied:
                continue
            names, deltas, weights = zip(*PIE_TYPES)
            idx = random.choices(range(len(PIE_TYPES)), weights=weights)[0]
            name, delta, _ = PIE_TYPES[idx]
            self.pies.append((x, y, name, delta))
            return

    def _wall_spawn_cadence(self):
        """
        Return the seconds-between-spawns based on the highest current score,
        or None if no player has crossed the wall threshold yet.
        """
        leader = max(s.health for s in self.snakes)
        for threshold, cadence in WALL_GROWTH_STAGES:
            if leader >= threshold:
                return cadence
        return None

    def _spawn_wall(self):
        """
        Place a single new obstacle block somewhere reasonable: not on a
        snake, not on a pie, not right in front of a snake's head, not on
        an existing obstacle.
        """
        occupied = set(self.static_obstacles)
        for s in self.snakes:
            occupied.update(s.body)
            # don't spawn walls right in front of a moving snake (unfair)
            hx, hy = s.head
            dx, dy = DIRS[s.direction]
            for k in range(1, 4):
                occupied.add((hx + dx * k, hy + dy * k))
        occupied.update((p[0], p[1]) for p in self.pies)
        occupied.update((mo["x"], mo["y"]) for mo in self.moving_obstacles)

        for _ in range(40):
            x = random.randint(1, GRID_W - 2)
            y = random.randint(1, GRID_H - 2)
            if (x, y) not in occupied:
                self.static_obstacles.append((x, y))
                return

    def _resolve_collisions(self):
        min_x, max_x = 0, GRID_W - 1
        min_y, max_y = 0, GRID_H - 1
        static = set(self.static_obstacles)
        moving = {(mo["x"], mo["y"]) for mo in self.moving_obstacles}

        # Snake bodies excluding own head (we check self-collision separately)
        body_maps = {
            s.player.username: set(list(s.body)[1:]) for s in self.snakes
        }

        heads = [s.head for s in self.snakes if s.alive]
        head_on = len(heads) == 2 and heads[0] == heads[1]

        for s in self.snakes:
            if not s.alive:
                continue
            hx, hy = s.head

            # Wall
            if hx < min_x or hx > max_x or hy < min_y or hy > max_y:
                s.health -= DMG_WALL
                self._recoil(s)
                continue

            # Static obstacle
            if (hx, hy) in static:
                s.health -= DMG_OBSTACLE
                self._recoil(s)
                continue

            # Moving obstacle
            if (hx, hy) in moving:
                s.health -= DMG_OBSTACLE
                self._recoil(s)
                continue

            # Self
            if (hx, hy) in body_maps[s.player.username]:
                s.health -= DMG_SELF
                self._recoil(s)
                continue

            # Head-on with opponent
            if head_on:
                s.health -= DMG_HEAD_HEAD
                self._recoil(s)
                continue

            # Head into opponent body
            for other in self.snakes:
                if other is s:
                    continue
                if (hx, hy) in body_maps[other.player.username]:
                    s.health -= DMG_HEAD_BODY
                    self._recoil(s)
                    break
            else:
                # Pie pickup only if no collision happened this tick
                for i, (px, py, name, delta) in enumerate(self.pies):
                    if (hx, hy) == (px, py):
                        s.health = min(WIN_SCORE, s.health + delta)
                        if delta > 0:
                            s.grow += 1
                        self.pies.pop(i)
                        break

            if s.health <= 0:
                s.alive = False

    def _recoil(self, snake):
        """After a collision, undo the move and clamp head inside arena."""
        if len(snake.body) > 1:
            snake.body.popleft()
        # re-grow so length stays consistent
        snake.grow += 1

    def _check_end(self):
        # Never end during the pre-match countdown
        if self.phase == "countdown":
            return False
        alive = [s for s in self.snakes if s.alive and s.health > 0]
        if len(alive) < 2:
            return True
        # Someone reached the score cap
        if any(s.health >= WIN_SCORE for s in self.snakes):
            return True
        # Safety timeout
        if time.time() - self.start_time >= SAFETY_TIMEOUT:
            return True
        return False

    def _build_state(self):
        leader_score = max(s.health for s in self.snakes)
        walls_active = leader_score >= WALL_THRESHOLD

        if self.phase == "countdown":
            cd_elapsed = time.time() - self.countdown_start
            countdown_remaining = max(0, int(COUNTDOWN_SECONDS - cd_elapsed) + 1)
            if countdown_remaining > COUNTDOWN_SECONDS:
                countdown_remaining = COUNTDOWN_SECONDS
            elapsed = 0
        else:
            countdown_remaining = 0
            elapsed = int(time.time() - self.start_time)

        return {
            "tick": self.tick,
            "phase": self.phase,
            "countdown": countdown_remaining,
            "arena": {
                "w": GRID_W, "h": GRID_H,
            },
            "snakes": [
                {
                    "user":  s.player.username,
                    "body":  list(s.body),
                    "dir":   s.direction,
                    "hp":    s.health,
                    "color": s.color_idx,
                    "alive": s.alive,
                } for s in self.snakes
            ],
            "pies": [
                {"x": p[0], "y": p[1], "type": p[2], "delta": p[3]}
                for p in self.pies
            ],
            "static_obstacles": self.static_obstacles,
            "moving_obstacles": [(mo["x"], mo["y"]) for mo in self.moving_obstacles],
            "win_score": WIN_SCORE,
            "wall_threshold": WALL_THRESHOLD,
            "walls_active": walls_active,
            "num_walls": len(self.static_obstacles),
            "elapsed": elapsed,
            "spectators": len(self.spectators),
            "cheers": self.pending_cheers,
        }

    def _broadcast_state(self):
        with self.lock:
            state = self._build_state()
            self.pending_cheers = []
        recipients = [s.player for s in self.snakes] + list(self.spectators)
        for p in recipients:
            p.send(STATE, **state)

    def _end_match(self):
        # Decide winner and reason
        alive = [s for s in self.snakes if s.alive and s.health > 0]

        # Score-reached takes priority if someone has actually crossed it
        top = max(self.snakes, key=lambda s: s.health)
        if top.health >= WIN_SCORE:
            winner = top.player.username
            reason = "score_reached"
        elif len(alive) == 1:
            winner = alive[0].player.username
            reason = "opponent_eliminated"
        elif len(alive) == 0:
            winner = None
            reason = "both_eliminated"
        else:
            # safety timeout fallback — higher score wins
            s0, s1 = self.snakes
            if s0.health > s1.health:
                winner = s0.player.username
            elif s1.health > s0.health:
                winner = s1.player.username
            else:
                winner = None
            reason = "timeout"

        scores = {s.player.username: s.health for s in self.snakes}

        recipients = [s.player for s in self.snakes] + list(self.spectators)
        for p in recipients:
            p.send(GAME_OVER, winner=winner, scores=scores, reason=reason)
            if p.sock:
                p.status = "lobby"

        self.running = False
        self.server.end_match(self)


# Server
class Server:
    def __init__(self, port):
        self.port = port
        self.players = {}          # username -> Player
        self.unnamed = set()       # Players who haven't logged in yet
        self.players_lock = threading.Lock()
        self.current_match = None
        self.pending_challenges = {}   # target_username -> challenger_username

    # lifecycle

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", self.port))
        s.listen()
        print(f"[server] listening on port {self.port}")
        try:
            while True:
                csock, caddr = s.accept()
                p = Player(csock, caddr)
                with self.players_lock:
                    self.unnamed.add(p)
                threading.Thread(target=self._client_loop, args=(p,), daemon=True).start()
        except KeyboardInterrupt:
            print("\n[server] shutting down")
        finally:
            s.close()

    #per-client loop

    def _client_loop(self, player: Player):
        try:
            while True:
                line = recv_line(player.sock)
                if not line:
                    break
                try:
                    msg = decode(line)
                except Exception:
                    player.send(ERROR, message="malformed message")
                    continue
                self._dispatch(player, msg)
        except OSError:
            pass
        finally:
            self._cleanup(player)

    def _dispatch(self, player, msg):
        t = msg.get("type")

        if t == LOGIN:
            self._handle_login(player, msg.get("username", "").strip())

        elif t == CHAT_INFO:
            player.chat_port = int(msg.get("port", 0)) or None

        elif t == LIST_PLAYERS:
            self._send_lobby_to(player)

        elif t == CHALLENGE:
            self._handle_challenge(player, msg.get("target"))

        elif t == CHALLENGE_REPLY:
            self._handle_challenge_reply(player, msg.get("from"), bool(msg.get("accept")))

        elif t == SPECTATE:
            self._handle_spectate(player)

        elif t == LEAVE_MATCH:
            self._handle_leave(player)

        elif t == MOVE:
            if self.current_match and player.status == "playing":
                self.current_match.handle_move(player.username, msg.get("direction"))

        elif t == CHEER:
            if self.current_match and player.status == "spectating":
                self.current_match.queue_cheer(player.username, msg.get("emoji", "👏"))

        elif t is None:
            player.send(ERROR, message="missing type field")

        else:
            # Unknown types are silently ignored so a client in the wrong
            # state so it doesn't get flooded with errors.
            pass

    #handlers

    def _handle_login(self, player, username):
        if not username or len(username) > 16 or not username.replace("_", "").isalnum():
            player.send(LOGIN_FAIL, reason="Username must be 1-16 alphanumeric chars")
            return
        with self.players_lock:
            if username in self.players:
                player.send(LOGIN_FAIL, reason="Username already in use")
                return
            player.username = username
            player.status = "lobby"
            self.players[username] = player
            self.unnamed.discard(player)
        player.send(LOGIN_OK, username=username)
        self._broadcast_lobby()

    def _handle_challenge(self, player, target):
        if not target or target == player.username:
            player.send(ERROR, message="invalid challenge target")
            return
        with self.players_lock:
            tgt = self.players.get(target)
        if not tgt:
            player.send(ERROR, message=f"{target} is not online")
            return
        if tgt.status != "lobby":
            player.send(ERROR, message=f"{target} is busy")
            return
        if self.current_match:
            player.send(ERROR, message="a match is already in progress")
            return
        self.pending_challenges[target] = player.username
        tgt.send(INCOMING_CHAL, **{"from": player.username})

    def _handle_challenge_reply(self, player, from_user, accept):
        # player is the one who was challenged; from_user challenged them
        if self.pending_challenges.get(player.username) != from_user:
            return
        del self.pending_challenges[player.username]
        with self.players_lock:
            challenger = self.players.get(from_user)
        if not challenger:
            return
        if not accept:
            challenger.send(CHAL_DECLINED, by=player.username)
            return
        if self.current_match:
            challenger.send(ERROR, message="another match started first")
            return
        self._start_match(challenger, player)

    def _handle_spectate(self, player):
        if not self.current_match:
            player.send(ERROR, message="no active match")
            return
        self.current_match.add_spectator(player)
        # tell them the match exists — first STATE will arrive next tick
        self._broadcast_lobby()

    def _handle_leave(self, player):
        if player.status == "playing" and self.current_match:
            # forfeit: set health to 0
            s = self.current_match.player_by_username(player.username)
            if s:
                s.alive = False
                s.health = 0
        elif player.status == "spectating" and self.current_match:
            self.current_match.remove_spectator(player)
            player.status = "lobby"
            self._send_lobby_to(player)

    #match lifecycle

    def _start_match(self, p1, p2):
        p1.status = "playing"
        p2.status = "playing"
        m = Match(p1, p2, self)
        self.current_match = m

        # tell each player who they are and give them the peer's chat info
        for me, other in ((p1, p2), (p2, p1)):
            me.send(
                MATCH_START,
                you=me.username,
                opponent=other.username,
                peer_ip=other.addr[0],
                peer_chat_port=other.chat_port,
                config={
                    "grid_w": GRID_W,
                    "grid_h": GRID_H,
                    "tick_rate": TICK_RATE,
                    "win_score": WIN_SCORE,
                    "wall_threshold": WALL_THRESHOLD,
                    "start_health": START_HEALTH,
                },
            )

        threading.Thread(target=m.run, daemon=True).start()
        self._broadcast_lobby()

    def end_match(self, match):
        if self.current_match is match:
            self.current_match = None
        self._broadcast_lobby()

    #lobby helpers

    def _lobby_snapshot(self):
        with self.players_lock:
            players = [
                {"username": u, "status": p.status}
                for u, p in self.players.items()
            ]
        matches = []
        if self.current_match:
            matches.append({
                "id": self.current_match.id,
                "players": [s.player.username for s in self.current_match.snakes],
                "spectators": len(self.current_match.spectators),
            })
        return {"players": players, "matches": matches}

    def _broadcast_lobby(self):
        snap = self._lobby_snapshot()
        with self.players_lock:
            targets = list(self.players.values())
        for p in targets:
            p.send(LOBBY, **snap)

    def _send_lobby_to(self, player):
        player.send(LOBBY, **self._lobby_snapshot())

    #cleanup

    def _cleanup(self, player):
        with self.players_lock:
            if player.username and self.players.get(player.username) is player:
                del self.players[player.username]
            self.unnamed.discard(player)
        # if they were in a match, forfeit them
        if self.current_match and player.status == "playing":
            s = self.current_match.player_by_username(player.username)
            if s:
                s.alive = False
                s.health = 0
        elif self.current_match and player.status == "spectating":
            self.current_match.remove_spectator(player)
        try:
            player.sock.close()
        except OSError:
            pass
        self._broadcast_lobby()
        if player.username:
            print(f"[server] {player.username} disconnected")


# Entry point
def main():
    if len(sys.argv) != 2:
        print("Usage: python server.py <port>")
        sys.exit(1)
    try:
        port = int(sys.argv[1])
    except ValueError:
        print("Port must be an integer")
        sys.exit(1)
    Server(port).start()


if __name__ == "__main__":
    main()