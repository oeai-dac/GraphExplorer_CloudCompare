"""Verknüpfungen einrichten: Entitätsname gegen Knotentyp + Schlüssel.

Das Gegenstück zu ``config_dialog.py`` und ``linkform.py`` im QGIS-Plugin, mit
einem Feld weniger auf der Objektseite: In CloudCompare gibt es weder Layer
noch Attributfelder, verglichen wird immer der Name der Entität im DB-Tree.

Ohne jeden ``pycc``-Import -- was der Dialog von CloudCompare wissen muss,
bekommt er als Funktionen hereingereicht. Damit lässt er sich auch außerhalb
von CloudCompare öffnen und prüfen.
"""

from qtcompat import QtCore, QtWidgets, enum, run_modal

from linking import (
    DEFAULT_PATTERN,
    NORMALIZE_MODES,
    NORMALIZE_PATTERN,
    SOURCE_ATTR,
    SOURCE_LABEL,
    SOURCE_LABELS,
    DisplayNames,
    LinkConfig,
    LinkIndex,
)

Qt = QtCore.Qt
QApplication = QtWidgets.QApplication
QCheckBox = QtWidgets.QCheckBox
QComboBox = QtWidgets.QComboBox
QDialog = QtWidgets.QDialog
QDialogButtonBox = QtWidgets.QDialogButtonBox
QFormLayout = QtWidgets.QFormLayout
QHBoxLayout = QtWidgets.QHBoxLayout
QLabel = QtWidgets.QLabel
QLineEdit = QtWidgets.QLineEdit
QListWidget = QtWidgets.QListWidget
QMessageBox = QtWidgets.QMessageBox
QPushButton = QtWidgets.QPushButton
QVBoxLayout = QtWidgets.QVBoxLayout

TOOLTIP_ROLE = enum(Qt, "ItemDataRole.ToolTipRole")


def is_usable(link):
    """Vollständig genug für CloudCompare -- ``LinkConfig.is_complete`` verlangt einen Layer."""
    if not link.node_type:
        return False
    return link.node_source != SOURCE_ATTR or bool(link.node_attr)


def describe_link(link, names):
    """``"Entity name  ⇄  Stratigraphic Unit · node label"``."""
    if link.node_source == SOURCE_ATTR:
        right = names.attr(link.node_attr) if link.node_attr else "?"
    else:
        right = SOURCE_LABELS.get(link.node_source, link.node_source).lower()
    node_type = names.type(link.node_type) if link.node_type else "?"
    return "Entity name  ⇄  %s · %s" % (node_type, right)


def cc_wording(text):
    """Den Bericht aus ``linking.py`` in CloudCompare-Begriffe übersetzen.

    ``linking.py`` bleibt Zeichen für Zeichen dieselbe Datei wie in QGIS und
    Blender und spricht deshalb von Layern und Features. Übersetzt wird erst
    hier, bei der Anzeige.
    """
    for old, new in (
        ("layer field and node key", "entity names and node key"),
        ("Only in layer", "Only in CloudCompare"),
        ("only in layer", "only in CloudCompare"),
        ("Several features per key", "Several entities per key"),
        ("features without a usable field value", "entities without a usable name"),
        ("no feature has a usable key value", "no entity has a usable name"),
        ("if the features carry", "if the entities carry"),
        ("The layer side is empty", "The CloudCompare side is empty"),
        ("Layer:  ", "CloudCompare:  "),
        (" features, ", " entities, "),
    ):
        text = text.replace(old, new)
    return text


def select_data(combo, value):
    idx = combo.findData(value) if value else -1
    if idx >= 0:
        combo.setCurrentIndex(idx)


class LinkConfigDialog(QDialog):
    """``graph_info``: Antwort von ``describeGraph``.

    ``check_fn(spec, callback)`` holt die Graph-Seite einer Verknüpfung aus dem
    Explorer, ``object_pairs_fn()`` liefert ``[(uid, name)]`` aus dem DB-Tree.
    """

    def __init__(self, links, graph_info, address, check_fn, object_pairs_fn, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GraphExplorer — Links")
        self.resize(600, 520)

        self._links = [LinkConfig.from_dict(link.to_dict()) for link in links]  # Arbeitskopie
        self._graph_info = graph_info or {"loaded": False, "types": []}
        self.names = DisplayNames(self._graph_info)
        self._address = address
        self._check_fn = check_fn
        self._object_pairs_fn = object_pairs_fn
        self._current = -1
        self._loading = False

        self._build_ui()
        self._reload_list()
        if self._links:
            self.list.setCurrentRow(0)
        else:
            self._set_form_enabled(False)

    # -- Aufbau ---------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("Connection address:"))
        addr = QLineEdit(self._address)
        addr.setReadOnly(True)
        row.addWidget(addr, 1)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self._address))
        row.addWidget(copy)
        outer.addLayout(row)

        # Der Dialog öffnet sich gleich beim Start, also meist noch ohne
        # Verbindung. Diese Zeile sagt, worauf er wartet; die Kopplung
        # schreibt hinein, sobald sich etwas tut (set_status).
        self.status = QLabel()
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        self.auto_hint = QLabel(
            "No links set up: every node type is matched automatically by its node "
            "label (tolerant comparison). Add a link to choose yourself.")
        self.auto_hint.setWordWrap(True)
        outer.addWidget(self.auto_hint)

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_row_changed)
        outer.addWidget(self.list, 1)

        row = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(self._on_add)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._on_remove)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        outer.addLayout(row)

        form = QFormLayout()
        name_label = QLabel("Entity name in the DB tree")
        name_label.setToolTip('File extensions and CloudCompare suffixes such as " - Mesh" '
                              "are ignored. Entities without a match use their parent's name.")
        form.addRow("CloudCompare", name_label)

        self.cb_type = QComboBox()
        self._refill_types()
        self.cb_type.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("Node type", self.cb_type)

        self.cb_source = QComboBox()
        for key, label in SOURCE_LABELS.items():
            self.cb_source.addItem(label, key)
        self.cb_source.currentIndexChanged.connect(self._on_source_changed)
        form.addRow("Key in the graph", self.cb_source)

        self.cb_attr = QComboBox()
        self.cb_attr.currentIndexChanged.connect(self._commit)
        form.addRow("Property", self.cb_attr)

        self.cb_norm = QComboBox()
        for key, label in NORMALIZE_MODES.items():
            self.cb_norm.addItem(label, key)
        self.cb_norm.currentIndexChanged.connect(self._on_normalize_changed)
        form.addRow("Comparison", self.cb_norm)

        # Immer sichtbar, nur gesperrt -- ein Feld, das auftaucht und
        # verschwindet, lässt das Formular springen.
        self.ed_pattern = QLineEdit()
        self.ed_pattern.setPlaceholderText(DEFAULT_PATTERN)
        self.ed_pattern.setToolTip("Regular expression; the first capture group is the key. "
                                   "Empty means: %s" % DEFAULT_PATTERN)
        self.ed_pattern.textChanged.connect(self._commit)
        form.addRow("Pattern", self.ed_pattern)
        outer.addLayout(form)

        self.chk_enabled = QCheckBox("active")
        self.chk_enabled.stateChanged.connect(self._commit)
        outer.addWidget(self.chk_enabled)

        self.btn_check = QPushButton("Check matches")
        self.btn_check.clicked.connect(self._on_check)
        outer.addWidget(self.btn_check)

        buttons = QDialogButtonBox(enum(QDialogButtonBox, "StandardButton.Ok")
                                   | enum(QDialogButtonBox, "StandardButton.Cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # -- Von außen -------------------------------------------------------------

    def set_status(self, text):
        self.status.setText(text)

    def set_graph_info(self, graph_info):
        """Knotentypen nachtragen -- der Graph kommt meist erst, wenn der Dialog schon offen ist."""
        self._graph_info = graph_info or {"loaded": False, "types": []}
        self.names = DisplayNames(self._graph_info)
        self._loading = True
        try:
            self._refill_types()
        finally:
            self._loading = False
        self._reload_list()
        if 0 <= self._current < len(self._links):
            self.list.setCurrentRow(self._current)
            self._load_into_form(self._links[self._current])

    # -- Liste ----------------------------------------------------------------

    def _row_text(self, link):
        return describe_link(link, self.names) + ("" if link.enabled else "  (inactive)")

    def _reload_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        for link in self._links:
            self.list.addItem(self._row_text(link))
        self.list.blockSignals(False)
        self.auto_hint.setVisible(not self._links)

    def _on_add(self):
        # Vorbelegt mit dem, was in der Praxis meistens passt: Knotenlabel,
        # tolerant verglichen, häufigster Knotentyp.
        link = LinkConfig(node_source=SOURCE_LABEL,
                          node_type=self.cb_type.itemData(0) if self.cb_type.count() else "")
        self._links.append(link)
        self._reload_list()
        self.list.setCurrentRow(len(self._links) - 1)
        self._set_form_enabled(True)

    def _on_remove(self):
        if 0 <= self._current < len(self._links):
            del self._links[self._current]
            self._current = -1
            self._reload_list()
            if self._links:
                self.list.setCurrentRow(0)
            else:
                self._set_form_enabled(False)

    def _on_row_changed(self, row):
        self._current = row
        if 0 <= row < len(self._links):
            self._set_form_enabled(True)
            self._load_into_form(self._links[row])

    def _set_form_enabled(self, on):
        for widget in (self.cb_type, self.cb_source, self.cb_norm, self.chk_enabled, self.btn_check):
            widget.setEnabled(on)
        self.cb_attr.setEnabled(on and self.cb_source.currentData() == SOURCE_ATTR)
        self.ed_pattern.setEnabled(on and self.cb_norm.currentData() == NORMALIZE_PATTERN)

    # -- Formular -------------------------------------------------------------

    def _refill_types(self):
        self.cb_type.blockSignals(True)
        self.cb_type.clear()
        for entry in self._graph_info.get("types", []):
            self.cb_type.addItem("%s  (%d)" % (self.names.type(entry["type"]), entry["count"]),
                                 entry["type"])
            self.cb_type.setItemData(self.cb_type.count() - 1, entry["type"], TOOLTIP_ROLE)
        self.cb_type.blockSignals(False)

    def _refill_attrs(self):
        self.cb_attr.blockSignals(True)
        self.cb_attr.clear()
        wanted = self.cb_type.currentData()
        for entry in self._graph_info.get("types", []):
            if entry["type"] == wanted:
                for attr in entry.get("attrs", []):
                    self.cb_attr.addItem(self.names.attr(attr), attr)
                    self.cb_attr.setItemData(self.cb_attr.count() - 1, attr, TOOLTIP_ROLE)
                break
        self.cb_attr.blockSignals(False)

    def _load_into_form(self, link):
        self._loading = True
        try:
            if link.node_type and self.cb_type.findData(link.node_type) < 0:
                # Typ aus einer gespeicherten Verknüpfung, den der geladene
                # Graph nicht (mehr) kennt -- zeigen statt still verwerfen.
                self.cb_type.addItem("%s  (not in graph)" % self.names.type(link.node_type),
                                     link.node_type)
            select_data(self.cb_type, link.node_type)
            select_data(self.cb_source, link.node_source)
            self._refill_attrs()
            select_data(self.cb_attr, link.node_attr)
            select_data(self.cb_norm, link.normalize)
            self.ed_pattern.setText(link.pattern or "")
            self.chk_enabled.setChecked(link.enabled)
            self._set_form_enabled(True)
        finally:
            self._loading = False

    def _commit(self, *_args):
        """Formular -> Datenmodell, bei jeder Änderung."""
        if self._loading or not (0 <= self._current < len(self._links)):
            return
        link = self._links[self._current]
        link.node_type = self.cb_type.currentData() or ""
        link.node_source = self.cb_source.currentData() or SOURCE_LABEL
        link.node_attr = (self.cb_attr.currentData() or "") if link.node_source == SOURCE_ATTR else ""
        link.normalize = self.cb_norm.currentData()
        link.pattern = self.ed_pattern.text()
        link.enabled = self.chk_enabled.isChecked()
        link.name = describe_link(link, self.names)
        self.list.blockSignals(True)
        self.list.item(self._current).setText(self._row_text(link))
        self.list.blockSignals(False)

    def _on_type_changed(self, _idx):
        if self._loading:
            return
        self._refill_attrs()
        self._commit()

    def _on_source_changed(self, _idx):
        if self._loading:
            return
        self.cb_attr.setEnabled(self.cb_source.currentData() == SOURCE_ATTR)
        self._commit()

    def _on_normalize_changed(self, _idx):
        if self._loading:
            return
        self.ed_pattern.setEnabled(self.cb_norm.currentData() == NORMALIZE_PATTERN)
        self._commit()

    # -- Probe ----------------------------------------------------------------

    def _on_check(self):
        if not (0 <= self._current < len(self._links)):
            return
        link = self._links[self._current]
        if not is_usable(link):
            QMessageBox.information(self, "Check matches", "The link is not complete yet.")
            return

        index = LinkIndex(link)
        index.set_layer_side(self._object_pairs_fn())

        def done(entries):
            if entries is None:
                QMessageBox.warning(self, "Check matches",
                                    "No answer from the GraphExplorer — is it connected?")
                return
            index.set_graph_side(entries)
            report = index.report()
            box = QMessageBox(self)
            box.setWindowTitle("Matches")
            box.setText(cc_wording(report.summary()))
            box.setDetailedText(cc_wording(report.details()))
            run_modal(box)

        self._check_fn(link.js_spec(), done)

    # -- Ergebnis -------------------------------------------------------------

    def links(self):
        return self._links
