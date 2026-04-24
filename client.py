import pygame
import socket
import threading
import queue
import sys
import time
import json

from protocol import (
    encode, decode, recv_line,
    LOGIN, LOGIN_OK, LOGIN_FAIL, LIST_PLAYERS, LOBBY,
    CHALLENGE, INCOMING_CHAL, CHALLENGE_REPLY, CHAL_DECLINED,
    SPECTATE, LEAVE_MATCH, MOVE, CHAT_INFO,
    MATCH_START, STATE, GAME_OVER, ERROR,
    CHEER, CHEER_FWD,
    P2P_HELLO, P2P_CHAT, P2P_CHAT_END,
)

# Visual configuration
CELL         = 25
HUD_H        = 72
CHAT_W       = 280
WIN_W        = 30 * CELL + CHAT_W
WIN_H        = 20 * CELL + HUD_H
FPS          = 60

POST_CHAT_SECONDS = 60   # chat remains open this long after a match ends

# Palette — softer, richer, arcade-chic
BG           = (14, 16, 24)          # near-black base
BG_SOFT      = (20, 23, 34)          # slightly lifted base for gradients
PANEL        = (26, 30, 44)          # card surface
PANEL_2      = (36, 41, 58)          # input surface / elevated card
PANEL_3      = (50, 56, 78)          # hover
GRID_LINE    = (28, 32, 44)
GRID_DOT     = (42, 48, 66)
BORDER       = (62, 70, 92)
BORDER_SOFT  = (44, 50, 70)
TEXT         = (232, 234, 244)
TEXT_DIM     = (150, 156, 178)
TEXT_FAINT   = (100, 108, 130)
ACCENT       = (120, 180, 255)       # soft electric blue
ACCENT_DEEP  = (70, 130, 230)
WARN         = (255, 130, 130)
WARN_DEEP    = (210, 80, 80)
GOOD         = (140, 230, 170)
GOLD         = (255, 210, 110)

SNAKE_COLORS = [
    [(120, 210, 255), (60, 150, 230)],   # blue
    [(255, 150, 190), (220, 90, 145)],   # pink
    [(170, 240, 150), (100, 190, 95)],   # green
    [(255, 200, 110), (220, 150, 60)],   # orange
    [(210, 150, 255), (150, 95, 225)],   # purple
]

PIE_COLORS = {
    "apple":  ((240, 95, 95),   (180, 60, 60)),
    "cherry": ((255, 135, 165), (220, 90, 130)),
    "golden": ((255, 220, 90),  (220, 170, 50)),
    "rotten": ((130, 190, 130), (80, 140, 80)),
}


# Drawing helpers
def draw_vertical_gradient(surf, top_color, bottom_color):
    """Paint a vertical top->bottom gradient across the whole surface."""
    h = surf.get_height()
    for y in range(h):
        t = y / max(1, h - 1)
        c = (
            int(top_color[0] + (bottom_color[0] - top_color[0]) * t),
            int(top_color[1] + (bottom_color[1] - top_color[1]) * t),
            int(top_color[2] + (bottom_color[2] - top_color[2]) * t),
        )
        pygame.draw.line(surf, c, (0, y), (surf.get_width(), y))


def draw_card(surf, rect, fill=PANEL, border=BORDER_SOFT, shadow=True, radius=10, border_w=1):
    """Rounded card with a soft drop shadow."""
    r = pygame.Rect(rect)
    if shadow:
        shad = r.move(0, 4).inflate(2, 2)
        pygame.draw.rect(surf, (0, 0, 0), shad, border_radius=radius + 2)
    pygame.draw.rect(surf, fill, r, border_radius=radius)
    if border_w:
        pygame.draw.rect(surf, border, r, border_w, border_radius=radius)


def draw_progress_bar(surf, rect, frac, color, bg=PANEL_2, threshold_frac=None):
    """
    Rounded progress bar. Optionally draws a marker line at
    `threshold_frac` of the width (used for the 200-pt wall threshold).
    """
    r = pygame.Rect(rect)
    pygame.draw.rect(surf, bg, r, border_radius=6)
    inner = r.copy()
    inner.width = max(0, int(r.width * max(0.0, min(1.0, frac))))
    if inner.width > 0:
        pygame.draw.rect(surf, color, inner, border_radius=6)
    pygame.draw.rect(surf, BORDER_SOFT, r, 1, border_radius=6)
    if threshold_frac is not None and 0 < threshold_frac < 1:
        mx = r.x + int(r.width * threshold_frac)
        pygame.draw.line(surf, WARN, (mx, r.y - 2), (mx, r.bottom + 2), 2)


# Utility UI widgets
class TextInput:
    def __init__(self, rect, placeholder="", initial="", max_len=20):
        self.rect = pygame.Rect(rect)
        self.text = initial
        self.placeholder = placeholder
        self.max_len = max_len
        self.active = False

    def handle(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN:
            self.active = self.rect.collidepoint(event.pos)
        elif event.type == pygame.KEYDOWN and self.active:
            if event.key == pygame.K_BACKSPACE:
                self.text = self.text[:-1]
            elif event.key == pygame.K_RETURN:
                return "submit"
            elif len(self.text) < self.max_len and event.unicode.isprintable():
                self.text += event.unicode
        return None

    def draw(self, surf, font):
        color = ACCENT if self.active else BORDER_SOFT
        draw_card(surf, self.rect, fill=PANEL_2, border=color, shadow=False, border_w=2)
        shown = self.text if self.text else self.placeholder
        tc = TEXT if self.text else TEXT_FAINT
        txt = font.render(shown, True, tc)
        surf.blit(txt, (self.rect.x + 12, self.rect.y + (self.rect.h - txt.get_height()) // 2))


class Button:
    def __init__(self, rect, label, on_click=None, enabled=True, kind="default"):
        # kind: "default", "primary", "danger"
        self.rect = pygame.Rect(rect)
        self.label = label
        self.on_click = on_click
        self.enabled = enabled
        self.hover = False
        self.kind = kind

    def handle(self, event):
        if event.type == pygame.MOUSEMOTION:
            self.hover = self.rect.collidepoint(event.pos)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if self.enabled and self.rect.collidepoint(event.pos) and self.on_click:
                self.on_click()
                return True
        return False

    def draw(self, surf, font):
        # Pick palette by kind + state
        if not self.enabled:
            bg, fg, border = PANEL_2, TEXT_FAINT, BORDER_SOFT
        elif self.kind == "primary":
            bg  = ACCENT if self.hover else ACCENT_DEEP
            fg  = (14, 16, 24)
            border = ACCENT
        elif self.kind == "danger":
            bg  = WARN if self.hover else WARN_DEEP
            fg  = (14, 16, 24)
            border = WARN
        else:
            bg = PANEL_3 if self.hover else PANEL_2
            fg = TEXT
            border = ACCENT if self.hover else BORDER
        draw_card(surf, self.rect, fill=bg, border=border, shadow=self.hover, radius=8, border_w=2)
        txt = font.render(self.label, True, fg)
        surf.blit(txt, txt.get_rect(center=self.rect.center))


# Network thread, connects to server and reads messages into a queue
class NetClient:
    def __init__(self):
        self.sock = None
        self.recv_thread = None
        self.queue = queue.Queue()
        self.connected = False
        self.send_lock = threading.Lock()

    def connect(self, host, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((host, port))
        s.settimeout(None)
        self.sock = s
        self.connected = True
        self.recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.recv_thread.start()

    def _recv_loop(self):
        try:
            while self.connected:
                line = recv_line(self.sock)
                if not line:
                    self.queue.put({"type": "_DISCONNECT"})
                    break
                try:
                    self.queue.put(decode(line))
                except Exception:
                    pass
        except OSError:
            self.queue.put({"type": "_DISCONNECT"})
        finally:
            self.connected = False

    def send(self, msg_type, **fields):
        if not self.connected:
            return
        try:
            with self.send_lock:
                self.sock.sendall(encode(msg_type, **fields))
        except OSError:
            self.connected = False
            self.queue.put({"type": "_DISCONNECT"})

    def close(self):
        self.connected = False
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass


# Peer-to-peer chat
class P2PChat:
    """
    Listens on a local TCP port for incoming peer connections AND connects
    out to the opponent when a match starts. Whichever side connects first
    wins; the other side's inbound connection is closed. Simple, avoids
    duplicate message delivery.
    """

    def __init__(self, username):
        self.username = username
        self.listen_sock = None
        self.listen_port = None
        self.peer_sock = None
        self.peer_user = None
        self.lock = threading.Lock()
        self.queue = queue.Queue()  # incoming (username, text) tuples
        self.ended_by_peer = False  # set when we receive P2P_CHAT_END

    def start_listener(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", 0))
        s.listen()
        self.listen_sock = s
        self.listen_port = s.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return self.listen_port

    def _accept_loop(self):
        while self.listen_sock:
            try:
                csock, _ = self.listen_sock.accept()
            except OSError:
                break
            with self.lock:
                if self.peer_sock is None:
                    self.peer_sock = csock
                    self.peer_sock.sendall(encode(P2P_HELLO, username=self.username))
                    threading.Thread(target=self._read_peer, args=(csock,), daemon=True).start()
                else:
                    try: csock.close()
                    except OSError: pass

    def connect_to(self, ip, port):
        if not ip or not port:
            return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect((ip, int(port)))
            s.settimeout(None)
        except OSError:
            return
        with self.lock:
            if self.peer_sock is None:
                self.peer_sock = s
                s.sendall(encode(P2P_HELLO, username=self.username))
                threading.Thread(target=self._read_peer, args=(s,), daemon=True).start()
            else:
                try: s.close()
                except OSError: pass

    def _read_peer(self, s):
        try:
            while True:
                line = recv_line(s)
                if not line:
                    break
                try:
                    msg = decode(line)
                except Exception:
                    continue
                if msg.get("type") == P2P_HELLO:
                    self.peer_user = msg.get("username", "peer")
                elif msg.get("type") == P2P_CHAT:
                    self.queue.put((self.peer_user or "peer", msg.get("text", "")))
                elif msg.get("type") == P2P_CHAT_END:
                    self.ended_by_peer = True
                    break
        except OSError:
            pass
        finally:
            with self.lock:
                if self.peer_sock is s:
                    self.peer_sock = None
            try: s.close()
            except OSError: pass

    def send(self, text):
        with self.lock:
            s = self.peer_sock
        if not s:
            return False
        try:
            s.sendall(encode(P2P_CHAT, text=text))
            return True
        except OSError:
            return False

    def end_chat(self):
        """Politely tell the peer we're closing the chat, then hang up."""
        with self.lock:
            s = self.peer_sock
        if s:
            try:
                s.sendall(encode(P2P_CHAT_END))
            except OSError:
                pass
        self.close_match_connection()

    def close_match_connection(self):
        with self.lock:
            if self.peer_sock:
                try: self.peer_sock.close()
                except OSError: pass
                self.peer_sock = None
                self.peer_user = None


# Main App — owns all state and drives the screen state machine
class App:
    def __init__(self):
        pygame.init()
        pygame.display.set_caption("Πthon Arena")
        self.screen = pygame.display.set_mode((WIN_W, WIN_H))
        self.clock = pygame.time.Clock()
        self.font_s = pygame.font.SysFont("consolas", 14)
        self.font_m = pygame.font.SysFont("consolas", 18)
        self.font_l = pygame.font.SysFont("consolas", 26, bold=True)
        self.font_xl = pygame.font.SysFont("consolas", 44, bold=True)

        # network + chat
        self.net = NetClient()
        self.chat = None            # P2PChat, created after login

        # Application-wide state
        self.screen_name = "connect"
        self.error_msg = ""
        self.info_msg  = ""

        # Lobby
        self.players = []
        self.matches = []
        self.incoming_challenge = None  # username or None

        # Match state (filled from STATE messages)
        self.my_username = None
        self.opponent = None
        self.is_spectator = False
        self.match_config = {}
        self.match_state  = None
        self.game_over_info = None

        # Customization
        self.color_idx = 0
        self.key_bindings = {
            pygame.K_UP: "UP", pygame.K_DOWN: "DOWN",
            pygame.K_LEFT: "LEFT", pygame.K_RIGHT: "RIGHT",
        }
        self.rebind_target = None  # "UP" / "DOWN" / ... while rebinding

        # Chat UI
        self.chat_log = []
        self.chat_input = ""
        self.chat_focus = False

        # Post-match chat lifecycle
        self.post_match_until = None     # time.time() deadline, or None
        self.chat_closed = False         # True once end_chat has been called
        self.chat_close_reason = None    # "self" | "peer" | "timeout" | None

        # Widgets (created per screen in _build_screen)
        self.widgets = []
        self._build_connect_screen()

    # SCREEN BUILDERS
    def _build_connect_screen(self):
        self.widgets = []
        cx = WIN_W // 2
        self.ip_input = TextInput((cx - 160, 275, 320, 38), "server IP (127.0.0.1)", "127.0.0.1", 30)
        self.port_input = TextInput((cx - 160, 335, 320, 38), "port (e.g. 5000)", "5000", 6)
        self.connect_btn = Button((cx - 90, 400, 180, 46), "Connect", self._do_connect, kind="primary")
        self.widgets = [self.ip_input, self.port_input, self.connect_btn]

    def _build_login_screen(self):
        self.widgets = []
        cx = WIN_W // 2
        self.username_input = TextInput((cx - 160, 305, 320, 38), "choose a username", "", 16)
        self.login_btn = Button((cx - 90, 370, 180, 46), "Join Arena", self._do_login, kind="primary")
        self.widgets = [self.username_input, self.login_btn]

    def _build_lobby_screen(self):
        self.widgets = []
        self.refresh_btn   = Button((WIN_W - 205, 130, 180, 36), "Refresh",
                                    lambda: self.net.send(LIST_PLAYERS))
        self.customize_btn = Button((WIN_W - 205, 175, 180, 36), "Customize Snake",
                                    self._goto_customize)
        self.spectate_btn  = Button((WIN_W - 205, 220, 180, 36), "Watch Match",
                                    self._do_spectate, enabled=bool(self.matches),
                                    kind="primary")
        self.widgets = [self.refresh_btn, self.customize_btn, self.spectate_btn]

    def _build_customize_screen(self):
        self.widgets = []
        cx = WIN_W // 2
        self.back_btn = Button((cx - 220, 520, 140, 42), "Back", self._goto_lobby)
        self.save_btn = Button((cx + 80, 520, 140, 42), "Save", self._goto_lobby, kind="primary")
        self.widgets = [self.back_btn, self.save_btn]

    def _build_post_match_screen(self):
        """
        Post-match chat screen: centered card with the result, a chat log,
        an input line, a live countdown, and two buttons (close chat / lobby).
        """
        self.widgets = []
        cx = WIN_W // 2
        # Layout: a card from y=80 to y=WIN_H-60, centered, ~580 wide
        btn_y = WIN_H - 110
        self.close_chat_btn = Button((cx - 200, btn_y, 180, 42),
                                     "Close Chat", self._close_chat, kind="danger")
        self.back_lobby_btn = Button((cx + 20, btn_y, 180, 42),
                                     "Return to Lobby", self._goto_lobby, kind="primary")
        self.widgets = [self.close_chat_btn, self.back_lobby_btn]

    # SCREEN TRANSITIONS

    def _goto(self, name):
        self.screen_name = name
        self.error_msg = ""
        self.info_msg = ""
        builders = {
            "connect":   self._build_connect_screen,
            "login":     self._build_login_screen,
            "lobby":     self._build_lobby_screen,
            "customize": self._build_customize_screen,
            "game":      lambda: setattr(self, "widgets", []),
            "postmatch": self._build_post_match_screen,
        }
        builders[name]()

    def _goto_lobby(self):
        self.is_spectator = False
        self.match_state = None
        self.opponent = None
        self.chat_log = []
        self.post_match_until = None
        self.chat_closed = False
        self.chat_close_reason = None
        if self.chat:
            self.chat.end_chat()
        self._goto("lobby")
        self.net.send(LIST_PLAYERS)

    def _goto_customize(self):
        self._goto("customize")

    def _close_chat(self):
        """User clicked 'Close Chat' on the post-match screen."""
        if self.chat:
            self.chat.end_chat()
        self.chat_closed = True
        self.chat_close_reason = "self"

    # ACTIONS
    def _do_connect(self):
        host = self.ip_input.text.strip() or "127.0.0.1"
        try:
            port = int(self.port_input.text.strip())
        except ValueError:
            self.error_msg = "Port must be a number"
            return
        try:
            self.net.connect(host, port)
        except (OSError, socket.timeout) as e:
            self.error_msg = f"Cannot connect: {e}"
            return
        self._goto("login")

    def _do_login(self):
        name = self.username_input.text.strip()
        if not name:
            self.error_msg = "Username cannot be empty"
            return
        self.net.send(LOGIN, username=name)
        self.info_msg = "Logging in..."

    def _do_spectate(self):
        if self.matches:
            self.net.send(SPECTATE)

    def _challenge(self, username):
        self.net.send(CHALLENGE, target=username)
        self.info_msg = f"Challenge sent to {username}"

    def _reply_challenge(self, accept):
        if self.incoming_challenge:
            self.net.send(CHALLENGE_REPLY, **{"from": self.incoming_challenge, "accept": accept})
            self.incoming_challenge = None

    def _leave_match(self):
        self.net.send(LEAVE_MATCH)
        self._goto_lobby()

    # NETWORK MESSAGE HANDLING
    def _drain_network(self):
        while not self.net.queue.empty():
            msg = self.net.queue.get()
            self._handle_server_msg(msg)
        if self.chat:
            while not self.chat.queue.empty():
                who, text = self.chat.queue.get()
                self.chat_log.append((who, text))
            # Peer closed the chat on their side
            if self.chat.ended_by_peer and not self.chat_closed:
                self.chat_closed = True
                self.chat_close_reason = "peer"
                self.chat.ended_by_peer = False

        # Auto-close the post-match chat when the 60s timer runs out
        if (self.post_match_until is not None
                and not self.chat_closed
                and time.time() >= self.post_match_until):
            if self.chat:
                self.chat.end_chat()
            self.chat_closed = True
            self.chat_close_reason = "timeout"

    def _handle_server_msg(self, msg):
        t = msg.get("type")

        if t == "_DISCONNECT":
            self.error_msg = "Disconnected from server"
            self._goto("connect")

        elif t == LOGIN_OK:
            self.my_username = msg["username"]
            # Start P2P chat listener and tell server our port
            self.chat = P2PChat(self.my_username)
            port = self.chat.start_listener()
            self.net.send(CHAT_INFO, port=port)
            self.net.send(LIST_PLAYERS)
            self._goto("lobby")

        elif t == LOGIN_FAIL:
            self.error_msg = msg.get("reason", "Login failed")

        elif t == LOBBY:
            self.players = msg.get("players", [])
            self.matches = msg.get("matches", [])
            if self.screen_name == "lobby":
                self.spectate_btn.enabled = bool(self.matches) and not self.is_spectator

        elif t == INCOMING_CHAL:
            self.incoming_challenge = msg.get("from")

        elif t == CHAL_DECLINED:
            self.info_msg = f"{msg.get('by')} declined your challenge"

        elif t == MATCH_START:
            self.opponent = msg.get("opponent")
            self.match_config = msg.get("config", {})
            self.is_spectator = False
            self.chat_log = []
            # connect to peer for chat
            if self.chat:
                self.chat.connect_to(msg.get("peer_ip"), msg.get("peer_chat_port"))
            self._goto("game")

        elif t == STATE:
            self.match_state = msg
            if self.screen_name != "game":
                # spectator joining mid-match
                self.is_spectator = True
                self._goto("game")

        elif t == GAME_OVER:
            self.game_over_info = msg
            # Spectators have no peer chat, so they bounce straight to lobby
            if self.is_spectator:
                self._goto_lobby()
            else:
                # Players get a 1-minute post-match chat window
                self.post_match_until = time.time() + POST_CHAT_SECONDS
                self.chat_closed = False
                self.chat_close_reason = None
                self._goto("postmatch")

        elif t == CHEER_FWD:
            self.chat_log.append(("fan", f"{msg.get('from')} {msg.get('emoji')}"))

        elif t == ERROR:
            self.error_msg = msg.get("message", "error")

    # EVENT HANDLING
    def _handle_event(self, event):
        # Global quit
        if event.type == pygame.QUIT:
            self._quit()
            return
        # Widgets first
        for w in self.widgets:
            if hasattr(w, "handle"):
                w.handle(event)
        # Per-screen extras
        screen = self.screen_name
        if screen == "connect":
            self._events_connect(event)
        elif screen == "login":
            self._events_login(event)
        elif screen == "lobby":
            self._events_lobby(event)
        elif screen == "customize":
            self._events_customize(event)
        elif screen == "game":
            self._events_game(event)
        elif screen == "postmatch":
            self._events_postmatch(event)

    def _events_connect(self, event):
        if event.type == pygame.KEYDOWN and event.key == pygame.K_RETURN:
            self._do_connect()

    def _events_login(self, event):
        if event.type == pygame.KEYDOWN and event.key == pygame.K_RETURN:
            self._do_login()

    def _events_lobby(self, event):
        # Clicks on player rows
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for i, p in enumerate(self._available_players()):
                rect = pygame.Rect(32, 160 + i * 52, WIN_W - 260, 44)
                if rect.collidepoint(event.pos):
                    # Only challenge if they're free
                    if p.get("status") == "lobby":
                        self._challenge(p["username"])
                    return

            # Incoming challenge modal buttons
            if self.incoming_challenge:
                cx = WIN_W // 2
                yes = pygame.Rect(cx - 110, WIN_H // 2 + 20, 100, 36)
                no  = pygame.Rect(cx + 10,  WIN_H // 2 + 20, 100, 36)
                if yes.collidepoint(event.pos):
                    self._reply_challenge(True)
                elif no.collidepoint(event.pos):
                    self._reply_challenge(False)

    def _events_customize(self, event):
        # Click on color swatches
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            cx = WIN_W // 2
            swatch_y = 130 + 50   # color_card.y + 50
            start_x = cx - 200
            for i in range(len(SNAKE_COLORS)):
                r = pygame.Rect(start_x + i * 80, swatch_y, 60, 60)
                if r.collidepoint(event.pos):
                    self.color_idx = i
                    return
            # Click on a keybind row to start rebinding
            keys_card_x = cx - 280
            keys_card_y = 280
            for i, action in enumerate(["UP", "DOWN", "LEFT", "RIGHT"]):
                r = pygame.Rect(keys_card_x + 20,
                                keys_card_y + 66 + i * 36,
                                520, 30)
                if r.collidepoint(event.pos):
                    self.rebind_target = action
                    return

        if event.type == pygame.KEYDOWN and self.rebind_target:
            # unbind any existing mapping to this key
            new_bindings = {
                k: v for k, v in self.key_bindings.items()
                if k != event.key and v != self.rebind_target
            }
            new_bindings[event.key] = self.rebind_target
            self.key_bindings = new_bindings
            self.rebind_target = None

    def _events_game(self, event):
        if event.type == pygame.KEYDOWN:
            # Chat focus toggle
            if event.key == pygame.K_t and not self.chat_focus:
                self.chat_focus = True
                return
            if self.chat_focus:
                if event.key == pygame.K_RETURN:
                    if self.chat_input.strip():
                        if self.is_spectator:
                            # spectators use server-relayed cheers
                            self.net.send(CHEER, emoji=self.chat_input.strip()[:8])
                        elif self.chat:
                            if self.chat.send(self.chat_input.strip()):
                                self.chat_log.append((self.my_username, self.chat_input.strip()))
                        self.chat_input = ""
                    self.chat_focus = False
                elif event.key == pygame.K_ESCAPE:
                    self.chat_input = ""
                    self.chat_focus = False
                elif event.key == pygame.K_BACKSPACE:
                    self.chat_input = self.chat_input[:-1]
                elif event.unicode.isprintable() and len(self.chat_input) < 80:
                    self.chat_input += event.unicode
                return

            # Movement (players only)
            if not self.is_spectator and event.key in self.key_bindings:
                self.net.send(MOVE, direction=self.key_bindings[event.key])

            # Leave / forfeit
            if event.key == pygame.K_ESCAPE:
                self._leave_match()

            # Spectator quick-cheer
            if self.is_spectator and event.key == pygame.K_SPACE:
                self.net.send(CHEER, emoji="👏")

    def _events_postmatch(self, event):
        """
        Post-match chat screen: the input box has focus-by-click and also
        'T' to focus, Enter to send, Esc to blur, buttons handled by widgets.
        """
        if self.chat_closed:
            return

        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            # Click on the input area focuses it
            card_w = min(WIN_W - 80, 700)
            card_x = (WIN_W - card_w) // 2
            chat_y = 70 + 170 + 20
            chat_h = WIN_H - chat_y - 130
            input_r = pygame.Rect(card_x + 16, chat_y + chat_h - 46,
                                  card_w - 32, 34)
            if input_r.collidepoint(event.pos):
                self.chat_focus = True
            else:
                self.chat_focus = False

        if event.type == pygame.KEYDOWN:
            if not self.chat_focus and event.key == pygame.K_t:
                self.chat_focus = True
                return
            if self.chat_focus:
                if event.key == pygame.K_RETURN:
                    txt = self.chat_input.strip()
                    if txt and self.chat:
                        if self.chat.send(txt):
                            self.chat_log.append((self.my_username, txt))
                    self.chat_input = ""
                elif event.key == pygame.K_ESCAPE:
                    self.chat_input = ""
                    self.chat_focus = False
                elif event.key == pygame.K_BACKSPACE:
                    self.chat_input = self.chat_input[:-1]
                elif event.unicode.isprintable() and len(self.chat_input) < 80:
                    self.chat_input += event.unicode

    # RENDERING
    def _render(self):
        self.screen.fill(BG)
        screen = self.screen_name
        if screen == "connect":   self._draw_connect()
        elif screen == "login":   self._draw_login()
        elif screen == "lobby":   self._draw_lobby()
        elif screen == "customize": self._draw_customize()
        elif screen == "game":    self._draw_game()
        elif screen == "postmatch": self._draw_postmatch()
        pygame.display.flip()

    #shared header

    def _draw_title(self, subtitle=None):
        title = self.font_xl.render("Πthon Arena", True, ACCENT)
        self.screen.blit(title, title.get_rect(center=(WIN_W // 2, 110)))
        if subtitle:
            s = self.font_m.render(subtitle, True, TEXT_DIM)
            self.screen.blit(s, s.get_rect(center=(WIN_W // 2, 160)))

    def _draw_status_footer(self):
        if self.error_msg:
            t = self.font_s.render(self.error_msg, True, WARN)
            self.screen.blit(t, t.get_rect(center=(WIN_W // 2, WIN_H - 30)))
        elif self.info_msg:
            t = self.font_s.render(self.info_msg, True, GOOD)
            self.screen.blit(t, t.get_rect(center=(WIN_W // 2, WIN_H - 30)))

    #screens

    def _draw_connect(self):
        # Gradient background
        bg = pygame.Surface((WIN_W, WIN_H))
        draw_vertical_gradient(bg, BG_SOFT, BG)
        self.screen.blit(bg, (0, 0))

        # Decorative snake-head emblem above the title
        cx = WIN_W // 2
        pygame.draw.circle(self.screen, ACCENT_DEEP, (cx, 90), 34)
        pygame.draw.circle(self.screen, ACCENT, (cx, 90), 30)
        pygame.draw.circle(self.screen, (20, 24, 34), (cx - 8, 82), 5)
        pygame.draw.circle(self.screen, (20, 24, 34), (cx + 8, 82), 5)
        pygame.draw.circle(self.screen, (255, 255, 255), (cx - 8, 82), 2)
        pygame.draw.circle(self.screen, (255, 255, 255), (cx + 8, 82), 2)

        title = self.font_xl.render("Πthon Arena", True, ACCENT)
        self.screen.blit(title, title.get_rect(center=(cx, 160)))
        sub = self.font_m.render(
            "online two-player snake battle", True, TEXT_DIM)
        self.screen.blit(sub, sub.get_rect(center=(cx, 198)))

        # Card wrapping the form
        card = pygame.Rect(cx - 200, 240, 400, 230)
        draw_card(self.screen, card, fill=PANEL, border=BORDER_SOFT,
                  shadow=True, radius=14, border_w=1)

        lbl1 = self.font_s.render("SERVER IP", True, TEXT_FAINT)
        self.screen.blit(lbl1, (cx - 160, 254))
        lbl2 = self.font_s.render("PORT", True, TEXT_FAINT)
        self.screen.blit(lbl2, (cx - 160, 314))
        for w in self.widgets:
            w.draw(self.screen, self.font_m)
        self._draw_status_footer()

    def _draw_login(self):
        bg = pygame.Surface((WIN_W, WIN_H))
        draw_vertical_gradient(bg, BG_SOFT, BG)
        self.screen.blit(bg, (0, 0))

        cx = WIN_W // 2
        title = self.font_xl.render("Choose a username", True, ACCENT)
        self.screen.blit(title, title.get_rect(center=(cx, 160)))
        sub = self.font_m.render(
            "you'll appear in the lobby under this name", True, TEXT_DIM)
        self.screen.blit(sub, sub.get_rect(center=(cx, 200)))

        card = pygame.Rect(cx - 200, 260, 400, 180)
        draw_card(self.screen, card, fill=PANEL, border=BORDER_SOFT,
                  shadow=True, radius=14, border_w=1)

        lbl = self.font_s.render("USERNAME", True, TEXT_FAINT)
        self.screen.blit(lbl, (cx - 160, 284))
        for w in self.widgets:
            w.draw(self.screen, self.font_m)
        self._draw_status_footer()

    def _draw_lobby(self):
        # Top banner with gradient
        banner = pygame.Surface((WIN_W, 80))
        draw_vertical_gradient(banner, (34, 40, 62), (20, 22, 34))
        self.screen.blit(banner, (0, 0))
        pygame.draw.line(self.screen, BORDER_SOFT, (0, 80), (WIN_W, 80))

        t = self.font_l.render("Πthon Arena — Lobby", True, ACCENT)
        self.screen.blit(t, (24, 26))
        who = self.font_m.render(
            f"signed in as {self.my_username}", True, TEXT_DIM)
        self.screen.blit(who, who.get_rect(topright=(WIN_W - 24, 32)))

        # Left: player list
        head = self.font_m.render("Online Players", True, TEXT)
        self.screen.blit(head, (32, 100))
        hint = self.font_s.render(
            "click a name to challenge them", True, TEXT_FAINT)
        self.screen.blit(hint, (32, 128))

        avail = self._available_players()
        if not avail:
            empty_card = pygame.Rect(32, 160, WIN_W - 260, 60)
            draw_card(self.screen, empty_card, fill=PANEL, border=BORDER_SOFT,
                      shadow=False, radius=8, border_w=1)
            t = self.font_s.render(
                "no one else is here yet — waiting for players...",
                True, TEXT_DIM)
            self.screen.blit(t, t.get_rect(center=empty_card.center))

        for i, p in enumerate(avail):
            r = pygame.Rect(32, 160 + i * 52, WIN_W - 260, 44)
            hovered = r.collidepoint(pygame.mouse.get_pos())
            draw_card(self.screen, r,
                      fill=PANEL_3 if hovered else PANEL,
                      border=ACCENT if hovered else BORDER_SOFT,
                      shadow=hovered, radius=8, border_w=2)
            # Snake icon on the left
            ic = pygame.Rect(r.x + 12, r.y + 10, 24, 24)
            # use the player's index to pick a consistent icon color
            idx = hash(p["username"]) % len(SNAKE_COLORS)
            pygame.draw.rect(self.screen, SNAKE_COLORS[idx][0], ic,
                             border_radius=6)
            name = self.font_m.render(p["username"], True, TEXT)
            self.screen.blit(name, (r.x + 48, r.y + 12))
            # Status badge
            status = p["status"]
            status_col = GOOD if status == "lobby" else TEXT_DIM
            badge_text = status if status != "lobby" else "available"
            badge = self.font_s.render(badge_text, True, status_col)
            self.screen.blit(badge, badge.get_rect(
                topright=(r.right - 16, r.y + 14)))

        # Right sidebar
        side = pygame.Rect(WIN_W - 215, 100, 200, WIN_H - 120)
        draw_card(self.screen, side, fill=PANEL, border=BORDER_SOFT,
                  shadow=True, radius=12, border_w=1)
        sh = self.font_m.render("Actions", True, TEXT)
        self.screen.blit(sh, (side.x + 14, side.y + 14))
        # Render buttons (they're positioned absolutely in the builder)
        for w in self.widgets:
            w.draw(self.screen, self.font_s)

        # Active match info inside sidebar
        if self.matches:
            m = self.matches[0]
            divider_y = 268
            pygame.draw.line(self.screen, BORDER_SOFT,
                             (side.x + 14, divider_y),
                             (side.right - 14, divider_y))
            mh = self.font_s.render("ACTIVE MATCH", True, TEXT_FAINT)
            self.screen.blit(mh, (side.x + 14, divider_y + 10))
            names = " vs ".join(m["players"])
            mn = self.font_s.render(names, True, TEXT)
            self.screen.blit(mn, (side.x + 14, divider_y + 32))
            sp = self.font_s.render(
                f"spectators: {m['spectators']}", True, TEXT_DIM)
            self.screen.blit(sp, (side.x + 14, divider_y + 52))

        # Challenge modal
        if self.incoming_challenge:
            self._draw_modal(
                f"{self.incoming_challenge} wants to play!",
                yes_label="Accept", no_label="Decline")
        self._draw_status_footer()

    def _draw_customize(self):
        bg = pygame.Surface((WIN_W, WIN_H))
        draw_vertical_gradient(bg, BG_SOFT, BG)
        self.screen.blit(bg, (0, 0))

        cx = WIN_W // 2
        title = self.font_xl.render("Customize your snake", True, ACCENT)
        self.screen.blit(title, title.get_rect(center=(cx, 80)))

        # Color picker card
        color_card = pygame.Rect(cx - 280, 130, 560, 130)
        draw_card(self.screen, color_card, fill=PANEL, border=BORDER_SOFT,
                  shadow=True, radius=12, border_w=1)
        ch = self.font_m.render("Color", True, TEXT)
        self.screen.blit(ch, (color_card.x + 20, color_card.y + 16))

        swatch_y = color_card.y + 50
        start_x = cx - 200
        for i, (c1, c2) in enumerate(SNAKE_COLORS):
            r = pygame.Rect(start_x + i * 80, swatch_y, 60, 60)
            pygame.draw.rect(self.screen, c2, r, border_radius=10)
            pygame.draw.rect(self.screen, c1, r.inflate(-8, -8),
                             border_radius=8)
            if i == self.color_idx:
                pygame.draw.rect(self.screen, ACCENT,
                                 r.inflate(10, 10), 3, border_radius=12)

        # Keys card
        keys_card = pygame.Rect(cx - 280, 280, 560, 220)
        draw_card(self.screen, keys_card, fill=PANEL, border=BORDER_SOFT,
                  shadow=True, radius=12, border_w=1)
        kh = self.font_m.render("Movement keys", True, TEXT)
        self.screen.blit(kh, (keys_card.x + 20, keys_card.y + 16))
        hint = self.font_s.render(
            "click a row, then press any key to rebind",
            True, TEXT_FAINT)
        self.screen.blit(hint, (keys_card.x + 20, keys_card.y + 40))

        inv_bindings = {v: k for k, v in self.key_bindings.items()}
        for i, action in enumerate(["UP", "DOWN", "LEFT", "RIGHT"]):
            r = pygame.Rect(keys_card.x + 20, keys_card.y + 66 + i * 36,
                            keys_card.w - 40, 30)
            is_active = self.rebind_target == action
            draw_card(self.screen, r,
                      fill=PANEL_2,
                      border=ACCENT if is_active else BORDER_SOFT,
                      shadow=False, radius=6, border_w=2)
            al = self.font_m.render(action, True, TEXT)
            self.screen.blit(al, (r.x + 14, r.y + 4))
            if is_active:
                kl = self.font_s.render("press any key...", True, ACCENT)
            else:
                key = inv_bindings.get(action)
                kl = self.font_s.render(
                    pygame.key.name(key) if key else "unset",
                    True, TEXT_DIM)
            self.screen.blit(kl, kl.get_rect(
                topright=(r.right - 14, r.y + 7)))

        for w in self.widgets:
            w.draw(self.screen, self.font_m)
        self._draw_status_footer()

    def _draw_game(self):
        # HUD
        self._draw_hud()
        # Arena
        if self.match_state:
            self._draw_arena(self.match_state)
            # Pre-match countdown overlay (dims board, shows big number)
            if self.match_state.get("phase") == "countdown":
                self._draw_countdown_overlay(self.match_state.get("countdown", 0))
        else:
            t = self.font_m.render("Waiting for first game tick...", True, TEXT_DIM)
            self.screen.blit(t, t.get_rect(center=(WIN_W // 2 - CHAT_W // 2, WIN_H // 2)))
        # Chat panel (always drawn, countdown doesn't block chat)
        self._draw_chat_panel()

    def _draw_countdown_overlay(self, seconds):
        """
        Dim the arena area and draw a large centered countdown number with
        a 'Get ready' tagline. Chat panel and HUD stay unobscured.
        """
        arena_w = 30 * CELL
        arena_h = 20 * CELL
        ox, oy = 0, HUD_H

        # Dim layer only over the arena (not HUD, not chat panel)
        overlay = pygame.Surface((arena_w, arena_h), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 170))
        self.screen.blit(overlay, (ox, oy))

        cx = ox + arena_w // 2
        cy = oy + arena_h // 2

        # "Get ready" tagline
        tag = self.font_m.render("Get ready", True, TEXT_DIM)
        self.screen.blit(tag, tag.get_rect(center=(cx, cy - 90)))

        # Big centered countdown number — pulses slightly on each new second
        # (pulse is driven by the current tick mod TICK_RATE so it's smooth)
        tick = self.match_state.get("tick", 0) if self.match_state else 0
        pulse_phase = (tick % 10) / 10.0          # 0.0 -> 1.0 each second
        scale = 1.0 + 0.18 * (1.0 - pulse_phase)  # 1.18 -> 1.0 falloff

        # Color shifts from warm (far) to urgent (near)
        if seconds <= 3:
            num_col = WARN
        elif seconds <= 5:
            num_col = GOLD
        else:
            num_col = ACCENT

        # Build the big number with a scaled font
        big_font = pygame.font.SysFont("consolas", int(140 * scale), bold=True)
        num_surf = big_font.render(str(max(1, seconds)), True, num_col)
        self.screen.blit(num_surf, num_surf.get_rect(center=(cx, cy)))

        # Subtext
        sub = self.font_m.render(
            f"match starts in {seconds} second{'s' if seconds != 1 else ''}",
            True, TEXT)
        self.screen.blit(sub, sub.get_rect(center=(cx, cy + 90)))

        # Chat hint during countdown
        hint = self.font_s.render(
            "chat with your opponent on the right →", True, TEXT_FAINT)
        self.screen.blit(hint, hint.get_rect(center=(cx, cy + 120)))

    def _draw_hud(self):
        # HUD background with a slight gradient bar
        hud = pygame.Surface((WIN_W, HUD_H))
        draw_vertical_gradient(hud, (28, 32, 48), (20, 22, 32))
        self.screen.blit(hud, (0, 0))
        pygame.draw.line(self.screen, BORDER_SOFT, (0, HUD_H), (WIN_W, HUD_H))

        state = self.match_state or {}
        snakes = state.get("snakes", [])
        win_score = state.get("win_score", 600)
        threshold = state.get("wall_threshold", 200)
        walls_active = state.get("walls_active", False)
        num_walls = state.get("num_walls", 0)

        # Two player cards
        for i, s in enumerate(snakes[:2]):
            x = 18 if i == 0 else WIN_W - CHAT_W - 280
            name = self.font_m.render(s["user"], True, TEXT)
            self.screen.blit(name, (x, 8))
            hp = max(0, s["hp"])
            hp_txt = self.font_s.render(f"{hp} / {win_score}", True, TEXT_DIM)
            self.screen.blit(hp_txt, hp_txt.get_rect(topright=(x + 260, 10)))
            c1, _ = SNAKE_COLORS[s["color"] % len(SNAKE_COLORS)]
            frac = hp / win_score
            draw_progress_bar(
                self.screen, (x, 34, 260, 18),
                frac, c1, bg=PANEL_2,
                threshold_frac=threshold / win_score,
            )

        # Center status: walls indicator + elapsed time (or pre-match badge)
        cx = (WIN_W - CHAT_W) // 2
        phase = state.get("phase", "playing")

        if phase == "countdown":
            pm = self.font_l.render("PRE-MATCH", True, GOLD)
            self.screen.blit(pm, pm.get_rect(center=(cx, 26)))
            wt = self.font_s.render("get ready...", True, TEXT_FAINT)
            self.screen.blit(wt, wt.get_rect(center=(cx, 54)))
        else:
            elapsed = state.get("elapsed", 0)
            mins = elapsed // 60
            secs = elapsed % 60
            time_txt = self.font_l.render(f"{mins}:{secs:02d}", True, ACCENT)
            self.screen.blit(time_txt, time_txt.get_rect(center=(cx, 26)))

            if walls_active:
                wt = self.font_s.render(f"⚠ WALLS SPAWNING  ({num_walls})",
                                        True, WARN)
                self.screen.blit(wt, wt.get_rect(center=(cx, 54)))
            else:
                wt = self.font_s.render(
                    f"walls arm at {threshold} pts", True, TEXT_FAINT)
                self.screen.blit(wt, wt.get_rect(center=(cx, 54)))

        if self.is_spectator:
            sp_card = pygame.Rect(WIN_W - CHAT_W - 120, HUD_H - 22, 100, 18)
            draw_card(self.screen, sp_card, fill=WARN_DEEP, border=WARN,
                      shadow=False, radius=6, border_w=1)
            sp = self.font_s.render("SPECTATING", True, (14, 16, 24))
            self.screen.blit(sp, sp.get_rect(center=sp_card.center))

    def _draw_arena(self, state):
        ox, oy = 0, HUD_H
        arena_w = 30 * CELL
        arena_h = 20 * CELL

        # background
        pygame.draw.rect(self.screen, (10, 12, 18), (ox, oy, arena_w, arena_h))

        # Dot grid instead of full lines — cleaner look
        for gx in range(1, 30):
            for gy in range(1, 20):
                self.screen.set_at((ox + gx * CELL, oy + gy * CELL), GRID_DOT)

        # Outer border — pulses red when walls are actively spawning
        walls_active = state.get("walls_active", False)
        if walls_active:
            # gentle pulse so it's visible but not distracting
            pulse = 0.6 + 0.4 * abs((state.get("tick", 0) % 20) / 20 - 0.5) * 2
            border_col = (
                int(180 + 60 * pulse),
                int(60 + 30 * pulse),
                int(60 + 30 * pulse),
            )
            pygame.draw.rect(self.screen, border_col, (ox, oy, arena_w, arena_h), 3)
        else:
            pygame.draw.rect(self.screen, BORDER_SOFT, (ox, oy, arena_w, arena_h), 2)

        # Static obstacles (blocky walls with a subtle hatched look)
        for (x, y) in state.get("static_obstacles", []):
            r = pygame.Rect(ox + x * CELL + 2, oy + y * CELL + 2, CELL - 4, CELL - 4)
            pygame.draw.rect(self.screen, (92, 100, 122), r, border_radius=4)
            pygame.draw.rect(self.screen, (140, 150, 175), r, 1, border_radius=4)
            # inner highlight
            pygame.draw.line(self.screen, (160, 170, 195),
                             (r.x + 3, r.y + 3), (r.right - 4, r.y + 3), 1)

        # Moving obstacles (warm red, distinct from static)
        for (x, y) in state.get("moving_obstacles", []):
            r = pygame.Rect(ox + x * CELL + 2, oy + y * CELL + 2, CELL - 4, CELL - 4)
            pygame.draw.rect(self.screen, (170, 80, 80), r, border_radius=5)
            pygame.draw.rect(self.screen, (220, 130, 130), r, 1, border_radius=5)

        # Pies — two-tone circle with a little "crust" mark
        for pie in state.get("pies", []):
            x, y = pie["x"], pie["y"]
            outer, inner = PIE_COLORS.get(pie["type"], ((255, 255, 255), (200, 200, 200)))
            cx_ = ox + x * CELL + CELL // 2
            cy_ = oy + y * CELL + CELL // 2
            pygame.draw.circle(self.screen, outer, (cx_, cy_), CELL // 2 - 3)
            pygame.draw.circle(self.screen, inner, (cx_, cy_), CELL // 2 - 6)
            # small cross on top to suggest pie crust
            pygame.draw.line(self.screen, outer,
                             (cx_ - 5, cy_), (cx_ + 5, cy_), 1)
            pygame.draw.line(self.screen, outer,
                             (cx_, cy_ - 5), (cx_, cy_ + 5), 1)

        # Snakes — rounded segments with gradient head
        for s in state.get("snakes", []):
            body = s["body"]
            c1, c2 = SNAKE_COLORS[s["color"] % len(SNAKE_COLORS)]
            if not s["alive"]:
                c1 = (90, 90, 100); c2 = (60, 60, 70)
            for i, (x, y) in enumerate(body):
                r = pygame.Rect(ox + x * CELL + 1, oy + y * CELL + 1, CELL - 2, CELL - 2)
                color = c1 if i == 0 else c2
                pygame.draw.rect(self.screen, color, r, border_radius=6)
                if i == 0:
                    # soft head highlight
                    hi = pygame.Rect(r.x + 3, r.y + 3, r.w - 6, 5)
                    pygame.draw.rect(self.screen, c1, hi.inflate(-2, 0), border_radius=3)
                    # eyes indicate direction
                    d = s["dir"]
                    cxh, cyh = r.center
                    if d == "UP":    e1 = (cxh-5, cyh-3); e2 = (cxh+5, cyh-3)
                    elif d == "DOWN":e1 = (cxh-5, cyh+3); e2 = (cxh+5, cyh+3)
                    elif d == "LEFT":e1 = (cxh-3, cyh-5); e2 = (cxh-3, cyh+5)
                    else:            e1 = (cxh+3, cyh-5); e2 = (cxh+3, cyh+5)
                    pygame.draw.circle(self.screen, (20, 20, 30), e1, 3)
                    pygame.draw.circle(self.screen, (20, 20, 30), e2, 3)
                    pygame.draw.circle(self.screen, (255, 255, 255), e1, 1)
                    pygame.draw.circle(self.screen, (255, 255, 255), e2, 1)

        # Cheer popups (fans)
        y_off = 14
        for cheer in state.get("cheers", [])[-3:]:
            t = self.font_m.render(f"{cheer['from']} {cheer['emoji']}", True, GOLD)
            self.screen.blit(t, (ox + 14, oy + y_off))
            y_off += 22

    def _draw_chat_panel(self):
        x = WIN_W - CHAT_W
        # background with subtle gradient
        panel = pygame.Surface((CHAT_W, WIN_H - HUD_H))
        draw_vertical_gradient(panel, (30, 34, 50), (22, 25, 36))
        self.screen.blit(panel, (x, HUD_H))
        pygame.draw.line(self.screen, BORDER_SOFT, (x, HUD_H), (x, WIN_H))

        # Header
        head_label = "Chat" if not self.is_spectator else "Fans"
        head = self.font_m.render(head_label, True, TEXT)
        self.screen.blit(head, (x + 16, HUD_H + 12))

        # Status subtitle (e.g. during post-match countdown)
        if self.post_match_until is not None and not self.chat_closed:
            remaining = max(0, int(self.post_match_until - time.time()))
            sub = f"chat closes in 0:{remaining:02d}"
            sub_col = WARN if remaining <= 10 else TEXT_DIM
        elif self.chat_closed:
            reason_txt = {
                "self":    "you closed the chat",
                "peer":    f"{self.opponent or 'peer'} closed the chat",
                "timeout": "chat window expired",
            }.get(self.chat_close_reason, "chat closed")
            sub = reason_txt
            sub_col = TEXT_FAINT
        elif self.is_spectator:
            sub = "SPACE to cheer"
            sub_col = TEXT_DIM
        else:
            sub = "press T to type"
            sub_col = TEXT_DIM
        st = self.font_s.render(sub, True, sub_col)
        self.screen.blit(st, (x + 16, HUD_H + 36))

        # Separator
        pygame.draw.line(self.screen, BORDER_SOFT,
                         (x + 16, HUD_H + 58), (x + CHAT_W - 16, HUD_H + 58))

        # Messages
        y = HUD_H + 72
        for who, msg in self.chat_log[-18:]:
            is_me = who == self.my_username
            who_col = ACCENT if is_me else GOLD if who == "fan" else GOOD
            who_txt = self.font_s.render(f"{who}:", True, who_col)
            self.screen.blit(who_txt, (x + 16, y))
            y += 16
            # Wrap message body at ~32 chars
            line_limit = 32
            for chunk in [msg[i:i+line_limit] for i in range(0, len(msg) or 1, line_limit)]:
                t = self.font_s.render(chunk, True, TEXT)
                self.screen.blit(t, (x + 24, y))
                y += 16
            y += 4
            if y > WIN_H - 60:
                break

        # Input bar (hidden once chat is closed)
        ir = pygame.Rect(x + 12, WIN_H - 44, CHAT_W - 24, 32)
        if self.chat_closed:
            draw_card(self.screen, ir, fill=PANEL_2, border=BORDER_SOFT,
                      shadow=False, radius=6, border_w=1)
            t = self.font_s.render("chat is closed", True, TEXT_FAINT)
            self.screen.blit(t, t.get_rect(center=ir.center))
        else:
            border = ACCENT if self.chat_focus else BORDER_SOFT
            draw_card(self.screen, ir, fill=PANEL_2, border=border,
                      shadow=False, radius=6, border_w=2)
            txt = (self.chat_input if self.chat_focus or self.chat_input
                   else "press T to chat...")
            color = TEXT if (self.chat_focus or self.chat_input) else TEXT_FAINT
            t = self.font_s.render(txt, True, color)
            self.screen.blit(t, (ir.x + 10, ir.y + (ir.h - t.get_height()) // 2))
            if self.chat_focus:
                # blinking caret
                if int(time.time() * 2) % 2 == 0:
                    cx = ir.x + 10 + t.get_width() + 2
                    pygame.draw.line(self.screen, TEXT,
                                     (cx, ir.y + 6), (cx, ir.bottom - 6), 1)

    def _draw_postmatch(self):
        # Background gradient
        bg = pygame.Surface((WIN_W, WIN_H))
        draw_vertical_gradient(bg, BG_SOFT, BG)
        self.screen.blit(bg, (0, 0))

        info = self.game_over_info or {}
        winner = info.get("winner")
        scores = info.get("scores", {})
        reason = info.get("reason", "")

        # Title bar
        header = self.font_l.render("Πthon Arena", True, ACCENT)
        self.screen.blit(header, (20, 22))

        # Result card (centered, wide)
        card_w = min(WIN_W - 80, 700)
        card_x = (WIN_W - card_w) // 2
        card = pygame.Rect(card_x, 70, card_w, 170)
        draw_card(self.screen, card, fill=PANEL, border=BORDER_SOFT,
                  shadow=True, radius=14, border_w=1)

        if winner is None:
            title_text = "Draw"
            title_col = TEXT_DIM
        elif winner == self.my_username:
            title_text = "Victory"
            title_col = GOOD
        else:
            title_text = "Defeat"
            title_col = WARN
        t = self.font_xl.render(title_text, True, title_col)
        self.screen.blit(t, t.get_rect(center=(card.centerx, card.y + 50)))

        # Scoreline
        if scores:
            items = list(scores.items())
            scoreline = "     ".join(f"{u}: {hp}" for u, hp in items)
            s = self.font_m.render(scoreline, True, TEXT)
            self.screen.blit(s, s.get_rect(center=(card.centerx, card.y + 100)))

        # Reason subtitle
        reason_txt = {
            "score_reached":     "score target reached",
            "opponent_eliminated": "opponent eliminated",
            "both_eliminated":   "both players eliminated",
            "timeout":           "match timed out",
        }.get(reason, reason)
        rs = self.font_s.render(reason_txt, True, TEXT_FAINT)
        self.screen.blit(rs, rs.get_rect(center=(card.centerx, card.y + 135)))

        # Chat card — big centered chat region below the result
        chat_y = card.bottom + 20
        chat_h = WIN_H - chat_y - 130
        chat_card = pygame.Rect(card_x, chat_y, card_w, chat_h)
        draw_card(self.screen, chat_card, fill=PANEL, border=BORDER_SOFT,
                  shadow=True, radius=12, border_w=1)

        # Chat header with countdown
        ch = self.font_m.render(
            f"Chat with {self.opponent or 'opponent'}", True, TEXT)
        self.screen.blit(ch, (chat_card.x + 18, chat_card.y + 14))

        if self.post_match_until is not None and not self.chat_closed:
            remaining = max(0, int(self.post_match_until - time.time()))
            count_col = WARN if remaining <= 10 else ACCENT
            count_txt = self.font_m.render(
                f"closes in 0:{remaining:02d}", True, count_col)
            self.screen.blit(count_txt, count_txt.get_rect(
                topright=(chat_card.right - 18, chat_card.y + 14)))
        elif self.chat_closed:
            reason_map = {
                "self":    "you closed the chat",
                "peer":    f"{self.opponent or 'peer'} closed the chat",
                "timeout": "chat expired",
            }
            closed_txt = self.font_m.render(
                reason_map.get(self.chat_close_reason, "chat closed"),
                True, WARN)
            self.screen.blit(closed_txt, closed_txt.get_rect(
                topright=(chat_card.right - 18, chat_card.y + 14)))

        # Separator
        pygame.draw.line(self.screen, BORDER_SOFT,
                         (chat_card.x + 16, chat_card.y + 48),
                         (chat_card.right - 16, chat_card.y + 48))

        # Messages area
        msg_top = chat_card.y + 60
        msg_bottom = chat_card.bottom - 60
        y = msg_top
        max_msgs = (msg_bottom - msg_top) // 22
        for who, msg in self.chat_log[-max_msgs:]:
            is_me = who == self.my_username
            who_col = ACCENT if is_me else GOOD
            prefix = self.font_s.render(f"{who}:", True, who_col)
            self.screen.blit(prefix, (chat_card.x + 20, y))
            body = self.font_s.render(msg, True, TEXT)
            self.screen.blit(body, (chat_card.x + 20 + prefix.get_width() + 6, y))
            y += 22

        # Chat input bar inside the card
        input_r = pygame.Rect(chat_card.x + 16, chat_card.bottom - 46,
                              chat_card.w - 32, 34)
        if self.chat_closed:
            draw_card(self.screen, input_r, fill=PANEL_2, border=BORDER_SOFT,
                      shadow=False, radius=6, border_w=1)
            t = self.font_s.render("chat is closed", True, TEXT_FAINT)
            self.screen.blit(t, t.get_rect(center=input_r.center))
        else:
            border = ACCENT if self.chat_focus else BORDER_SOFT
            draw_card(self.screen, input_r, fill=PANEL_2, border=border,
                      shadow=False, radius=6, border_w=2)
            txt = (self.chat_input if self.chat_focus or self.chat_input
                   else "click here or press T to type, Enter to send...")
            color = TEXT if (self.chat_focus or self.chat_input) else TEXT_FAINT
            t = self.font_s.render(txt, True, color)
            self.screen.blit(t, (input_r.x + 12,
                                 input_r.y + (input_r.h - t.get_height()) // 2))
            if self.chat_focus and int(time.time() * 2) % 2 == 0:
                cx_ = input_r.x + 12 + t.get_width() + 2
                pygame.draw.line(self.screen, TEXT,
                                 (cx_, input_r.y + 8),
                                 (cx_, input_r.bottom - 8), 1)

        # Buttons at the bottom
        # Disable "Close Chat" if already closed
        self.close_chat_btn.enabled = not self.chat_closed
        for w in self.widgets:
            w.draw(self.screen, self.font_m)

        self._draw_status_footer()

    def _draw_modal(self, text, yes_label="Yes", no_label="No"):
        overlay = pygame.Surface((WIN_W, WIN_H), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 160))
        self.screen.blit(overlay, (0, 0))
        cx = WIN_W // 2
        box = pygame.Rect(cx - 200, WIN_H // 2 - 80, 400, 160)
        pygame.draw.rect(self.screen, PANEL, box, border_radius=10)
        pygame.draw.rect(self.screen, ACCENT, box, 2, border_radius=10)
        t = self.font_m.render(text, True, TEXT)
        self.screen.blit(t, t.get_rect(center=(cx, WIN_H // 2 - 20)))
        yes = pygame.Rect(cx - 110, WIN_H // 2 + 20, 100, 36)
        no  = pygame.Rect(cx + 10,  WIN_H // 2 + 20, 100, 36)
        pygame.draw.rect(self.screen, GOOD, yes, border_radius=6)
        pygame.draw.rect(self.screen, WARN, no, border_radius=6)
        yt = self.font_s.render(yes_label, True, (18, 20, 28))
        nt = self.font_s.render(no_label, True, (18, 20, 28))
        self.screen.blit(yt, yt.get_rect(center=yes.center))
        self.screen.blit(nt, nt.get_rect(center=no.center))

    # HELPERS
    def _available_players(self):
        return [p for p in self.players if p["username"] != self.my_username]
    # MAIN LOOP
    def run(self):
        while True:
            for event in pygame.event.get():
                self._handle_event(event)
            self._drain_network()
            self._render()
            self.clock.tick(FPS)
    def _quit(self):
        self.net.close()
        if self.chat:
            self.chat.close_match_connection()
        pygame.quit()
        sys.exit(0)

def main():
    App().run()
if __name__ == "__main__":
    main()