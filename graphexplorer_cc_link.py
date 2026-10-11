"""GraphExplorer Link for CloudCompare -- Prototyp.

In CloudCompare 2.13 (mit Python-Plugin) im Python-Editor öffnen und
ausführen.

- **Erster Lauf:** startet einen WebSocket-Server und öffnet den Dialog mit
  der Verbindungsadresse und den Verknüpfungen (die Adresse steht außerdem in
  der Konsole und in der Zwischenablage). Das Skript kehrt danach zurück; die
  Kopplung läuft über Qt-Timer in der Ereignisschleife von CloudCompare weiter.
- **Jeder weitere Lauf:** öffnet den Dialog wieder. Die Verbindung bleibt
  dabei bestehen. Wer geänderten Code der Kopplung selbst laden will, muss
  CloudCompare neu starten.

Verknüpfungen stehen in ``links.json`` neben diesem Skript. Gibt es keine,
wird automatisch über das Knotenlabel verknüpft, und zwar mit *jedem*
Knotentyp, bei dem mindestens ein Name im DB-Tree passt.

Protokoll und Zuordnung (``linking.py``) sind dieselben wie im
Blender-Addon; ``linking.py`` ist eine unveränderte Kopie.
"""

import builtins
import importlib
import json
import os
import re
import secrets
import sys
import traceback

import pycc
from PyQt5.QtCore import QThread, QTimer
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtNetwork import QHostAddress
from PyQt5.QtWebSockets import QWebSocketServer
from PyQt5.QtWidgets import QApplication, QMainWindow

try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    # Der Python-Editor von CloudCompare 2.13 setzt __file__. Ohne geht es
    # nicht: Dann wären config_dialog.py und linking.py nicht zu finden.
    raise RuntimeError("Run graphexplorer_cc_link.py as a file from CloudCompare's "
                       "Python editor, not as pasted code.")
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import linking  # noqa: E402
import config_dialog  # noqa: E402

importlib.reload(linking)  # damit ein erneuter Lauf Änderungen sieht
importlib.reload(config_dialog)

HOST_NAME = "CloudCompare"
EXPECTED_API_VERSION = 2
TICK_MS = 250
TREE_EVERY = 6          # DB-Tree nur jeden n-ten Tick neu einlesen
DESCRIBE_EVERY = 8      # ohne Graph: so oft nachfragen, ob inzwischen einer da ist
LINKS_FILE = os.path.join(HERE, "links.json")

# CloudCompare benennt geladene Entitäten nach Datei oder Typ: "SU1001.obj",
# "SU1001 - Cloud", "SU1001 - Mesh". Das gehört nicht zur Bezeichnung -- wie
# Blenders ".001" wird es beim Lesen abgeschnitten.
_CC_SUFFIX = re.compile(r"\s*-\s*(cloud|mesh|vertices)$", re.IGNORECASE)
_FILE_EXT = re.compile(
    r"\.(obj|ply|stl|fbx|off|bin|las|laz|e57|xyz|txt|asc|pts|ptx|pcd|vtk|dxf|shp|csv)$",
    re.IGNORECASE)


def log(text):
    text = "[GraphExplorer] " + text
    try:
        print(text)
    except UnicodeEncodeError:
        # Eine Konsole mit Windows-Codepage kennt "⇄" nicht.
        print(text.encode("ascii", "replace").decode("ascii"))


def clean_name(name):
    name = _CC_SUFFIX.sub("", (name or "").strip())
    return _FILE_EXT.sub("", name).strip()


def guarded(fn):
    """Ausnahmen in Qt-Rückrufen abfangen.

    PyQt5 beendet bei einer unbehandelten Ausnahme in einem Slot den ganzen
    Prozess -- hier also CloudCompare. Ein Fehler im Prototyp darf aber nur in
    der Konsole stehen, nicht die offene Sitzung kosten.
    """
    def wrapper(*args):
        try:
            return fn(*args)
        except Exception:
            log("Error:\n" + traceback.format_exc())
    return wrapper


def spec_key(spec):
    return json.dumps(spec, sort_keys=True)


# ---------------------------------------------------------------------------
# Gespeicherte Verknüpfungen
# ---------------------------------------------------------------------------

def load_links():
    try:
        with open(LINKS_FILE, encoding="utf-8") as f:
            return [linking.LinkConfig.from_dict(d) for d in json.load(f)]
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        log("Could not read %s: %s" % (LINKS_FILE, e))
        return []


def save_links(links):
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump([link.to_dict() for link in links], f, ensure_ascii=False, indent=1)


def main_window():
    """Das Hauptfenster von CloudCompare, als PyQt-Objekt -- oder ``None``.

    Als Elternfenster hält es den Dialog über CloudCompare, ohne ihn über alle
    anderen Programme zu legen (wo er beim Einfügen der Adresse im Browser
    stören würde). ``pycc`` liefert das Fenster nicht als PyQt-Objekt; PyQt und
    CloudCompare teilen sich aber dieselbe Qt-Bibliothek, also findet es sich
    unter den Fenstern der Anwendung.
    """
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, QMainWindow) and widget.isVisible():
            return widget
    return None


# ---------------------------------------------------------------------------
# DB-Tree
# ---------------------------------------------------------------------------

def walk_tree(cc):
    """``[(uid, entity, name, parent_uid)]`` -- frisch gelesen, nie zwischengespeichert.

    Entitäten werden nicht über einen Tick hinaus gehalten: Löscht jemand eine
    in CloudCompare, wäre der Python-Verweis darauf ein toter Zeiger.
    """
    out = []

    def visit(obj, parent_uid):
        for i in range(obj.getChildrenNumber()):
            child = obj.getChild(i)
            uid = child.getUniqueID()
            out.append((uid, child, str(child.getName()), parent_uid))
            visit(child, uid)

    root = cc.dbRootObject()
    if root is not None:
        visit(root, None)
    return out


# ---------------------------------------------------------------------------
# Die Kopplung
# ---------------------------------------------------------------------------

class CCLink(object):

    def __init__(self):
        self.cc = pycc.GetInstance()
        self.token = secrets.token_urlsafe(16)
        self.server = QWebSocketServer("GraphExplorer Link", QWebSocketServer.NonSecureMode)
        if not self.server.listen(QHostAddress(QHostAddress.LocalHost), 0):
            raise RuntimeError("Could not open a port: " + self.server.errorString())
        self.server.newConnection.connect(guarded(self._on_new_connection))

        self.sock = None
        self.authed = False
        self.api_version = None
        self.pending = {}
        self.next_id = 1

        self.links = load_links()  # gespeicherte Verknüpfungen; leer = automatisch
        self.graph_info = {"loaded": False, "types": []}
        self.names = linking.DisplayNames()
        self.graph_loaded = False
        self.graph_side = {}       # spec_key -> [{"id", "key"}]
        self.runtimes = []         # [(LinkConfig, LinkIndex)] -- was gerade gilt
        self.sent_specs = None
        self.parents = {}          # uid -> uid des Elternteils
        self.tree_sig = None
        self.last_sel = None
        self.ticks = 0
        self.dialog = None         # der offene Dialog, falls einer offen ist
        self.match_summary = ""

        self.timer = QTimer()
        self.timer.timeout.connect(guarded(self._tick))
        self.timer.start(TICK_MS)

    @property
    def address(self):
        return "ws://127.0.0.1:%d/?token=%s" % (self.server.serverPort(), self.token)

    def stop(self):
        self.timer.stop()
        if self.sock is not None:
            self.sock.close()
        self.server.close()

    # -- Leitung -------------------------------------------------------------

    def _on_new_connection(self):
        sock = self.server.nextPendingConnection()
        if sock is None:
            return
        if self.sock is not None:
            # Eine Seite zur Zeit -- die neue ersetzt die alte, wie beim
            # Neuladen des Explorers.
            self.sock.close()
        self.sock = sock
        self.authed = False
        sock.textMessageReceived.connect(guarded(self._on_message))
        sock.disconnected.connect(guarded(lambda s=sock: self._on_disconnected(s)))

    def _on_disconnected(self, sock):
        if sock is not self.sock:
            return
        self.sock = None
        self.authed = False
        self.sent_specs = None
        pending, self.pending = self.pending, {}
        for callback in pending.values():
            guarded(callback)(None)
        log("Connection to the GraphExplorer closed. Paste the same address again to reconnect.")
        self._update_status()

    def _send(self, payload):
        if self.sock is None:
            return False
        self.sock.sendTextMessage(json.dumps(payload, ensure_ascii=False))
        return True

    def call(self, method, args=None, callback=None):
        message = {"method": method, "args": list(args or [])}
        if callback is not None:
            message["id"] = self.next_id
            self.pending[self.next_id] = callback
            self.next_id += 1
        if not (self.authed and self._send(message)) and callback is not None:
            self.pending.pop(message.get("id"), None)
            callback(None)

    def _on_message(self, text):
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return

        if not self.authed:
            if msg.get("event") == "hello" and secrets.compare_digest(
                    str(msg.get("token") or ""), self.token):
                self.authed = True
                self.api_version = int(msg.get("apiVersion") or 0)
                self._send({"event": "host", "name": HOST_NAME})
                log("Connected to the GraphExplorer (interface version %s)." % self.api_version)
                if self.api_version < EXPECTED_API_VERSION:
                    log("Warning: the GraphExplorer is older than this script expects.")
                self._update_status()
                self._describe()
            else:
                log("Connection without a valid token refused.")
                self.sock.close()
            return

        if "id" in msg:
            callback = self.pending.pop(msg["id"], None)
            if callback:
                # Nicht hier im Slot des Sockets ausführen, sondern gleich
                # danach: Ein Rückruf darf ein Meldungsfenster öffnen, und
                # dessen Ereignisschleife würde sonst weitere Nachrichten in
                # denselben, noch nicht zurückgekehrten Slot schieben -- daran
                # stürzt QWebSocket ab.
                result = msg.get("result")
                QTimer.singleShot(0, lambda: guarded(callback)(result))
            return

        event = msg.get("event")
        if event == "selection":
            self._apply_graph_selection([str(k) for k in (msg.get("keys") or [])])
        elif event == "graph":
            log("Another graph in the GraphExplorer -- matching again.")
            self.graph_loaded = False
            self.graph_side = {}
            self._update_status()
            self._describe()
        elif event == "log":
            log(str(msg.get("message") or ""))

    # -- Graph-Seite -----------------------------------------------------------

    def _describe(self):
        self.call("describeGraph", [], self._on_described)

    def _on_described(self, info):
        info = info or {}
        if not info.get("loaded"):
            return  # der Tick fragt wieder nach
        self.graph_info = info
        self.names = linking.DisplayNames(info)
        self.graph_loaded = True
        self.graph_side = {}
        log("Graph loaded: %d nodes, %d node types."
            % (info.get("nodeCount") or 0, len(info.get("types") or [])))
        if self.dialog is not None:
            self.dialog.set_graph_info(info)
        self._update_status()
        self.rebuild()

    def _configs(self):
        """Was gerade gilt: die gespeicherten Verknüpfungen -- oder die automatischen."""
        usable = [c for c in self.links if c.enabled and config_dialog.is_usable(c)]
        if usable:
            return usable, False
        auto = [linking.LinkConfig(node_type=t["type"], node_source=linking.SOURCE_LABEL)
                for t in self.graph_info.get("types") or []]
        return auto, True

    def rebuild(self):
        """Fehlende Graph-Seiten holen, dann abgleichen."""
        if not self.graph_loaded:
            return
        configs, _auto = self._configs()
        missing = []
        for config in configs:
            key = spec_key(config.js_spec())
            if key not in self.graph_side and key not in missing:
                missing.append(key)
        if not missing:
            self._relink(force_report=True)
            return

        pending = {"count": len(missing)}

        def make(key):
            def done(entries):
                self.graph_side[key] = entries or []
                pending["count"] -= 1
                if pending["count"] == 0:
                    self._relink(force_report=True)
            return done

        for key in missing:
            self.call("collectKeys", [json.loads(key)], make(key))

    # -- Abgleich --------------------------------------------------------------

    def object_pairs(self):
        tree = walk_tree(self.cc)
        self.parents = {uid: parent for uid, _e, _n, parent in tree}
        return [(uid, clean_name(name)) for uid, _e, name, _p in tree]

    def _relink(self, force_report=False):
        """Die Objektseite neu einlesen und mit allen Verknüpfungen abgleichen."""
        if not self.graph_loaded:
            return
        configs, auto = self._configs()
        pairs = self.object_pairs()

        self.runtimes = []
        lines = []
        for config in configs:
            entries = self.graph_side.get(spec_key(config.js_spec()))
            if entries is None:
                continue
            index = linking.LinkIndex(config)
            index.set_graph_side(entries)
            index.set_layer_side(pairs)
            report = index.report()
            # Automatisch: nur Typen, bei denen etwas passt. Selbst eingerichtet:
            # alle -- auch eine Verknüpfung ohne Treffer soll im Bericht stehen.
            if auto and not report.matched_keys:
                continue
            self.runtimes.append((config, index))
            lines.append("  %s: %s" % (config_dialog.describe_link(config, self.names),
                                       config_dialog.cc_wording(report.summary())))

        specs = [c.js_spec() for c, _i in self.runtimes]
        if specs != self.sent_specs:
            self.sent_specs = specs
            self.call("setSpecs", [specs])
            force_report = True
        self.last_sel = None

        if lines:
            self.match_summary = "%s\n%s" % (
                "Matched automatically by node label:" if auto else "Matched:", "\n".join(lines))
        else:
            self.match_summary = "No entity name in CloudCompare matches a node yet."
        self._update_status()

        if force_report:
            if lines:
                log("%s:\n%s" % ("Linked automatically by node label" if auto else "Links",
                                 "\n".join(lines)))
            else:
                log("No entity name in the DB tree matches a node yet "
                    "(%d entities). Load data or set up links." % len(pairs))

    # -- Dialog ----------------------------------------------------------------

    def _status_text(self):
        if not self.authed:
            return ("Waiting for the GraphExplorer. Copy the address, then in the "
                    "GraphExplorer click 'Connect application' (bottom left), paste it "
                    "and click Connect.")
        if not self.graph_loaded:
            return "Connected. Waiting for a graph to be loaded in the GraphExplorer."
        return "Connected · graph with %d nodes.\n%s" % (
            self.graph_info.get("nodeCount") or 0, self.match_summary)

    def _update_status(self):
        if self.dialog is not None:
            self.dialog.set_status(self._status_text())

    def open_dialog(self):
        if self.dialog is not None:
            self.dialog.raise_()
            self.dialog.activateWindow()
            return
        dialog = config_dialog.LinkConfigDialog(
            self.links, self.graph_info, self.address,
            check_fn=lambda spec, done: self.call("collectKeys", [spec], done),
            object_pairs_fn=self.object_pairs,
            parent=main_window(),
        )
        # Nicht modal: Solange der Dialog offen ist, soll man in CloudCompare
        # weiterarbeiten und die Kopplung ausprobieren können -- und das
        # Skript soll zurückkehren, statt im Editor als "läuft" zu hängen.
        dialog.finished.connect(guarded(self._on_dialog_finished))
        self.dialog = dialog
        self._update_status()
        dialog.show()

    def _on_dialog_finished(self, result):
        dialog, self.dialog = self.dialog, None
        if dialog is None or result != dialog.Accepted:
            return
        self.links = dialog.links()
        try:
            save_links(self.links)
        except OSError as e:
            log("Could not save the links: %s" % e)
        self.rebuild()

    # -- Explorer -> CloudCompare ---------------------------------------------

    def _apply_graph_selection(self, keys):
        if not self.runtimes:
            return
        wanted = set()
        for _config, index in self.runtimes:
            wanted.update(index.feature_ids_for_keys(keys))

        selected = {e.getUniqueID() for e in self.cc.getSelectedEntities()}
        for uid, entity, _name, _parent in walk_tree(self.cc):
            want = uid in wanted
            if want != (uid in selected):
                self.cc.setSelectedInDB(entity, want)
        self.cc.redrawAll()

        # Den tatsächlichen Stand vermerken, nicht die Absicht -- sonst hielte
        # der nächste Tick die eigene Änderung für eine Benutzeraktion.
        self.last_sel = {e.getUniqueID() for e in self.cc.getSelectedEntities()}

    # -- CloudCompare -> Explorer ---------------------------------------------

    def _nodes_for(self, uid):
        """Knoten zu einer Entität -- notfalls über ihre Vorfahren.

        Ein Klick trifft oft das Mesh unter der Dateigruppe ("Mesh" unter
        "SU1001.obj"); der Name, der zählt, steht dann eine Ebene höher.
        """
        seen = set()
        while uid is not None and uid not in seen:
            seen.add(uid)
            nodes = []
            for _config, index in self.runtimes:
                keys = index.keys_for_feature_ids([uid])
                nodes.extend(index.node_ids_for_normalized_keys(keys))
            if nodes:
                return nodes
            uid = self.parents.get(uid)
        return []

    def _poll_selection(self):
        if not self.runtimes:
            return
        sel = {e.getUniqueID() for e in self.cc.getSelectedEntities()}
        if sel == self.last_sel:
            return
        self.last_sel = sel
        if not sel:
            return  # Abwählen lässt den Explorer stehen -- wie in QGIS und Blender
        node_ids = []
        for uid in sorted(sel):
            for node_id in self._nodes_for(uid):
                if node_id not in node_ids:
                    node_ids.append(node_id)
        if node_ids:
            self.call("selectNodes", [node_ids])

    # -- Takt ------------------------------------------------------------------

    def _tick(self):
        self.ticks += 1
        if not self.authed:
            return
        if not self.graph_loaded:
            if self.ticks % DESCRIBE_EVERY == 0:
                self._describe()
            return
        if self.ticks % TREE_EVERY == 0:
            tree = walk_tree(self.cc)
            sig = tuple((uid, name) for uid, _e, name, _p in tree)
            if sig != self.tree_sig:
                first = self.tree_sig is None
                self.tree_sig = sig
                if not first:
                    self._relink(force_report=True)
        self._poll_selection()


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

def start():
    running = getattr(builtins, "_graphexplorer_cc_link", None)
    if running is not None and hasattr(running, "open_dialog") and running.server.isListening():
        # Schon verbunden: Ein zweiter Lauf soll die Verbindung nicht kappen,
        # sondern das öffnen, was man dann meistens will -- die Einstellungen.
        running.open_dialog()
        return
    if running is not None:
        # Eine Kopplung aus einer älteren Fassung dieses Skripts, ohne Dialog.
        try:
            running.stop()
        except Exception:
            pass
        log("Previous link stopped.")

    app = QGuiApplication.instance()
    if app is not None and QThread.currentThread() != app.thread():
        log("Warning: not running in the GUI thread -- timers may not fire.")

    link = CCLink()
    # In builtins, damit die Kopplung das Ende des Skripts überlebt: Der
    # Namensraum des Skripts darf danach verworfen werden, die Timer nicht.
    builtins._graphexplorer_cc_link = link

    QGuiApplication.clipboard().setText(link.address)
    log("Listening. Address (also copied to the clipboard):\n  " + link.address)
    log("In the GraphExplorer: bottom left, 'Connect application', paste, Connect.")
    log("Run this script again to reopen the links dialog.")
    link.open_dialog()


start()
