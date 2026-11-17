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
    ("Glätten nach Regeln", "smooth"),
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


def propose(app, mode: str, instruction: str, jobs: list[dict], rules: list[str],
            signals=None) -> tuple[list[dict], int]:
    """(changes, dropped) for every job {fid, name, text, start, end, existing}, chunk by chunk.

    Runs on a worker thread. Smoothing asks for single edits tied to a rule; the other text modes
    ask for the revised text and diff it."""
    changes, dropped = [], 0
    for job in jobs:
        text = job["text"]
        for c0, c1 in core.chunks(text, job["start"], job["end"]):
            if signals is not None:
                signals.progress.emit(f"{job['name']}, Zeile {core.line_of(text, c0)} …")
            if mode == "code":
                quotes = core.parse_quotes(ask_model(app, prompts.CODING.format(instruction=instruction), text[c0:c1]))
                found = core.coding_changes(text, quotes, c0, c1, job["existing"])
            elif mode == "smooth":
                edits = core.parse_edits(ask_model(app, prompts.smoothing(rules), text[c0:c1]))
                found, lost = core.rule_changes(text, edits, c0, c1, rules)
                dropped += lost
            else:
                system = prompts.SUMMARIZE if mode == "summarize" else prompts.EDIT.format(instruction=instruction)
                revised = ask_model(app, system, text[c0:c1])
                # Models often drop the chunk's trailing line break; keep it so lines do not merge.
                if text[c0:c1].endswith("\n") and not revised.endswith("\n"):
                    revised += "\n"
                found = core.diff_changes(text[c0:c1], revised, c0)
            for change in found:
                change.update(fid=job["fid"], file=job["name"], line=core.line_of(text, change["pos0"]))
            changes += found
    return changes, dropped


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
    """Instruction → proposal as a diff → accept single changes or all → applied, backed up, logged.

    Works on one or several files; smoothing follows the project's own rules and tags every change
    with the rule it applies. Double-clicking a proposal scrolls the diff to it and highlights it."""

    def __init__(self, app, fid: int | None = None, start: int | None = None, end: int | None = None,
                 parent=None):
        super().__init__(parent)
        self.app = app
        self.texts: dict[int, str] = {}
        self.names: dict[int, str] = {}
        self.scopes: dict[int, tuple[int, int]] = {}
        self.changes: list[dict] = []
        self.shown_fid: int | None = None
        self.focus: int | None = None
        self.selection = (fid, start, end) if None not in (fid, start, end) and end > start else None
        self.setWindowTitle(_("AI edit with diff review"))
        self.resize(1100, 800)

        self.files = QtWidgets.QListWidget()
        self.files.setMaximumHeight(110)
        for sid, name in app.conn.execute("select id, name from source where fulltext is not null "
                                          "and lower(ifnull(mediapath, '')) not like '%.pdf' order by name"):
            item = QtWidgets.QListWidgetItem(name)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, sid)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked if sid == fid else QtCore.Qt.CheckState.Unchecked)
            self.files.addItem(item)
        self.scope = QtWidgets.QLabel()
        self.mode = QtWidgets.QComboBox()
        for label, key in MODES:
            self.mode.addItem(label, key)
        self.rules = QtWidgets.QPlainTextEdit("\n".join(core.load_rules(app.project_path, prompts.DEFAULT_RULES)))
        self.rules.setMaximumHeight(120)
        self.rules.setToolTip(_("One smoothing rule per line. They are numbered R1, R2, … and saved with the project."))
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
        self.list.setToolTip(_("Double-click: show the change in the diff"))
        self.diff_title = QtWidgets.QLabel()
        self.diff = QtWidgets.QTextBrowser()
        self.detail = QtWidgets.QLabel()
        self.detail.setWordWrap(True)
        self.select_all = QtWidgets.QPushButton(_("Select all"))
        self.select_none = QtWidgets.QPushButton(_("Select none"))
        self.apply_button = QtWidgets.QPushButton(_("Apply selected"))
        self.apply_button.setEnabled(False)

        form = QtWidgets.QFormLayout()
        form.addRow(_("Files"), self.files)
        form.addRow(_("Scope"), self.scope)
        form.addRow(_("Mode"), self.mode)
        form.addRow(_("Smoothing rules"), self.rules)
        form.addRow(_("Code"), self.code)
        form.addRow(_("Instruction"), self.instruction)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.model_label, 1)
        top.addWidget(self.request_button)
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.diff_title)
        right_layout.addWidget(self.diff, 1)
        right_layout.addWidget(self.detail)
        split = QtWidgets.QSplitter()
        split.addWidget(self.list)
        split.addWidget(right)
        split.setSizes([400, 700])
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

        self.files.itemChanged.connect(self.files_changed)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.request_button.clicked.connect(self.request)
        self.list.itemChanged.connect(self.refresh_diff)
        self.list.currentRowChanged.connect(self.show_detail)
        self.list.itemDoubleClicked.connect(lambda item: self.focus_change(self.list.row(item)))
        self.select_all.clicked.connect(lambda: self.check_all(True))
        self.select_none.clicked.connect(lambda: self.check_all(False))
        self.apply_button.clicked.connect(self.apply)
        self.mode_changed()
        self.load_texts()

    # ---------------------------------------------------------------- state

    def checked_fids(self) -> list[int]:
        return [self.files.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(self.files.count())
                if self.files.item(i).checkState() == QtCore.Qt.CheckState.Checked]

    def load_texts(self):
        """Fresh texts of the checked files; a selection from the coding window limits its own file."""
        fids = self.checked_fids()
        self.texts, self.names, self.scopes = {}, {}, {}
        for fid in fids:
            text, name = self.app.conn.execute("select ifnull(fulltext, ''), name from source where id=?", (fid,)).fetchone()
            self.texts[fid], self.names[fid] = text, name
            if self.selection and self.selection[0] == fid:
                self.scopes[fid] = (self.selection[1], min(self.selection[2], len(text)))
            else:
                self.scopes[fid] = (0, len(text))
        if len(fids) == 1 and self.selection and self.selection[0] == fids[0]:
            start, end = self.scopes[fids[0]]
            text = self.texts[fids[0]]
            self.scope.setText(f"Markierung, Zeilen {core.line_of(text, start)}–{core.line_of(text, end)}")
        else:
            self.scope.setText(f"{len(fids)} Datei(en), jeweils ganzes Transkript" if fids else _("No file checked"))
        self.shown_fid = fids[0] if fids else None
        self.show_changes([])

    def files_changed(self):
        self.load_texts()

    def mode_changed(self):
        key = self.mode.currentData()
        self.code.setEnabled(key == "code")
        self.instruction.setEnabled(key in ("edit", "code"))
        self.rules.setEnabled(key == "smooth")

    def current_rules(self) -> list[str]:
        return core.parse_rules(self.rules.toPlainText())

    def selected(self) -> set[int]:
        return {i for i in range(self.list.count())
                if self.list.item(i).checkState() == QtCore.Qt.CheckState.Checked}

    def check_all(self, on: bool):
        state = QtCore.Qt.CheckState.Checked if on else QtCore.Qt.CheckState.Unchecked
        with QtCore.QSignalBlocker(self.list):
            for i in range(self.list.count()):
                self.list.item(i).setCheckState(state)
        self.refresh_diff()

    def show_result(self, result: tuple[list[dict], int]):
        changes, dropped = result
        self.show_changes(changes)
        if dropped:
            self.summary.setText(self.summary.text() + f" · {dropped} Vorschläge des Modells verworfen "
                                 "(nicht im Text gefunden, überschneidend oder an einem Zeilenumbruch)")

    def show_changes(self, changes: list[dict]):
        self.changes, self.focus = changes, None
        with QtCore.QSignalBlocker(self.list):
            self.list.clear()
            for c in changes:
                old = c["old"].replace("\n", "⏎")
                text = f"{c.get('file', '')} · Z. {c.get('line', '?')} · {c['kind']}: "
                text += old[:60] if c["kind"] == core.CODE_SET else f"„{old[:30]}“ → „{c['new'].replace(chr(10), '⏎')[:30]}“"
                item = QtWidgets.QListWidgetItem(text)
                item.setToolTip(c.get("reason") or "")
                item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                # A smoothing change without a valid rule is not what the rules asked for: opt-in only.
                item.setCheckState(QtCore.Qt.CheckState.Unchecked if c["kind"] == core.NO_RULE
                                   else QtCore.Qt.CheckState.Checked)
                self.list.addItem(item)
        self.summary.setText(core.summary(None, changes) if changes else "")
        self.detail.setText("")
        if changes:
            self.shown_fid = changes[0]["fid"]
        self.refresh_diff()

    def show_detail(self, row: int):
        if not 0 <= row < len(self.changes):
            self.detail.setText("")
            return
        c = self.changes[row]
        rule = ""
        if c.get("rule"):
            rules = self.current_rules()
            rule = f" – Regel: {rules[c['rule'] - 1]}" if c["rule"] <= len(rules) else ""
        reason = f" – Begründung: {c['reason']}" if c.get("reason") else ""
        self.detail.setText(f"{c.get('file', '')} · Zeile {c.get('line', '?')} · {c['kind']}{rule}{reason}")

    def focus_change(self, row: int):
        """Scroll the diff to a proposal and highlight it (switching to its file if needed)."""
        if not 0 <= row < len(self.changes):
            return
        self.focus, self.shown_fid = row, self.changes[row]["fid"]
        self.list.setCurrentRow(row)
        self.refresh_diff()
        self.diff.scrollToAnchor(f"c{row}")

    def refresh_diff(self):
        fid = self.shown_fid
        if fid is None or fid not in self.texts:
            self.diff_title.setText("")
            self.diff.setHtml("")
        else:
            start, end = self.scopes[fid]
            items = [(i, c) for i, c in enumerate(self.changes) if c["fid"] == fid]
            self.diff_title.setText(f"Diff: {self.names[fid]}")
            self.diff.setHtml(core.diff_html(self.texts[fid], start, end, items, self.selected(), self.focus))
        self.apply_button.setEnabled(bool(self.selected()))

    # ---------------------------------------------------------------- model

    def prompt_text(self) -> str:
        key, instruction = self.mode.currentData(), self.instruction.text().strip()
        if key == "smooth":
            return prompts.smoothing(self.current_rules())
        return {"summarize": prompts.SUMMARIZE,
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
        rules = self.current_rules()
        if key in ("edit", "code") and not instruction:
            QtWidgets.QMessageBox.information(self, _("AI edit"), _("Please enter an instruction."))
            return
        if key == "smooth" and not rules:
            QtWidgets.QMessageBox.information(self, _("AI edit"), _("Please enter at least one smoothing rule."))
            return
        self.load_texts()
        if not self.texts:
            QtWidgets.QMessageBox.information(self, _("AI edit"), _("Please check at least one file."))
            return
        if key == "smooth":
            core.save_rules(self.app.project_path, rules)
        jobs = []
        for fid, text in self.texts.items():
            existing = []
            if key == "code":
                existing = [tuple(r) for r in self.app.conn.execute(
                    "select pos0, pos1 from code_text where cid=? and fid=?", (self.code.currentData(), fid))]
            start, end = self.scopes[fid]
            jobs.append({"fid": fid, "name": self.names[fid], "text": text, "start": start, "end": end,
                         "existing": existing})
        self.request_button.setEnabled(False)
        self.summary.setText(_("Asking the local model …"))
        worker = Worker(propose, self.app, key, instruction, jobs, rules)
        worker.signals.progress.connect(lambda msg: self.summary.setText(_("Asking the local model …") + " " + msg))
        worker.signals.result.connect(self.show_result)
        worker.signals.error.connect(lambda _t, value, _tb: self.summary.setText(f"Fehler: {value}"))
        worker.signals.finished.connect(lambda: self.request_button.setEnabled(True))
        QtCore.QThreadPool.globalInstance().start(worker)

    # ---------------------------------------------------------------- apply

    def apply(self):
        selected = self.selected()
        chosen = [c for i, c in enumerate(self.changes) if i in selected]
        if not chosen:
            return
        by_file: dict[int, list[dict]] = {}
        for c in chosen:
            by_file.setdefault(c["fid"], []).append(c)
        for fid in by_file:
            row = self.app.conn.execute("select fulltext from source where id=?", (fid,)).fetchone()
            if row is None or (row[0] or "") != self.texts[fid]:
                QtWidgets.QMessageBox.warning(self, _("AI edit"), self.names[fid] + ": " + _(
                    "The transcript changed since the proposal was made. Please request a new proposal."))
                return
        question = core.summary(None, chosen) + "\n\n" + _(
            "Apply these changes? A backup of the project is made first.")
        if QtWidgets.QMessageBox.question(self, _("AI edit"), question) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        key = self.mode.currentData()
        try:
            backup_path = core.backup(self.app.conn, self.app.project_path)
        except RuntimeError as err:
            QtWidgets.QMessageBox.warning(self, _("AI edit"), str(err))
            return
        model, base = current_model(self.app)
        log = None
        for fid, changes in by_file.items():
            if key == "code":
                apply_codings(self.app, fid, self.code.currentData(), changes,
                              f"KI-Vorschlag ({self.instruction.text().strip()})")
            else:
                core.save_original(self.app.project_path, fid, self.names[fid], self.texts[fid])
                apply_text_changes(self.app, fid, changes)
            log = core.append_log(
                self.app.project_path, file_name=self.names[fid], mode=self.mode.currentText(),
                instruction=self.instruction.text().strip(), prompt=self.prompt_text(), model=f"{model} ({base})",
                text=self.texts[fid], changes=changes,
                proposed=sum(1 for c in self.changes if c["fid"] == fid), backup_path=backup_path)
        if hasattr(self.app, "delete_backup"):
            self.app.delete_backup = False
        self.load_texts()
        self.summary.setText(_("Applied. Logged in ") + str(log))
