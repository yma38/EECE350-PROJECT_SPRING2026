import json


#Client -> Server
LOGIN           = "LOGIN"           
LIST_PLAYERS    = "LIST_PLAYERS"     
CHALLENGE       = "CHALLENGE"        
CHALLENGE_REPLY = "CHALLENGE_REPLY"  
SPECTATE        = "SPECTATE"        
LEAVE_MATCH     = "LEAVE_MATCH"      
MOVE            = "MOVE"             
CHEER           = "CHEER"            
CHAT_INFO       = "CHAT_INFO"        

#Server -> Client
LOGIN_OK        = "LOGIN_OK"         # {username}
LOGIN_FAIL      = "LOGIN_FAIL"       # {reason}
LOBBY           = "LOBBY"            # {players: , matches:}
INCOMING_CHAL   = "INCOMING_CHAL"    # {from}
CHAL_DECLINED   = "CHAL_DECLINED"    # {by}
MATCH_START     = "MATCH_START"      # {role, you, opponent, peer_ip, peer_chat_port, config}
STATE           = "STATE"            # {tick, snakes, pies, obstacles, arena, time_left, cheers}
GAME_OVER       = "GAME_OVER"        # {winner, scores, reason}
ERROR           = "ERROR"            # {message}
CHEER_FWD       = "CHEER_FWD"        # {from, emoji}

#Peer-to-peer (client <-> client)
P2P_HELLO       = "P2P_HELLO"        
P2P_CHAT        = "P2P_CHAT"       
P2P_CHAT_END    = "P2P_CHAT_END"     



# Framing helpers
def encode(msg_type: str, **fields) -> bytes:
    """Serialize a message to bytes ready to send on a TCP socket."""
    payload = {"type": msg_type, **fields}
    return (json.dumps(payload) + "\n").encode("utf-8")


def decode(line: bytes) -> dict:
    """Parse a single newline-terminated JSON line into a dict."""
    return json.loads(line.decode("utf-8").strip())


def recv_line(sock) -> bytes:
    """
    Read one '\n'-terminated line from a blocking socket.
    Returns b"" if the peer closed the connection.
    """
    buf = bytearray()
    while True:
        chunk = sock.recv(1)
        if not chunk:
            return b""
        if chunk == b"\n":
            return bytes(buf)
        buf.extend(chunk)