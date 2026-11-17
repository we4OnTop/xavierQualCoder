"""
This file is part of QualCoder (fork: LaTeX bridge, see docs/latex_bridge.md).

QualCoder is free software: you can redistribute it and/or modify it under the
terms of the GNU Lesser General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later version.

AI edit with diff review: the local model proposes, nothing changes until the user accepts. Text
changes go through QualCoder's own edit dialog (so codings, annotations and cases move with the
text), codings through QualCoder's connection. Every accepted change is backed up and logged.
"""

import datetime
import logging

from PyQt6 import QtCore, QtGui, QtWidgets

from . import core, prompts

logger = logging.getLogger(__name__)

MODES = [
    ("Glätten (fester Prompt)", "smooth"),
    ("Zusammenfassen", "summarize"),
    ("Eigene Anweisung (Text ändern)", "edit"),
    ("Überall markieren (Code setzen)", "code"),
]


def current_model(app) -> tuple[str, str]:
    """(model name, api_base) of the AI profile selected in QualCoder's AI settings."""
    try:
        model = app.ai_models[int(app.settings.get("ai_model_index", -1))]
    except (IndexError, ValueError, TypeError, AttributeError):
        return "", ""
    return str(model.get("large_model") or model.get("name", "")), str(model.get("api_base", ""))


def ask_model(app, system: str, text: str) -> str:
    """One request through QualCoder's existing OpenAI-compatible connection (logged in its AI log)."""
    answer = app.ai.invoke_with_logging([("system", system), ("human", text)], context="latex_bridge")
    return str(getattr(answer, "content", answer))


def propose(app, mode: str, instruction: str, text: str, start: int, end: int,
            existing: list[tuple[int, int]], signals=None) -> list[dict]:
    """The proposals for [start, end) of text, chunk by chunk. Runs on a worker thread."""
    changes = []
    for c0, c1 in core.chunks(text, start, end):
        if signals is not None:
            signals.progress.emit(f"Zeile {core.line_of(text, c0)} …")
        if mode == "code":
            quotes = core.parse_quotes(ask_model(app, prompts.CODING.format(instruction=instruction), text[c0:c1]))
            changes += core.coding_changes(text, quotes, c0, c1, existing)
            continue
        system = {"smooth": prompts.SMOOTHING, "summarize": prompts.SUMMARIZE}.get(mode) or \
            prompts.EDIT.format(instruction=instruction)
        revised = ask_model(app, system, text[c0:c1])
        # Models often drop the chunk's trailing line break; keep it so lines do not merge.
        if text[c0:c1].endswith("\n") and not revised.endswith("\n"):
            revised += "\n"
        changes += core.diff_changes(text[c0:c1], revised, c0)
    return changes


def apply_text_changes(app, fid: int, changes: list[dict]) -> None:
    """Applies changes through QualCoder's edit dialog: one delete and one insert at a time, the edits
    its position tracking handles, so codings, annotations and case links move with the text."""
    from ..edit_textfile import DialogEditTextFile

    dialog = DialogEditTextFile(app, fid)
    edit = dialog.ui.plainTextEdit
    for change in sorted(changes, key=lambda c: c["pos0"], reverse=True):
        cursor = edit.textCursor()
        if change["pos1"] > change["pos0"]:
            cursor.setPosition(change["pos0"])
            cursor.setPosition(change["pos1"], QtGui.QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        if change["new"]:
            cursor.setPosition(change["pos0"])
            cursor.insertText(change["new"])
    dialog.accept()


def apply_codings(app, fid: int, cid: int, changes: list[dict], memo: str) -> None:
    now = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    owner = app.settings.get("codername", "")
    cur = app.conn.cursor()
    try:
        for c in changes:
            cur.execute("insert or ignore into code_text (cid, fid, seltext, pos0, pos1, owner, date, memo) "
                        "values (?,?,?,?,?,?,?,?)", (cid, fid, c["old"], c["pos0"], c["pos1"], owner, now, memo))
        app.conn.commit()
    except Exception:
        app.conn.rollback()
        raise
    if getattr(app, "project_events", None) is not None:
        app.project_events.emit_table_changes(["code_text"], source=None)


class AiEditDialog(QtWidgets.QDialog):
    """Instruction → proposal as a diff → accept single changes or all → applied, backed up, logged."""

    def __init__(self, app, fid: int | None = None, start: int | None = None, end: int | None = None,
                 parent=None):
        super().__init__(parent)
        self.app = app
        self.text, self.start, self.end, self.fid = "", 0, 0, None
        self.changes: list[dict] = []
        self.selection = (fid, start, end) if fid is not None and start is not None and end is not None and end > start else None
        self.setWindowTitle(_("AI edit with diff review"))
        self.resize(1000, 700)

        self.files = QtWidgets.QComboBox()
        for sid, name in app.conn.execute("select id, name from source where fulltext is not null "
                                          "and lower(ifnull(mediapath, '')) not like '%.pdf' order by name"):
            self.files.addItem(name, sid)
        if fid is not None:
            self.files.setCurrentIndex(max(0, self.files.findData(fid)))
        self.scope = QtWidgets.QLabel()
        self.mode = QtWidgets.QComboBox()
        for label, key in MODES:
            self.mode.addItem(label, key)
        self.code = QtWidgets.QComboBox()
        for cid, name in app.conn.execute("select cid, name from code_name order by name"):
            self.code.addItem(name, cid)
        self.instruction = QtWidgets.QLineEdit()
        self.instruction.setPlaceholderText("z. B. „überall, wo Zeitdruck beschrieben wird“")
        self.request_button = QtWidgets.QPushButton(_("Request proposal"))
        model, base = current_model(app)
        self.model_label = QtWidgets.QLabel(f"Modell: {model or '–'} · {base or 'kein KI-Profil'}")
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        self.list = QtWidgets.QListWidget()
        self.diff = QtWidgets.QTextBrowser()
        self.select_all = QtWidgets.QPushButton(_("Select all"))
        self.select_none = QtWidgets.QPushButton(_("Select none"))
        self.apply_button = QtWidgets.QPushButton(_("Apply selected"))
        self.apply_button.setEnabled(False)

        form = QtWidgets.QFormLayout()
        form.addRow(_("File"), self.files)
        form.addRow(_("Scope"), self.scope)
        form.addRow(_("Mode"), self.mode)
        form.addRow(_("Code"), self.code)
        form.addRow(_("Instruction"), self.instruction)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.model_label, 1)
        top.addWidget(self.request_button)
        split = QtWidgets.QSplitter()
        split.addWidget(self.list)
        split.addWidget(self.diff)
        split.setSizes([350, 650])
        buttons = QtWidgets.QHBoxLayout()
        for button in (self.select_all, self.select_none):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.apply_button)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(top)
        layout.addWidget(self.summary)
        layout.addWidget(split, 1)
        layout.addLayout(buttons)

        self.files.currentIndexChanged.connect(self.file_changed)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.request_button.clicked.connect(self.request)
        self.list.itemChanged.connect(self.refresh_diff)
        self.select_all.clicked.connect(lambda: self.check_all(True))
        self.select_none.clicked.connect(lambda: self.check_all(False))
        self.apply_button.clicked.connect(self.apply)
        self.mode_changed()
        self.load_text()

    # ---------------------------------------------------------------- state

    def load_text(self):
        self.fid = self.files.currentData()
        row = self.app.conn.execute("select fulltext from source where id=?", (self.fid,)).fetchone()
        self.text = row[0] if row and row[0] else ""
        if self.selection and self.selection[0] == self.fid:
            self.start, self.end = self.selection[1], min(self.selection[2], len(self.text))
            self.scope.setText(f"Markierung, Zeilen {core.line_of(self.text, self.start)}–{core.line_of(self.text, self.end)}")
        else:
            self.start, self.end = 0, len(self.text)
            self.scope.setText(f"Ganzes Transkript ({len(self.text)} Zeichen)")
        self.show_changes([])

    def file_changed(self):
        self.selection = None
        self.load_text()

    def mode_changed(self):
        key = self.mode.currentData()
        self.code.setEnabled(key == "code")
        self.instruction.setEnabled(key in ("edit", "code"))

    def selected(self) -> set[int]:
        return {i for i in range(self.list.count())
                if self.list.item(i).checkState() == QtCore.Qt.CheckState.Checked}

    def check_all(self, on: bool):
        state = QtCore.Qt.CheckState.Checked if on else QtCore.Qt.CheckState.Unchecked
        with QtCore.QSignalBlocker(self.list):
            for i in range(self.list.count()):
                self.list.item(i).setCheckState(state)
        self.refresh_diff()

    def show_changes(self, changes: list[dict]):
        self.changes = changes
        with QtCore.QSignalBlocker(self.list):
            self.list.clear()
            for c in changes:
                old = c["old"].replace("\n", "⏎")
                text = f"Z. {core.line_of(self.text, c['pos0'])} · {c['kind']}: "
                text += old[:60] if c["kind"] == core.CODE_SET else f"„{old[:30]}“ → „{c['new'].replace(chr(10), '⏎')[:30]}“"
                item = QtWidgets.QListWidgetItem(text)
                item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(QtCore.Qt.CheckState.Checked)
                self.list.addItem(item)
        self.summary.setText(core.summary(self.text, changes) if changes else "")
        self.refresh_diff()

    def refresh_diff(self):
        self.diff.setHtml(core.diff_html(self.text, self.start, self.end, self.changes, self.selected()))
        self.apply_button.setEnabled(bool(self.selected()))

    # ---------------------------------------------------------------- model

    def prompt_text(self) -> str:
        key, instruction = self.mode.currentData(), self.instruction.text().strip()
        return {"smooth": prompts.SMOOTHING, "summarize": prompts.SUMMARIZE,
                "code": prompts.CODING.format(instruction=instruction)}.get(key) or prompts.EDIT.format(instruction=instruction)

    def request(self):
        from ..ai_async_worker import Worker
        from ..ai_runtime import ensure_ai_ready

        model, base = current_model(self.app)
        if not core.is_loopback_url(base):
            QtWidgets.QMessageBox.warning(self, _("AI edit"), _(
                "Only a local model is allowed here (an AI profile whose API address is localhost, "
                "e.g. llama.cpp or Ollama). Current profile: ") + (base or "–"))
            return
        if not ensure_ai_ready(self.app, _("AI edit")):
            return
        key = self.mode.currentData()
        instruction = self.instruction.text().strip()
        if key in ("edit", "code") and not instruction:
            QtWidgets.QMessageBox.information(self, _("AI edit"), _("Please enter an instruction."))
            return
        self.load_text()
        existing = []
        if key == "code":
            existing = [tuple(r) for r in self.app.conn.execute(
                "select pos0, pos1 from code_text where cid=? and fid=?", (self.code.currentData(), self.fid))]
        self.request_button.setEnabled(False)
        self.summary.setText(_("Asking the local model …"))
        worker = Worker(propose, self.app, key, instruction, self.text, self.start, self.end, existing)
        worker.signals.progress.connect(lambda msg: self.summary.setText(_("Asking the local model …") + " " + msg))
        worker.signals.result.connect(self.show_changes)
        worker.signals.error.connect(lambda _t, value, _tb: self.summary.setText(f"Fehler: {value}"))
        worker.signals.finished.connect(lambda: self.request_button.setEnabled(True))
        QtCore.QThreadPool.globalInstance().start(worker)

    # ---------------------------------------------------------------- apply

    def apply(self):
        chosen = [c for i, c in enumerate(self.changes) if i in self.selected()]
        row = self.app.conn.execute("select fulltext, name from source where id=?", (self.fid,)).fetchone()
        if not chosen or row is None:
            return
        if (row[0] or "") != self.text:
            QtWidgets.QMessageBox.warning(self, _("AI edit"), _(
                "The transcript changed since the proposal was made. Please request a new proposal."))
            return
        question = core.summary(self.text, chosen) + "\n\n" + _(
            "Apply these changes? A backup of the project is made first.")
        if QtWidgets.QMessageBox.question(self, _("AI edit"), question) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        key = self.mode.currentData()
        backup_path = core.backup(self.app.conn, self.app.project_path)
        if key == "code":
            apply_codings(self.app, self.fid, self.code.currentData(), chosen,
                          f"KI-Vorschlag ({self.instruction.text().strip()})")
        else:
            core.save_original(self.app.project_path, self.fid, row[1], self.text)
            apply_text_changes(self.app, self.fid, chosen)
        if hasattr(self.app, "delete_backup"):
            self.app.delete_backup = False
        model, base = current_model(self.app)
        log = core.append_log(
            self.app.project_path, file_name=row[1], mode=self.mode.currentText(),
            instruction=self.instruction.text().strip(), prompt=self.prompt_text(), model=f"{model} ({base})",
            text=self.text, changes=chosen, proposed=len(self.changes), backup_path=backup_path)
        self.load_text()
        self.summary.setText(_("Applied. Logged in ") + str(log))
