# Πthon Arena

An online two-player snake battle game implemented for **EECE 350 — Computing Networks**.

Two players connect to a centralized server, log in with unique usernames, challenge each other through a lobby, and compete in real-time matches. Players collect pies to raise their score while avoiding walls, obstacles, and the opposing snake. First to reach 600 points wins.

---

## Quick start

```bash
# Install dependency (Pygame is the only external one)
pip install pygame

# Start the server on any free port
python server.py 5000

# Launch a client (on the same or another machine on your LAN)
python client.py
```

In the client: enter the server IP and port, pick a username, and you're in the lobby. For local testing on one machine, use `127.0.0.1`.

---

## Controls

| Context | Key | Action |
|---------|-----|--------|
| Lobby | *click player row* | Challenge that player |
| Lobby | *click Watch Match* | Spectate the active match |
| Customize | *click swatch / row* | Pick snake color or rebind a key |
| In game | Arrow keys (default) | Move your snake |
| In game | `T` | Focus the chat input |
| In game | `Enter` (while typing) | Send chat message |
| In game | `Esc` | Forfeit the match / return to lobby |
| Spectating | `Space` | Quick cheer (👏) |
| Post-match | *Close Chat* | Terminate chat early for both players |
| Post-match | *Return to Lobby* | Leave the post-match screen |

---

## Repository layout

```
pithon_arena/
├── protocol.py     Shared wire-protocol constants and framing helpers
├── server.py       Authoritative game server (lobby, matchmaking, game loop)
├── client.py       Pygame client (UI, input, rendering, P2P chat)
└── README.md       This file
```

All three Python files are self-contained. There are no custom asset files — every graphic is drawn procedurally at runtime.

---

## File-by-file overview

### `protocol.py` (~95 lines)

The single source of truth for the wire protocol. Defines:

- **Message-type constants** (e.g. `LOGIN`, `STATE`, `MATCH_START`, `P2P_CHAT`) that both client and server import, so there's no way for the two sides to disagree on wire strings.
- **`encode(type, **fields)`** — serializes a message to a newline-terminated JSON line ready to send on a TCP socket.
- **`decode(line)`** — parses one line back into a dict.
- **`recv_line(sock)`** — reads bytes from a socket until it hits `\n`, returning the raw line. Returns `b""` if the peer closed the connection.

Keep this file small. Any change to the wire format goes here first.

### `server.py` (~500 lines)

Authoritative game server. Accepts client connections, runs the lobby, mediates matchmaking, and runs the game loop for active matches.

Main classes:

- **`Player`** — everything the server knows about one connected client: socket, address, username, status (`connecting` / `lobby` / `playing` / `spectating`), and a send-lock so concurrent broadcasts don't interleave.
- **`Snake`** — a player's in-match state: body deque with the head at index 0, direction, pending direction (queued for the next tick), score (called `health` internally for historical reasons), color index, alive flag, and pending growth counter.
- **`Match`** — one live game. Owns the snakes, pies, static and moving obstacles, the game-loop thread, and the list of spectators. Includes the pre-match countdown phase, wall-spawn logic, collision resolution, and end-of-match reasoning.
- **`Server`** — the top-level class. Holds the accept loop, the shared players dict, the mutex guarding it, and the reference to the current match. Dispatches client messages to their handlers.

Run it with:

```bash
python server.py <port>
```

### `client.py` (~900 lines)

Pygame client with six screens (connect, login, lobby, customize, game, post-match), a threaded network receiver, a peer-to-peer chat implementation, and a HUD + arena + chat-panel renderer.

Main classes:

- **`TextInput` / `Button`** — lightweight UI widgets with hover and focus states. Widgets get both `handle(event)` and `draw(surf, font)` calls each frame.
- **`NetClient`** — TCP socket wrapper for talking to the server. Has a background `_recv_loop` thread that reads messages and puts them on a `queue.Queue`. The main thread drains the queue each frame via `_drain_network`.
- **`P2PChat`** — the peer-to-peer chat subsystem. Owns a listening socket (for incoming peer connections) and can also `connect_to(peer_ip, peer_port)` to initiate outbound. Uses a mutex + "first to connect wins" rule to guarantee exactly one live chat socket even though both sides race.
- **`App`** — the top-level application. Holds all state, runs the Pygame event loop at 60 FPS, dispatches events per-screen, and renders per-screen.

Drawing helpers — `draw_vertical_gradient`, `draw_card`, `draw_progress_bar` — produce the card-based look shared across screens.

---

## Protocol reference

Every message is **one line of UTF-8 JSON terminated by `\n`**. Every message has a `type` field plus optional payload fields.

### Client → Server

| Type | Payload | Purpose |
|------|---------|---------|
| `LOGIN` | `username` | Request a username |
| `LIST_PLAYERS` | *(none)* | Request current lobby snapshot |
| `CHALLENGE` | `target` | Challenge another player to a match |
| `CHALLENGE_REPLY` | `from`, `accept` | Accept/decline an incoming challenge |
| `SPECTATE` | *(none)* | Join the active match as a fan |
| `LEAVE_MATCH` | *(none)* | Forfeit or leave as spectator |
| `MOVE` | `direction` ∈ {UP,DOWN,LEFT,RIGHT} | Movement intent |
| `CHEER` | `emoji` | Send a fan cheer (broadcast via server) |
| `CHAT_INFO` | `port` | Register our P2P chat listener port |

### Server → Client

| Type | Payload | Purpose |
|------|---------|---------|
| `LOGIN_OK` | `username` | Username accepted |
| `LOGIN_FAIL` | `reason` | Username rejected (taken/invalid) |
| `LOBBY` | `players`, `matches` | Lobby snapshot |
| `INCOMING_CHAL` | `from` | Someone challenged you |
| `CHAL_DECLINED` | `by` | Your challenge was declined |
| `MATCH_START` | `you`, `opponent`, `peer_ip`, `peer_chat_port`, `config` | Match begins |
| `STATE` | `tick`, `phase`, `countdown`, `snakes`, `pies`, `static_obstacles`, `moving_obstacles`, `walls_active`, `num_walls`, `win_score`, `wall_threshold`, `elapsed`, `cheers`, ... | Per-tick game state |
| `GAME_OVER` | `winner`, `scores`, `reason` | Match ended |
| `CHEER_FWD` | `from`, `emoji` | Cheer forwarded from another user |
| `ERROR` | `message` | Server-reported error |

### Peer-to-peer (client ↔ client)

| Type | Payload | Purpose |
|------|---------|---------|
| `P2P_HELLO` | `username` | Peer introduction (first message sent on the peer socket) |
| `P2P_CHAT` | `text` | A chat message |
| `P2P_CHAT_END` | *(none)* | Peer cleanly closing the chat |

---

## Threading model

**Server:**

- Main thread: `accept()` loop. One worker thread per accepted client runs `_client_loop`, reading messages and dispatching them.
- Per-match thread: created when a match starts, runs `Match.run()`. Drives the 10 Hz game loop, broadcasts `STATE`, and detects end conditions.
- All shared mutable state (the players dict, the current match, the per-match snake list) is guarded by mutexes.

**Client:**

- Main thread: Pygame event loop at 60 FPS. Drains incoming messages from `NetClient.queue` each frame and applies state transitions.
- Network receive thread: `NetClient._recv_loop` reads messages from the server socket and pushes them onto the queue.
- P2P chat listener thread: `P2PChat._accept_loop` accepts incoming peer connections.
- P2P chat reader thread: one per live peer socket, reading messages and pushing them onto `P2PChat.queue`.

---

## Socket-handling details

### Server accept loop

`SO_REUSEADDR` is set so the server can be restarted quickly during development. Each accepted connection is handed to its own worker thread — the thread-per-client pattern is simple and handles disconnects gracefully (the thread exits when `recv` returns empty bytes).

### Framing

TCP is a byte stream, not a message stream. We frame by rule: **one JSON message per line, terminated by `\n`**. The helper `recv_line()` reads one byte at a time until it sees a newline. One-byte reads are not the fastest approach, but at our message rates (tens per second) they are entirely adequate and remove any need for a receive-buffer state machine.

### P2P chat establishment

The most interesting socket interaction in the project:

1. At login, each client opens a listening socket on an OS-assigned high port and tells the server the port via `CHAT_INFO`.
2. When a match starts, the server's `MATCH_START` message includes the opponent's public IP and chat port.
3. Each client both continues to `accept()` incoming and tries `connect()` outbound to the peer.
4. **First-to-connect-wins**: a mutex in `P2PChat` guarantees exactly one active peer socket — whichever connection establishes first is kept, and any later incoming or outgoing connection on the other side is closed immediately.

This means chat messages flow directly between the two players without routing through the server, while the server-mediated handshake avoids the usual NAT-traversal complexity (it only works when both clients are reachable from each other, e.g. on the same LAN).

### Teardown

Three teardown paths are handled:

- **Client disconnects from server** — worker thread detects empty `recv`, removes the Player from the players dict, forfeits their active match if any, closes the socket.
- **P2P chat close** — either client sends `P2P_CHAT_END`, both sides close the peer socket.
- **Post-match timeout** — if the 60-second post-match chat timer expires with neither side closing first, the client calls `end_chat()` automatically.

---

## Game-rule summary

- **Start score**: 100. **Win at**: 600.
- **Pies**:
  - Apple `+10` (common)
  - Cherry `+20` (most common, "standard" pie)
  - Golden `+40` (rare, high-value)
  - Rotten `−20` (trap)
- **Collision damage**: wall 30, obstacle 25, self 40, head-into-body 35, head-on 50.
- **Tick rate**: 10 Hz.
- **Pre-match countdown**: 10 seconds with a dim overlay on the arena. Chat is fully interactive during the countdown.
- **Match ends** when: a player hits 600 points (`score_reached`), opponent is eliminated to 0 (`opponent_eliminated`), or a safety timeout elapses.
- **Post-match chat**: stays open for 60 seconds on a dedicated screen with a live countdown. Either player can click *Close Chat* to end early.
- **Dynamic Arena (creative feature)**: once the leading player's score crosses 200, the server spawns additional walls at an accelerating cadence (every 6 s past 200, 4 s past 350, 2 s past 500). The arena border pulses red while walls are actively spawning.

---

## Testing across two machines

1. On the **server** laptop, find its LAN IP (e.g. `ifconfig` on Mac/Linux, `ipconfig` on Windows).
2. Run `python server.py 5000` on that laptop.
3. On both laptops (or both windows on one laptop), run `python client.py` and enter the server's LAN IP.
4. Both machines must be on the **same network**. On Windows, allow Python through the firewall the first time you run the server.

For spectator testing, run a third client on either laptop — it'll appear in the lobby and can click *Watch Match*.

---

## Extending the project

Common extension points:

- **New pie type** — add a tuple to `PIE_TYPES` in `server.py`; the client already renders any known pie type from `PIE_COLORS` in `client.py`. Add a new key there for the new type.
- **New collision type** — add the constant at the top of `server.py`, then add a check in `_resolve_collisions`. Remember to respect the priority order (wall, obstacle, self, head-on, head-into-body).
- **New message type** — add the constant to `protocol.py` first, then handle it in the server's `_dispatch` method and/or the client's `_handle_server_msg`.
- **New screen** — add a `_build_<n>_screen()` method (creates widgets), a `_draw_<n>()` method (renders), optionally an `_events_<n>()` method, and register the screen in the `_goto` builders dict and the `_render` dispatch.

---

## License and credits

Academic project, EECE 350, AUB Spring 2026.

Built with Python 3, Pygame 2, and standard-library `socket` / `threading`.
