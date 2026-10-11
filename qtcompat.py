"""Qt für das Skript -- PyQt5 oder PySide6, je nachdem, was CloudCompare mitbringt.

CloudCompare 2.13 aus dem Windows-Installer bringt Qt 5 und PyQt5 mit. Die
Fassung auf Flathub (Linux) baut schon auf Qt 6; dort stellt die
Laufzeitumgebung PySide6 bereit. Das Skript braucht von Qt nur Fenster,
Zwischenablage und Timer -- dafür genügt diese dünne Schicht über beiden.

Genommen wird nicht die erste Bindung, die sich importieren lässt, sondern die,
die zu der Qt-Bibliothek passt, die CloudCompare **selbst geladen hat**. Eine
PyQt5 in einem CloudCompare mit Qt 6 würde eine zweite, fremde Qt in den Prozess
holen: Sie fände die Anwendung von CloudCompare nicht, und ihre Fenster und Timer
liefen ins Leere.

Ohne jeden ``pycc``-Import, damit sich der Dialog auch außerhalb von
CloudCompare prüfen lässt.
"""

import importlib
import sys

#: Bindungen je Qt-Hauptversion, in dieser Reihenfolge versucht.
BINDINGS = {
    5: ("PyQt5", "PySide2"),
    6: ("PySide6", "PyQt6"),
}


def loaded_qt_major():
    """5 oder 6 -- je nachdem, welche Qt schon im Prozess steckt. ``None``: unbekannt.

    Außerhalb von CloudCompare (und unter macOS) ist noch keine Qt geladen
    bzw. lässt sich das nicht so einfach feststellen; dann wird jede Bindung
    der Reihe nach versucht.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.GetModuleHandleW.restype = ctypes.c_void_p
            for major in (6, 5):
                if kernel32.GetModuleHandleW("Qt%dCore.dll" % major):
                    return major
        except (OSError, AttributeError):
            return None
        return None
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/maps", encoding="utf-8", errors="replace") as f:
                maps = f.read()
        except OSError:
            return None
        for major in (6, 5):
            if "libQt%dCore" % major in maps:
                return major
    return None


def _load():
    major = loaded_qt_major()
    names = BINDINGS[major] if major else BINDINGS[5] + BINDINGS[6]
    errors = []
    for name in names:
        try:
            core = importlib.import_module(name + ".QtCore")
            gui = importlib.import_module(name + ".QtGui")
            widgets = importlib.import_module(name + ".QtWidgets")
            return name, core, gui, widgets
        except ImportError as e:
            errors.append("  %s: %s" % (name, e))
    raise ImportError(
        "GraphExplorer Link needs Python bindings for Qt%s (%s), but none could be "
        "imported:\n%s\nCloudCompare 2.13 for Windows includes PyQt5; the Flatpak "
        "version for Linux includes PySide6."
        % (" %d" % major if major else "", " or ".join(names), "\n".join(errors)))


BINDING, QtCore, QtGui, QtWidgets = _load()


def enum(owner, path):
    """Einen Enum-Wert holen, egal wie die Bindung ihn ablegt.

    ``enum(QDialogButtonBox, "StandardButton.Ok")``: PyQt6 kennt nur die lange
    Form, ältere PyQt5 nur die kurze (``QDialogButtonBox.Ok``).
    """
    obj = owner
    try:
        for part in path.split("."):
            obj = getattr(obj, part)
        return obj
    except AttributeError:
        return getattr(owner, path.rsplit(".", 1)[-1])


def as_int(value):
    """Enum-Wert oder Zahl als ``int`` -- PySide6 und PyQt6 haben echte Enums."""
    try:
        return int(value)
    except TypeError:
        return int(value.value)


def run_modal(widget):
    """``exec()`` -- in PyQt5 heißt es ``exec_()``, in PyQt6 nur noch ``exec()``."""
    fn = getattr(widget, "exec", None) or getattr(widget, "exec_")
    return fn()
