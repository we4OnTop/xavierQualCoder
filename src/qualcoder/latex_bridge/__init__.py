"""
This file is part of QualCoder (fork: LaTeX bridge, see docs/latex_bridge.md).

QualCoder is free software: you can redistribute it and/or modify it under the
terms of the GNU Lesser General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later version.

Link to the xarles LaTeX editor. Existing QualCoder files only call the three hooks below, so
upstream merges stay small:
    install_main(main_window)                        __main__.py, MainWindow.__init__
    install_code_text(dialog)                        code_text.py, DialogCodeText.__init__
    add_text_menu_actions(dialog, menu, position)    code_text.py, text_edit_menu
"""

import logging

from PyQt6 import QtCore, QtWidgets

from . import core

logger = logging.getLogger(__name__)
HEARTBEAT_MS = 10_000


def _editor_check(project_path, signals=None):
    return core.editor_status(project_path, core.EDITOR_URL)


def install_main(window) -> None:
    """Status of the LaTeX editor in the status bar, the heartbeat it reads, and a LaTeX menu."""
    from ..ai_async_worker import Worker

    app = window.app
    label = QtWidgets.QLabel("LaTeX-Editor: …")
    window.statusBar().addPermanentWidget(label)

    def tick():
        project = getattr(app, "project_path", "") or ""
        if project:
            try:
                core.write_heartbeat(project, getattr(app, "version", ""))
            except OSError as err:
                logger.debug("latex_bridge heartbeat not written: %s", err)
        worker = Worker(_editor_check, project)
        worker.signals.result.connect(lambda result: label.setText(result[1]))
        QtCore.QThreadPool.globalInstance().start(worker)

    menu = window.ui.menubar.addMenu("LaTeX")
    menu.addAction(_("AI edit with diff review…")).triggered.connect(lambda: open_ai_edit(app, window))
    menu.addAction(_("Check LaTeX editor connection")).triggered.connect(tick)
    window.latex_bridge_timer = QtCore.QTimer(window)
    window.latex_bridge_timer.timeout.connect(tick)
    window.latex_bridge_timer.start(HEARTBEAT_MS)
    tick()


def open_ai_edit(app, parent=None, fid=None, start=None, end=None) -> None:
    if not getattr(app, "project_path", ""):
        QtWidgets.QMessageBox.information(parent, "LaTeX", _("Please open a project first."))
        return
    from .ai_edit import AiEditDialog

    AiEditDialog(app, fid, start, end, parent).exec()


def _codings(dialog) -> list[dict]:
    return [c for c in getattr(dialog, "code_text", []) if c.get("ctid") is not None]


def _selection(dialog) -> tuple[int, int, str]:
    """Absolute start and end of the selection (the view may show only a part of a large file)."""
    cursor = dialog.ui.plainTextEdit.textCursor()
    offset = dialog.file_.get("start", 0) if dialog.file_ else 0
    return cursor.selectionStart() + offset, cursor.selectionEnd() + offset, cursor.selectedText().replace(" ", "\n")


def install_code_text(dialog) -> None:
    """Dragging coded text carries the coding (ctid etc.) besides the plain text."""
    edit = dialog.ui.plainTextEdit
    base = edit.createMimeDataFromSelection

    def create_mime_data():
        try:
            start, end, text = _selection(dialog)
            coding = core.segment_for_selection(_codings(dialog), start, end) if dialog.file_ else None
        except Exception as err:  # a drag must never fail because of the extra data
            logger.debug("latex_bridge drag data: %s", err)
            coding = None
        if not coding:
            return base()
        # A fresh QMimeData: Qt's own text-edit mime data regenerates its HTML and ignores setHtml.
        payload = core.drag_payload(coding, dialog.file_)
        mime = QtCore.QMimeData()
        mime.setText(text)
        mime.setHtml(core.drag_html(payload, text))
        mime.setData(core.MIME, QtCore.QByteArray(payload.encode("utf-8")))
        return mime

    # Monkey-patching the virtual on this instance: PyQt dispatches to it from C++ (drag and copy).
    edit.createMimeDataFromSelection = create_mime_data


def add_text_menu_actions(dialog, menu, position) -> None:
    """"Copy LaTeX reference" on a coded passage, and the AI edit for a selection."""
    if not dialog.file_:
        return
    start, end, _text = _selection(dialog)
    if start == end:
        offset = dialog.file_.get("start", 0)
        start = end = dialog.ui.plainTextEdit.cursorForPosition(position).position() + offset
    coding = core.segment_for_selection(_codings(dialog), start, end)
    if coding:
        action = menu.addAction(_("Copy LaTeX reference") + f"  {core.iref(coding['ctid'])}")
        action.triggered.connect(lambda: QtWidgets.QApplication.clipboard().setText(core.iref(coding["ctid"])))
    if end > start:
        action = menu.addAction(_("AI edit with diff review…"))
        action.triggered.connect(lambda: open_ai_edit(dialog.app, dialog, dialog.file_["id"], start, end))
