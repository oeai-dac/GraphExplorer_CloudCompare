"""Ein WebSocket-Server aus der Standardbibliothek — ohne Qt, ohne Fremdpakete.

Warum von Hand: Das QGIS-Plugin nimmt ``QtWebSockets``. Blender bringt kein Qt
mit, und seine Python-Umgebung hat keine WebSocket-Bibliothek. Für **einen**
Client ist RFC 6455 aber überschaubar -- ein Handschlag und ein Rahmenformat --,
und eine selbstgebaute Datei ohne Abhängigkeiten ist in einem Addon leichter zu
verteilen als ein Wheel.

Das Fadenmodell ist der eigentliche Punkt. Blenders ``bpy`` ist **nicht
fadensicher**: Wer aus einem Hintergrundfaden heraus ``obj.select_set()``
aufruft, bekommt Abstürze, die sich nicht reproduzieren lassen. Deshalb fasst
dieser Server nie etwas vom Wirt an. Er legt eingehende Ereignisse in eine
Warteschlange; der Wirt holt sie sich mit ``poll()`` ab, wann es ihm passt --
in Blender aus ``bpy.app.timers``, also auf dem Hauptfaden.

    Socket-Faden          Warteschlange          Hauptfaden
    ────────────          ─────────────          ──────────
    liest Rahmen    ──▶   ("message", …)   ──▶   poll() ──▶ bpy

Gedeckt ist genau so viel vom Standard, wie ein Browser für diesen Zweck
braucht: Text-, Fortsetzungs-, Ping- und Schließen-Rahmen. Kein TLS (der Server
lauscht auf 127.0.0.1), keine Erweiterungen, keine Binärrahmen, ein Client zur
Zeit.
"""

import base64
import hashlib
import queue
import re
import socket
import struct
import threading

# Die Zeichenfolge aus RFC 6455 §1.3. Sie wird an den Schlüssel des Clients
# gehängt, das Ganze SHA-1-summiert und zurückgeschickt -- so weist der Server
# nach, dass er das Protokoll wirklich spricht und nicht nur einen HTTP-Port
# offen hat.
#
# Ein einziges falsches Zeichen hier, und jeder Browser weist die Verbindung
# wortlos ab. Deshalb prüft tests/run-websocket-checks.py sie gegen den
# Testvektor aus demselben Abschnitt des Standards -- verlassen sollte man sich
# darauf nicht aus dem Gedächtnis.
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: Obergrenze für eine einzelne eingehende Nachricht. Der Explorer schickt die
#: Schlüsselliste eines großen Graphen am Stück zurück, das können einige
#: Megabyte sein -- aber unbegrenzt darf es nicht sein, sonst genügt ein
#: erfundener Längenwert, um den Speicher vollzuschreiben.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# Rahmen
# ---------------------------------------------------------------------------

def accept_key(client_key):
    """Die Antwort auf ``Sec-WebSocket-Key``."""
    digest = hashlib.sha1((client_key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(payload, opcode=OP_TEXT):
    """Einen Rahmen vom Server zum Client bauen.

    Server-Rahmen werden **nicht** maskiert -- das schreibt der Standard so vor,
    und ein Browser bricht die Verbindung ab, wenn doch.
    """
    length = len(payload)
    header = bytearray()
    header.append(0x80 | opcode)          # FIN gesetzt, keine Fragmentierung
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    return bytes(header) + payload


def unmask(payload, mask):
    """Client-Rahmen sind maskiert; hier wird die Maske wieder herausgerechnet.

    Der naheliegende Weg -- Byte für Byte in einer Schleife -- kostet bei einer
    mehrere Megabyte großen Nachricht spürbar Zeit, und die läuft in Blender
    gegen den Timer. Der Umweg über eine große Ganzzahl schiebt dasselbe XOR in
    den C-Teil von Python und ist um Größenordnungen schneller.
    """
    n = len(payload)
    if not n:
        return b""
    full = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(full, "big")).to_bytes(n, "big")


def _recv_exact(sock, n):
    """Genau ``n`` Bytes lesen, oder ``None``, wenn die Gegenseite zumacht."""
    chunks = []
    got = 0
    while got < n:
        chunk = sock.recv(min(n - got, 65536))
        if not chunk:
            return None
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_frame(sock):
    """Einen Rahmen lesen. Gibt ``(fin, opcode, payload)`` oder ``None`` zurück."""
    header = _recv_exact(sock, 2)
    if header is None:
        return None

    fin = bool(header[0] & 0x80)
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F

    if length == 126:
        ext = _recv_exact(sock, 2)
        if ext is None:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = _recv_exact(sock, 8)
        if ext is None:
            return None
        length = struct.unpack("!Q", ext)[0]

    if length > MAX_MESSAGE_BYTES:
        raise ValueError("Frame too large: %d bytes" % length)

    mask = None
    if masked:
        mask = _recv_exact(sock, 4)
        if mask is None:
            return None

    payload = b""
    if length:
        payload = _recv_exact(sock, length)
        if payload is None:
            return None
        if mask:
            payload = unmask(payload, mask)

    return fin, opcode, payload


# ---------------------------------------------------------------------------
# Handschlag
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(rb"^Sec-WebSocket-Key:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


def read_handshake(sock, limit=8192):
    """Die HTTP-Anfrage lesen und den Schlüssel des Clients herausziehen.

    ``limit`` verhindert, dass eine Verbindung, die niemals eine Leerzeile
    schickt, den Faden endlos beschäftigt.
    """
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > limit:
            return None
        chunk = sock.recv(1024)
        if not chunk:
            return None
        data += chunk

    match = _KEY_RE.search(data)
    return match.group(1).decode("ascii") if match else None


def handshake_response(key):
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Accept: %s\r\n"
        "\r\n" % accept_key(key)
    ).encode("ascii")


# ---------------------------------------------------------------------------
# Der Server
# ---------------------------------------------------------------------------

class WebSocketServer(object):
    """Nimmt genau eine Verbindung an und stellt Ereignisse in eine Warteschlange.

    Der Wirt sieht nur ``start``, ``poll``, ``send``, ``stop`` -- und ruft
    ``poll`` von dort auf, wo er seine eigenen Daten anfassen darf.
    """

    def __init__(self):
        self._listener = None
        self._client = None
        self._client_lock = threading.Lock()
        self._events = queue.Queue()
        self._accept_thread = None
        self._read_thread = None
        self._running = False

    # -- Aufbau ---------------------------------------------------------------

    def start(self, host="127.0.0.1", port=0):
        """Lauschen. Gibt den tatsächlichen Port zurück (0 = freien wählen lassen)."""
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Kein SO_REUSEADDR: Wir nehmen ohnehin einen freien Port, und unter
        # Windows erlaubt die Option, einen *belegten* Port zu übernehmen --
        # das würde stille Verwechslungen zwischen zwei Sitzungen ermöglichen.
        self._listener.bind((host, port))
        self._listener.listen(1)
        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return int(self._listener.getsockname()[1])

    def stop(self):
        self._running = False
        self.drop_client()
        if self._listener is not None:
            _quiet_close(self._listener)
            self._listener = None

    def drop_client(self):
        """Nur die aktuelle Verbindung schließen -- der Server lauscht weiter.

        Gebraucht, wenn sich jemand ohne gültige Marke meldet: Abgewiesen wird
        dieser eine Anrufer, nicht der Anschluss. Sonst genügte ein beliebiger
        Tab mit einer alten Adresse, um die frisch kopierte unbrauchbar zu
        machen.
        """
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.sendall(encode_frame(b"", OP_CLOSE))
            except OSError:
                pass
            _quiet_close(client)

    # -- Für den Wirt ---------------------------------------------------------

    def poll(self):
        """Alle seit dem letzten Aufruf eingetroffenen Ereignisse.

        Liste von ``("open", None)``, ``("message", text)``, ``("close", None)``.
        Blockiert nie -- gedacht für einen Timer, der ohnehin regelmäßig läuft.
        """
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    def send(self, text):
        """Text senden. Aus jedem Faden aufrufbar."""
        with self._client_lock:
            client = self._client
            if client is None:
                return False
            try:
                client.sendall(encode_frame(text.encode("utf-8"), OP_TEXT))
                return True
            except OSError:
                return False

    def is_connected(self):
        with self._client_lock:
            return self._client is not None

    # -- Innenleben -----------------------------------------------------------

    def _accept_loop(self):
        while self._running:
            try:
                sock, _addr = self._listener.accept()
            except OSError:
                return  # Listener geschlossen -- regulärer Weg aus der Schleife

            if self.is_connected():
                # Nur eine Sitzung gleichzeitig. Ein zweiter Tab würde sonst
                # stillschweigend die Auswahl des ersten überschreiben.
                _quiet_close(sock)
                continue

            try:
                key = read_handshake(sock)
                if not key:
                    _quiet_close(sock)
                    continue
                sock.sendall(handshake_response(key))
            except OSError:
                _quiet_close(sock)
                continue

            with self._client_lock:
                self._client = sock
            self._events.put(("open", None))
            self._read_thread = threading.Thread(target=self._read_loop, args=(sock,), daemon=True)
            self._read_thread.start()

    def _read_loop(self, sock):
        parts = []
        parts_opcode = None
        try:
            while self._running:
                frame = read_frame(sock)
                if frame is None:
                    break
                fin, opcode, payload = frame

                if opcode == OP_CLOSE:
                    break
                if opcode == OP_PING:
                    with self._client_lock:
                        if self._client is sock:
                            sock.sendall(encode_frame(payload, OP_PONG))
                    continue
                if opcode == OP_PONG:
                    continue

                # Ein Browser darf eine große Nachricht in Stücke zerlegen: das
                # erste Stück trägt den Opcode, die folgenden eine 0, und erst
                # FIN schließt sie ab.
                if opcode == OP_CONTINUATION:
                    if parts_opcode is None:
                        break  # Fortsetzung ohne Anfang -- Protokollbruch
                    parts.append(payload)
                else:
                    parts = [payload]
                    parts_opcode = opcode

                if not fin:
                    continue

                data = b"".join(parts)
                parts = []
                finished, parts_opcode = parts_opcode, None
                if finished == OP_TEXT:
                    self._events.put(("message", data.decode("utf-8", "replace")))
                # Binärrahmen werden nicht gebraucht und still verworfen.
        except (OSError, ValueError):
            pass
        finally:
            with self._client_lock:
                if self._client is sock:
                    self._client = None
            _quiet_close(sock)
            self._events.put(("close", None))


def _quiet_close(sock):
    try:
        sock.close()
    except OSError:
        pass
