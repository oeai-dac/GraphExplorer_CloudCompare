"""Die Verknüpfung zwischen Layer-Features und Graph-Knoten.

Bewusst **ohne jeden QGIS- und Qt-Import**: Die Zuordnung ist der Teil, an dem
sich echte Daten reiben ("SE 2001" gegen "2001" gegen "se2001"), und genau der
soll sich ohne laufendes QGIS prüfen lassen -- dieselbe Trennung wie in
``src/lib/`` des GraphExplorers, das auch React-frei bleibt, damit es unter
Node testbar ist. ``tests/run-linking-checks.py`` fährt das hier mit reinem
Python durch.

Die Arbeitsteilung mit der JS-Seite:

- **JS liefert die Graph-Seite.** Welche Knoten es gibt und welchen Schlüssel
  sie tragen, weiß der Explorer besser als wir -- er kennt das geladene Format
  (Graph-JSON *und* clientseitig importiertes RDF) und er weiß, ob gerade
  Durchgangsknoten zusammengefasst sind, was Knoten-IDs verändert. Python
  bekommt eine fertige Liste ``[{"id": ..., "key": ...}]``.
- **Python besitzt die Layer-Seite** und führt beide zusammen.

Dadurch bleibt hier nur noch Mengenlehre über Strings -- und die Diagnose, die
in der Praxis mehr wert ist als die Kopplung selbst: *welche* SE im Layer
findet keinen Knoten und umgekehrt.
"""

import re
from collections import OrderedDict

# ---------------------------------------------------------------------------
# Normalisierung
# ---------------------------------------------------------------------------

# Stufen statt frei kombinierbarer Schalter: Sie bauen aufeinander auf, und
# eine Auswahlliste ist ehrlicher als drei Checkboxen, aus denen acht
# Kombinationen entstehen, von denen die Hälfte niemand will.
#
# Die beiden letzten sind für Bezeichnungen der Form **T2_SE2004**
# dazugekommen. Darin stecken zwei Zahlengruppen -- der Schnitt und die
# Stratigrafische Einheit --, und "Nur Ziffern" verweigert
# deshalb zu Recht die Auskunft: Zwischen 2 und 2004 zu wählen wäre geraten,
# und geraten hieße, Funde am falschen Befund zu zeigen.
NORMALIZE_EXACT = "exact"
NORMALIZE_TRIM = "trim"
NORMALIZE_LOOSE = "loose"
NORMALIZE_DIGITS = "digits"
NORMALIZE_LAST_NUMBER = "last_number"
NORMALIZE_PATTERN = "pattern"

NORMALIZE_MODES = OrderedDict([
    (NORMALIZE_LOOSE, "Tolerant — ignore case and whitespace"),
    (NORMALIZE_TRIM, "Trim — strip outer whitespace only"),
    (NORMALIZE_EXACT, "Exact — character by character"),
    (NORMALIZE_DIGITS, 'Digits only — "SE 2001" matches "2001"'),
    (NORMALIZE_LAST_NUMBER, 'Last number — "T2_SE2004" matches "2004"'),
    (NORMALIZE_PATTERN, "Pattern — custom regular expression"),
])

#: Vorgabe für den Muster-Modus. Fängt die gängigen Schreibweisen einer SE:
#: "SE2004", "SE 2004", "SE-2004", "T2_SE2004", "se_2004".
DEFAULT_PATTERN = r"SE[\s_-]*(\d+)"

_WS = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")

def normalize_key(value, mode=NORMALIZE_LOOSE, pattern=None):
    """Einen Rohwert auf seine Vergleichsform bringen.

    Gibt ``None`` zurück, wenn der Wert als Schlüssel unbrauchbar ist (leer,
    NULL, oder ohne das, was der gewählte Modus verlangt). ``None`` heißt
    überall weiter unten: nimmt an der Verknüpfung nicht teil.

    Angewandt wird dieselbe Normalisierung auf **beide** Seiten. Genau daraus
    zieht der Muster-Modus seine Kraft: ``SE[\\s_-]*(\\d+)`` macht aus dem
    Feldwert ``T2_SE2004`` eine ``2004`` und aus dem Attributwert
    ``SE 2004`` ebenfalls -- die beiden finden zusammen, ohne dass irgendwo
    eine Umrechnungstabelle stünde.
    """
    if value is None:
        return None

    # QGIS liefert NULL-Felder als QVariant, dessen str() je nach Version
    # "NULL" ergibt. Ein Feature ohne Wert soll aber nicht mit allen anderen
    # wertlosen Features zu einer Gruppe verschmelzen.
    text = value if isinstance(value, str) else str(value)
    if text.strip() == "" or text.strip().upper() == "NULL":
        return None

    if mode == NORMALIZE_EXACT:
        return text
    if mode == NORMALIZE_TRIM:
        return text.strip() or None
    if mode == NORMALIZE_DIGITS:
        found = _DIGITS.findall(text)
        # Mehrere Zahlen ("Schnitt 3, SE 2001", "T2_SE2004") sind nicht
        # eindeutig auflösbar. Die längste zu nehmen wäre geraten; hier gilt:
        # die *einzige* Zahl zählt, sonst nichts. Lieber ein Nichttreffer im
        # Bericht als eine stille Fehlzuordnung.
        return found[0] if len(found) == 1 else None

    if mode == NORMALIZE_LAST_NUMBER:
        # Für Namen der Form "<Schnitt>_SE<Nummer>": Die Einheit steht hinten.
        found = _DIGITS.findall(text)
        return found[-1] if found else None

    if mode == NORMALIZE_PATTERN:
        try:
            match = re.search(pattern or DEFAULT_PATTERN, text, re.IGNORECASE)
        except re.error:
            # Ein unfertiger Ausdruck im Eingabefeld darf nicht die ganze
            # Zuordnung sprengen -- dann eben kein Schlüssel, und der Bericht
            # zeigt lauter Nichttreffer.
            return None
        if not match:
            return None
        # Eine Klammergruppe, wenn es sie gibt: "SE(\d+)" soll die Nummer
        # liefern, nicht "SE2004". Ohne Gruppe zählt der ganze Treffer.
        found = match.group(1) if match.groups() else match.group(0)
        return _WS.sub(" ", found).strip().casefold() or None

    # NORMALIZE_LOOSE
    collapsed = _WS.sub(" ", text).strip().casefold()
    return collapsed or None


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

# Woher der Schlüssel auf der Graph-Seite kommt.
SOURCE_ID = "id"        # die Knoten-ID selbst (robustest, wenn beide Seiten aus derselben Pipeline stammen)
SOURCE_LABEL = "label"  # das Anzeigelabel ("SE 2 (1989)")
SOURCE_ATTR = "attr"    # ein Attribut aus dem "a"-Block des Knotens

SOURCE_LABELS = OrderedDict([
    (SOURCE_ATTR, "Node property"),
    (SOURCE_LABEL, "Node label"),
    (SOURCE_ID, "Node ID"),
])


# ---------------------------------------------------------------------------
# Anzeige
# ---------------------------------------------------------------------------

def short_name(value):
    """Die Kurzform einer URI für die Anzeige -- der Teil nach ``#`` oder ``/``.

    Typen, Properties und Beziehungen aus importiertem RDF heißen
    ``http://www.cidoc-crm.org/cidoc-crm/E22_Human-Made_Object``. In einer
    Auswahlliste ist davon nur das Ende brauchbar; der Rest schiebt genau das
    aus dem Blick, woran man den Eintrag erkennt. Gespeichert und verglichen
    wird weiterhin der volle Wert -- das hier ist nur Anzeige.

    Was keine URI ist (``SE``, ``crm:P46_is_composed_of``), bleibt wie es ist.
    """
    text = "" if value is None else str(value)
    if "://" not in text and not text.startswith("urn:"):
        return text
    tail = text.rstrip("/#")
    for sep in ("#", "/"):
        if sep in tail:
            tail = tail.rsplit(sep, 1)[1]
            break
    return tail or text


def short_names(values):
    return [short_name(v) for v in values]


_PREFIX = re.compile(r"^[A-Za-z][\w.-]*:(?!//)")
_ATTR_PREFIX = re.compile(r"^(crm:|rdfs:|rdf:|skos:)")


class DisplayNames(object):
    """Die Namen, unter denen der GraphExplorer Typen, Beziehungen und Attribute zeigt.

    Seit Vertragsversion 3 schickt der Explorer sie mit ``describeGraph``
    (``labels``), aus denselben Funktionen, mit denen er sie selbst anzeigt --
    dann heißt ein Typ hier "Stratigraphic Unit" und nicht
    ``…/A8_Stratigraphic_Unit#as:stratigraphic_unit``. Für ältere Builds wird
    hier nachgebildet, so gut es ohne Schema geht: Kurzform der URI, ohne
    Präfix wie ``as:``, Unterstriche als Leerzeichen.
    """

    def __init__(self, graph_info=None):
        labels = (graph_info or {}).get("labels") or {}
        self._types = dict(labels.get("types") or {})
        self._rels = dict(labels.get("rels") or {})
        self._attrs = dict(labels.get("attrs") or {})
        # Ältere Antworten tragen den Namen höchstens am Typ-Eintrag selbst.
        for entry in (graph_info or {}).get("types") or []:
            if entry.get("label") and entry.get("type") not in self._types:
                self._types[entry["type"]] = entry["label"]

    @staticmethod
    def _fallback(key):
        text = _PREFIX.sub("", short_name(key))
        return text.replace("_", " ").strip() or short_name(key)

    def type(self, key):
        return self._types.get(key) or self._fallback(key)

    def rel(self, key):
        return self._rels.get(key) or self._fallback(key)

    def attr(self, key):
        if key in self._attrs:
            return self._attrs[key]
        # Wie humaniseKey() im GraphExplorer, plus Kurzform für URIs.
        return _ATTR_PREFIX.sub("", short_name(key)).replace("_", " ")


class LinkConfig(object):
    """Eine Verknüpfung: dieser Layer, dieses Feld, dieser Knotentyp, diese Property.

    Wird als schlichtes dict im QGIS-Projekt abgelegt (siehe ``settings.py``) --
    die Verknüpfung beschreibt das Projekt, nicht die Installation, und soll
    deshalb mit der .qgz weitergegeben werden.
    """

    def __init__(self, layer_id="", layer_field="", node_type="",
                 node_source=SOURCE_ATTR, node_attr="",
                 normalize=NORMALIZE_LOOSE, enabled=True, name="", pattern=""):
        self.layer_id = layer_id
        self.layer_field = layer_field
        self.node_type = node_type
        self.node_source = node_source
        self.node_attr = node_attr
        self.normalize = normalize
        # Nur im Muster-Modus benutzt; leer heißt DEFAULT_PATTERN.
        self.pattern = pattern
        self.enabled = enabled
        self.name = name or (layer_field or "Link")

    def is_complete(self):
        if not (self.layer_id and self.layer_field and self.node_type):
            return False
        if self.node_source == SOURCE_ATTR and not self.node_attr:
            return False
        return True

    def to_dict(self):
        return {
            "layer_id": self.layer_id,
            "layer_field": self.layer_field,
            "node_type": self.node_type,
            "node_source": self.node_source,
            "node_attr": self.node_attr,
            "normalize": self.normalize,
            "pattern": self.pattern,
            "enabled": self.enabled,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            layer_id=d.get("layer_id", ""),
            layer_field=d.get("layer_field", ""),
            node_type=d.get("node_type", ""),
            node_source=d.get("node_source", SOURCE_ATTR),
            node_attr=d.get("node_attr", ""),
            normalize=d.get("normalize", NORMALIZE_LOOSE),
            pattern=d.get("pattern", ""),
            enabled=bool(d.get("enabled", True)),
            name=d.get("name", ""),
        )

    def js_spec(self):
        """Der Teil, den die JS-Seite braucht, um ihre Schlüsselliste zu bauen."""
        return {
            "nodeType": self.node_type,
            "nodeSource": self.node_source,
            "nodeAttr": self.node_attr,
        }


# ---------------------------------------------------------------------------
# Der Index
# ---------------------------------------------------------------------------

class LinkIndex(object):
    """Beide Seiten einer Verknüpfung, zusammengeführt über den normalisierten Schlüssel.

    Der Index ist absichtlich *n:m*. Die Versuchung ist groß, 1:1 anzunehmen --
    eine SE, eine Fläche -- aber schon eine SE, die in zwei Schnitten
    dokumentiert wurde, hat zwei Polygone, und ein Layer kann eine SE-Nummer
    mehrfach führen. Beide Richtungen liefern deshalb immer Listen.
    """

    def __init__(self, config):
        self.config = config
        self._key_to_fids = {}
        self._fid_to_key = {}
        self._key_to_nodes = {}
        self._node_to_key = {}
        self._layer_skipped = 0   # Features ohne brauchbaren Schlüsselwert
        self._graph_skipped = 0   # Knoten ohne brauchbaren Schlüsselwert

    # -- Aufbau --

    def set_layer_side(self, pairs):
        """``pairs``: iterierbar über ``(feature_id, roher_feldwert)``."""
        self._key_to_fids = {}
        self._fid_to_key = {}
        self._layer_skipped = 0
        for fid, raw in pairs:
            key = normalize_key(raw, self.config.normalize, self.config.pattern)
            if key is None:
                self._layer_skipped += 1
                continue
            self._fid_to_key[fid] = key
            self._key_to_fids.setdefault(key, []).append(fid)

    def set_graph_side(self, entries):
        """``entries``: iterierbar über ``{"id": knoten_id, "key": rohwert}``.

        Kommt so von der JS-Seite (``src/explorer-bridge.js`` im GraphExplorer).
        """
        self._key_to_nodes = {}
        self._node_to_key = {}
        self._graph_skipped = 0
        for entry in entries:
            node_id = entry.get("id")
            if not node_id:
                continue
            key = normalize_key(entry.get("key"), self.config.normalize, self.config.pattern)
            if key is None:
                self._graph_skipped += 1
                continue
            self._node_to_key[node_id] = key
            self._key_to_nodes.setdefault(key, []).append(node_id)

    # -- Auflösung --

    def feature_ids_for_keys(self, keys):
        """Alle Feature-IDs zu einer Menge von (rohen) Schlüsseln."""
        out = []
        seen = set()
        for raw in keys or []:
            key = normalize_key(raw, self.config.normalize, self.config.pattern)
            if key is None:
                continue
            for fid in self._key_to_fids.get(key, ()):
                if fid not in seen:
                    seen.add(fid)
                    out.append(fid)
        return out

    def node_ids_for_normalized_keys(self, keys):
        """Wie ``node_ids_for_keys``, aber für schon normalisierte Schlüssel.

        Der Weg vom Wirt zum Explorer geht über ``keys_for_feature_ids``, und
        das liefert bereits normalisierte Schlüssel. Die noch einmal durch
        ``normalize_key`` zu schicken, war ein Fehler: Bei den meisten Stufen
        fällt er nicht auf, weil sie idempotent sind -- bei "Muster" aber
        schon. Aus ``T2_SE2004`` wird ``2004``, und in ``2004`` findet
        ``SE[\\s_-]*(\\d+)`` nichts mehr. Die Richtung Wirt -> Explorer war
        damit still tot, während die Gegenrichtung tadellos lief.
        """
        out = []
        seen = set()
        for key in keys or []:
            for node_id in self._key_to_nodes.get(key, ()):
                if node_id not in seen:
                    seen.add(node_id)
                    out.append(node_id)
        return out

    def feature_ids(self):
        """Alle Features, die mit einem brauchbaren Schlüssel im Index stehen.

        Features ohne Schlüsselwert fehlen hier bewusst: Sie können an keiner
        Übernahme teilnehmen, und sie unter "hat keinen Knoten" zu zählen wäre
        eine falsche Auskunft -- das Problem sitzt im Feld, nicht im Graphen.
        """
        return list(self._fid_to_key)

    def feature_ids_for_normalized_keys(self, keys):
        """Wie ``feature_ids_for_keys``, aber für schon normalisierte Schlüssel.

        Das Gegenstück zu ``node_ids_for_normalized_keys`` und aus demselben
        Grund da: Ein zweiter Durchlauf durch ``normalize_key`` ist bei den
        meisten Stufen folgenlos, bei "Muster" aber nicht -- aus ``T2_SE2004``
        ist dann längst ``2004``, und darin findet ``SE[\\s_-]*(\\d+)`` nichts
        mehr.
        """
        out = []
        seen = set()
        for key in keys or []:
            for fid in self._key_to_fids.get(key, ()):
                if fid not in seen:
                    seen.add(fid)
                    out.append(fid)
        return out

    def keys_for_feature_ids(self, fids):
        """Die normalisierten Schlüssel zu einer Menge von Feature-IDs."""
        out = []
        seen = set()
        for fid in fids or []:
            key = self._fid_to_key.get(fid)
            if key is not None and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def node_ids_for_keys(self, keys):
        out = []
        seen = set()
        for raw in keys or []:
            key = normalize_key(raw, self.config.normalize, self.config.pattern)
            if key is None:
                continue
            for node_id in self._key_to_nodes.get(key, ()):
                if node_id not in seen:
                    seen.add(node_id)
                    out.append(node_id)
        return out

    # -- Diagnose --

    def report(self):
        """Was nicht zusammengefunden hat.

        Das ist mehr als Fehlersuche für die Kopplung: Ein Bericht "12 SE im
        Layer ohne Knoten, 3 Knoten ohne Fläche" ist für sich genommen ein
        Befund über den Datenbestand -- und meistens der erste Grund, aus dem
        jemand das Plugin überhaupt aufmacht.
        """
        layer_keys = set(self._key_to_fids)
        graph_keys = set(self._key_to_nodes)
        matched = layer_keys & graph_keys

        ambiguous_layer = sorted(k for k in matched if len(self._key_to_fids[k]) > 1)
        ambiguous_graph = sorted(k for k in matched if len(self._key_to_nodes[k]) > 1)

        return LinkReport(
            layer_samples=sorted(layer_keys)[:LinkReport.SAMPLE],
            graph_samples=sorted(graph_keys)[:LinkReport.SAMPLE],
            matched_keys=len(matched),
            layer_features=len(self._fid_to_key),
            layer_keys=len(layer_keys),
            layer_skipped=self._layer_skipped,
            graph_nodes=len(self._node_to_key),
            graph_keys=len(graph_keys),
            graph_skipped=self._graph_skipped,
            only_in_layer=sorted(layer_keys - graph_keys),
            only_in_graph=sorted(graph_keys - layer_keys),
            ambiguous_layer=ambiguous_layer,
            ambiguous_graph=ambiguous_graph,
        )


class LinkReport(object):
    """Ergebnis eines Abgleichs, in menschenlesbarer Form."""

    #: Wie viele Nichttreffer der Kurzbericht namentlich nennt.
    SAMPLE = 8

    def __init__(self, matched_keys, layer_features, layer_keys, layer_skipped,
                 graph_nodes, graph_keys, graph_skipped,
                 only_in_layer, only_in_graph, ambiguous_layer, ambiguous_graph,
                 layer_samples=(), graph_samples=()):
        # Beispiele von *beiden* Seiten, auch wenn gar nichts zusammenfand.
        # Genau das fehlte beim ersten Versuch in Blender: Der Bericht sagte
        # "keine Übereinstimmung", aber nicht, dass die Graph-Seite schlicht
        # leer war -- und damit auch nicht, dass die falsche Schlüsselquelle
        # gewählt war. Zwei Beispiellisten nebeneinander zeigen das sofort.
        self.layer_samples = list(layer_samples)
        self.graph_samples = list(graph_samples)
        self.matched_keys = matched_keys
        self.layer_features = layer_features
        self.layer_keys = layer_keys
        self.layer_skipped = layer_skipped
        self.graph_nodes = graph_nodes
        self.graph_keys = graph_keys
        self.graph_skipped = graph_skipped
        self.only_in_layer = only_in_layer
        self.only_in_graph = only_in_graph
        self.ambiguous_layer = ambiguous_layer
        self.ambiguous_graph = ambiguous_graph

    @property
    def ok(self):
        return self.matched_keys > 0

    def _sample(self, keys):
        head = ", ".join(short_names(keys[: self.SAMPLE]))
        rest = len(keys) - self.SAMPLE
        return head + (" … (+%d more)" % rest if rest > 0 else "")

    def summary(self):
        """Eine Zeile für die Statusleiste des Dockfensters."""
        if not self.matched_keys:
            return "No match — layer field and node key do not fit together."
        return "%d keys linked · %d only in layer · %d only in graph" % (
            self.matched_keys, len(self.only_in_layer), len(self.only_in_graph),
        )

    def details(self):
        """Der ausführliche Bericht für den Konfigurationsdialog."""
        lines = [
            "Layer:  %d features, %d distinct keys" % (self.layer_features, self.layer_keys),
            "        %s" % (", ".join(short_names(self.layer_samples)) or "(none)"),
            "Graph:  %d nodes,    %d distinct keys" % (self.graph_nodes, self.graph_keys),
            "        %s" % (", ".join(short_names(self.graph_samples)) or "(none)"),
            "",
            "Linked: %d keys" % self.matched_keys,
        ]
        # Die häufigste Ursache zuerst nennen, statt sie aus den Zahlen
        # herauslesen zu lassen.
        if not self.graph_keys:
            lines.append(
                "\nThe graph side is empty. Usually this means the key source does "
                "not fit: if the features carry the same name as the nodes, choose "
                "\"Node label\" -- \"Node property\" only if the name is stored in "
                "a named property of the node."
            )
        elif not self.layer_keys:
            lines.append("\nThe layer side is empty -- no feature has a usable key value.")
        if self.layer_skipped:
            lines.append("%d features without a usable field value skipped." % self.layer_skipped)
        if self.graph_skipped:
            lines.append("%d nodes without a usable key value skipped." % self.graph_skipped)
        if self.only_in_layer:
            lines += ["", "Only in layer (%d): %s" % (len(self.only_in_layer), self._sample(self.only_in_layer))]
        if self.only_in_graph:
            lines += ["", "Only in graph (%d): %s" % (len(self.only_in_graph), self._sample(self.only_in_graph))]
        if self.ambiguous_layer:
            lines += ["", "Several features per key (%d): %s"
                      % (len(self.ambiguous_layer), self._sample(self.ambiguous_layer))]
        if self.ambiguous_graph:
            lines += ["", "Several nodes per key (%d): %s"
                      % (len(self.ambiguous_graph), self._sample(self.ambiguous_graph))]
        return "\n".join(lines)
