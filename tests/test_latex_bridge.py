"""Tests for the fork's LaTeX bridge (src/qualcoder/latex_bridge, docs/latex_bridge.md)."""

import gc
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PyQt6 import QtCore, QtWidgets

from qualcoder import latex_bridge
from qualcoder.latex_bridge import ai_edit, core, prompts

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TEXT = (
    "I: Wie war das?\n"
    "B1: Also äh das war halt sehr sehr anstrengend.\n"
    "Wir hatten Zeitdruck."
)


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_segment_for_selection_prefers_the_smallest_containing_coding(self):
        codings = [{"ctid": 1, "pos0": 0, "pos1": 50}, {"ctid": 2, "pos0": 10, "pos1": 20},
                   {"ctid": 3, "pos0": 40, "pos1": 80}]
        self.assertEqual(core.segment_for_selection(codings, 12, 15)["ctid"], 2)
        self.assertEqual(core.segment_for_selection(codings, 15, 15)["ctid"], 2)
        self.assertEqual(core.segment_for_selection(codings, 45, 90)["ctid"], 3)
        self.assertIsNone(core.segment_for_selection(codings, 85, 90))

    def test_drag_payload_and_html_are_escaped(self):
        payload = core.drag_payload({"ctid": 7, "pos0": 1, "pos1": 5, "cid": 3}, {"id": 2, "name": 'A "B".txt'})
        self.assertEqual(json.loads(payload)["ctid"], 7)
        markup = core.drag_html(payload, "<b>x</b>")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", markup)
        self.assertIn('data-qcref="{&quot;ctid&quot;: 7', markup)
        self.assertEqual(core.iref(7), r"\iref{7}")

    def test_editor_status_only_contacts_loopback(self):
        self.assertTrue(core.is_loopback_url("http://127.0.0.1:8088/qualcoder/health"))
        self.assertFalse(core.is_loopback_url("http://example.org/health"))
        with mock.patch("urllib.request.OpenerDirector.open") as opener:
            self.assertEqual(core.editor_status(self.tmp, "https://example.org/x")[0], False)
            opener.assert_not_called()
        self.assertEqual(core.editor_status(self.tmp, "http://127.0.0.1:9/none", timeout=0.5),
                         (False, "LaTeX-Editor: nicht erreichbar"))

    def test_heartbeat(self):
        core.write_heartbeat(self.tmp, "3.9")
        beat = json.loads((Path(self.tmp) / "latex_bridge" / "heartbeat.json").read_text())
        self.assertAlmostEqual(beat["time"], time.time(), delta=5)

    def test_diff_and_classification(self):
        revised = "I: Wie war das?\nB1: Das war sehr anstrengend.\nWir hatten Zeitdruck."
        changes = core.diff_changes(TEXT, revised)
        kinds = [c["kind"] for c in changes]
        self.assertEqual(kinds, ["Füllwort entfernt", "Füllwort entfernt", "Schreibweise angepasst",
                                 "Füllwort entfernt", "Wiederholung entfernt"])
        # Applying the changes from the back reproduces the revised text.
        text = TEXT
        for c in sorted(changes, key=lambda c: c["pos0"], reverse=True):
            text = text[:c["pos0"]] + c["new"] + text[c["pos1"]:]
        self.assertEqual(text, revised)
        self.assertEqual(core.classify("a b c d", 0, 7, "d c b a"), "Satz umgestellt")
        long = "x " * 40
        self.assertEqual(core.classify(long, 0, len(long), "kurz"), "zusammengefasst")
        self.assertIn("Zeilen 2–2", core.summary(TEXT, changes).replace("Zeile 2", "Zeilen 2–2"))

    def test_chunks_cut_at_line_breaks(self):
        text = "aaaa\nbbbb\ncccc\n"
        self.assertEqual(core.chunks(text, 0, len(text), size=10), [(0, 10), (10, 15)])
        self.assertEqual(core.chunks("x" * 25, 0, 25, size=10), [(0, 10), (10, 20), (20, 25)])

    def test_coding_quotes(self):
        self.assertEqual(core.parse_quotes('```json\n["Wir hatten  Zeitdruck", {"quote": "sehr anstrengend"}]\n```'),
                         ["Wir hatten  Zeitdruck", "sehr anstrengend"])
        self.assertEqual(core.parse_quotes("keine Treffer"), [])
        changes = core.coding_changes(TEXT, ["Wir hatten\nZeitdruck", "sehr anstrengend", "fehlt"], 0, len(TEXT), [])
        self.assertEqual([c["old"] for c in changes], ["sehr anstrengend", "Wir hatten Zeitdruck"])
        start = TEXT.index("Wir")
        coded = [(start, start + len("Wir hatten Zeitdruck"))]
        self.assertEqual(core.coding_changes(TEXT, ["Wir hatten Zeitdruck"], 0, len(TEXT), coded), [])

    def test_backup_original_and_log(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("create table t (x)")
        path = core.backup(conn, self.tmp)
        self.assertEqual(sqlite3.connect(path).execute("select name from sqlite_master").fetchone()[0], "t")
        original = core.save_original(self.tmp, 3, "Interview 3.txt", "alt")
        core.save_original(self.tmp, 3, "Interview 3.txt", "neu")
        self.assertEqual(original.read_text(), "alt")
        changes = core.diff_changes(TEXT, TEXT.replace("äh ", ""))
        log = core.append_log(self.tmp, file_name="Interview 3.txt", mode="Glätten", instruction="",
                              prompt=prompts.SMOOTHING, model="gemma (http://localhost:8081/v1/)", text=TEXT,
                              changes=changes, proposed=2, backup_path=path).read_text()
        self.assertIn("Füllwort entfernt", log)
        self.assertIn("Sprecherkennungen", log)
        self.assertIn("@@ Zeile 2 · Füllwort entfernt\n-  äh", log)

    def test_diff_html_marks_and_escapes(self):
        changes = core.diff_changes("a <b> c", "a c")
        markup = core.diff_html("a <b> c", 0, 7, list(enumerate(changes)), {0}, focus=1)
        self.assertIn("line-through", markup)
        self.assertIn("&lt;", markup)
        self.assertNotIn("<b>", markup)
        self.assertIn('<a name="c1"></a>', markup)
        self.assertIn("font-weight:bold", markup)

    def test_rules_are_the_projects_and_numbered_by_us(self):
        self.assertEqual(core.load_rules(self.tmp, ["Standard"]), ["Standard"])
        core.save_rules(self.tmp, ["Füllwörter weg", "Dialekt glätten"])
        self.assertEqual(core.load_rules(self.tmp, ["Standard"]), ["Füllwörter weg", "Dialekt glätten"])
        self.assertEqual(core.parse_rules("1. Füllwörter\n\nR2: Wiederholungen\n3) Dialekt\n2026 als Zahl"),
                         ["Füllwörter", "Wiederholungen", "Dialekt", "2026 als Zahl"])
        prompt = prompts.smoothing(["Füllwörter weg", "Dialekt glätten"])
        self.assertIn("R1: Füllwörter weg\nR2: Dialekt glätten", prompt)
        self.assertIn("<pruefung>", prompt)
        self.assertIn("Zeilenumbrüche bleiben unverändert", prompt)

    def test_parse_edits_reads_the_tagged_array_and_survives_almost_json(self):
        answer = ('<pruefung>Satz 1 [I:] ok. Satz 2: R1 "äh"</pruefung>\n<aenderungen>\n'
                  '[{"regel": "R1", "original": "äh", "ersetzung": "", "begruendung": "Laut"},'
                  ' {"regel": 2, "original": "", "ersetzung": "x"}, "kaputt"]\n</aenderungen>')
        self.assertEqual(core.parse_edits(answer), [{"rule": 1, "original": "äh", "new": "", "reason": "Laut"}])
        self.assertEqual(len(core.parse_edits('```json\n[{"regel": 2, "original": "sehr sehr", "ersetzung": "sehr",}]\n```')), 1)
        self.assertEqual(core.parse_edits("Ich habe nichts gefunden."), [])

    def test_rule_changes_locate_tag_and_guard(self):
        text = "B1: Also äh das war äh halt sehr sehr anstrengend.\nI: Und?"
        rules = ["Füllwörter entfernen", "Wiederholungen entfernen"]
        edits = [{"rule": 1, "original": "äh", "new": "", "reason": "a"},
                 {"rule": 1, "original": "äh halt", "new": "", "reason": "b"},
                 {"rule": 2, "original": "sehr sehr", "new": "sehr", "reason": "c"},
                 {"rule": 9, "original": "anstrengend", "new": "hart", "reason": "d"},
                 {"rule": 2, "original": "sehr", "new": "", "reason": "überschneidet"},
                 {"rule": 1, "original": "anstrengend.\nI:", "new": "anstrengend. I:", "reason": "Zeile"},
                 {"rule": 1, "original": "fehlt", "new": "", "reason": "e"}]
        changes, dropped = core.rule_changes(text, edits, 0, len(text), rules)
        self.assertEqual(dropped, 3)
        self.assertEqual([c["kind"] for c in changes],
                         ["R1 Füllwörter entfernen", "R1 Füllwörter entfernen", "R2 Wiederholungen entfernen", core.NO_RULE])
        result = text
        for c in sorted(changes[:3], key=lambda c: c["pos0"], reverse=True):
            result = result[:c["pos0"]] + c["new"] + result[c["pos1"]:]
        self.assertEqual(result, "B1: Also das war sehr anstrengend.\nI: Und?")
        self.assertEqual(changes[2]["reason"], "c")


YES = QtWidgets.QMessageBox.StandardButton.Yes
SCHEMA = """
create table project (databaseversion text);
create table source (id integer primary key, name text, fulltext text, mediapath text);
create table code_name (cid integer primary key, name text);
create table code_text (ctid integer primary key, cid integer, fid integer, seltext text, pos0 integer,
  pos1 integer, owner text, date text, memo text, avid integer, important integer, unique(cid,fid,pos0,pos1, owner));
create table annotation (anid integer primary key, fid integer, pos0 integer, pos1 integer, memo text);
create table case_text (id integer primary key, caseid integer, fid integer, pos0 integer, pos1 integer);
"""


class QtTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.execute("insert into source (id, name, fulltext) values (1, 'Interview 1.txt', ?)", (TEXT,))
        conn.execute("insert into code_name values (1, 'Belastung'), (2, 'Zeitdruck')")
        for ctid, quote in ((1, "sehr sehr anstrengend"), (2, "Wir hatten Zeitdruck")):
            p0 = TEXT.index(quote)
            conn.execute("insert into code_text (ctid, cid, fid, seltext, pos0, pos1, owner) values (?,1,1,?,?,?,'me')",
                         (ctid, quote, p0, p0 + len(quote)))
        conn.commit()
        self.app = SimpleNamespace(
            conn=conn, project_path=self.tmp,
            settings={"font": "Sans", "fontsize": 10, "docfontsize": 10, "codername": "me", "ai_model_index": 0},
            ai_models=[{"name": "Gemma lokal", "large_model": "gemma-4", "api_base": "http://localhost:8081/v1/"}],
            project_events=None, vectorstore_import_document=lambda *args: None, delete_backup=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def codings(self):
        rows = self.app.conn.execute("select ctid, pos0, pos1, seltext from code_text")
        return {r[0]: (r[1], r[2], r[3]) for r in rows}

    def test_edits_go_through_qualcoders_editor_and_move_the_codings(self):
        revised = "I: Wie war das?\nB1: Das war sehr sehr anstrengend.\nWir hatten Zeitdruck."
        ai_edit.apply_text_changes(self.app, 1, core.diff_changes(TEXT, revised))
        self.assertEqual(self.app.conn.execute("select fulltext from source").fetchone()[0], revised)
        for _ctid, (pos0, pos1, seltext) in self.codings().items():
            self.assertEqual(revised[pos0:pos1], seltext)
        self.assertEqual(self.codings()[2][2], "Wir hatten Zeitdruck")

    def test_a_coding_that_ends_with_the_text_survives_an_edit(self):
        end = len(TEXT)
        self.app.conn.execute("insert into code_text (ctid, cid, fid, seltext, pos0, pos1, owner) "
                              "values (3, 2, 1, ?, ?, ?, 'me')", (TEXT[end - 10:], end - 10, end))
        revised = TEXT.replace("Also äh ", "")
        ai_edit.apply_text_changes(self.app, 1, core.diff_changes(TEXT, revised))
        self.assertEqual(self.codings()[3][2], revised[-10:])

    SMOOTH_ANSWER = (
        "<pruefung>Satz 2: R1 äh, halt; R2 sehr sehr</pruefung><aenderungen>["
        '{"regel": 1, "original": "äh", "ersetzung": "", "begruendung": "Verzögerungslaut"},'
        '{"regel": 1, "original": "halt", "ersetzung": "", "begruendung": "Füllwort"},'
        '{"regel": 2, "original": "sehr sehr", "ersetzung": "sehr", "begruendung": "Wiederholung"},'
        '{"regel": 7, "original": "Zeitdruck", "ersetzung": "Stress", "begruendung": "?"},'
        '{"regel": 1, "original": "gibt es nicht", "ersetzung": ""}]</aenderungen>')

    def jobs(self, dialog):
        return [{"fid": fid, "name": dialog.names[fid], "text": dialog.texts[fid], "start": dialog.scopes[fid][0],
                 "end": dialog.scopes[fid][1], "existing": []} for fid in dialog.texts]

    def test_dialog_smooths_by_rule_applies_backs_up_and_logs(self):
        self.app.ai = SimpleNamespace(invoke_with_logging=lambda messages, context: SimpleNamespace(
            content=self.SMOOTH_ANSWER))
        dialog = ai_edit.AiEditDialog(self.app, 1)
        dialog.show_result(ai_edit.propose(self.app, "smooth", "", self.jobs(dialog), dialog.current_rules()))
        self.assertIn("R1 Füllwörter und", dialog.summary.text())
        self.assertIn("1 Vorschläge des Modells verworfen", dialog.summary.text())
        unchecked = [dialog.list.item(i).text() for i in range(dialog.list.count())
                     if dialog.list.item(i).checkState() == QtCore.Qt.CheckState.Unchecked]
        self.assertEqual(len(unchecked), 1)
        self.assertIn(core.NO_RULE, unchecked[0])
        self.assertIn("vertical-align:super;\">R2<", dialog.diff.toHtml())  # rule badge
        with mock.patch.object(QtWidgets.QMessageBox, "question", return_value=YES):
            dialog.apply()
        smoothed = "I: Wie war das?\nB1: Also das war sehr anstrengend.\nWir hatten Zeitdruck."
        self.assertEqual(self.app.conn.execute("select fulltext from source where id=1").fetchone()[0], smoothed)
        for _ctid, (pos0, pos1, seltext) in self.codings().items():
            self.assertEqual(smoothed[pos0:pos1], seltext)
        folder = Path(self.tmp, "latex_bridge")
        self.assertEqual(len(list((folder / "backups").glob("data_*.qda"))), 1)
        self.assertEqual((folder / "originale" / "1_Interview_1.txt.txt").read_text(), TEXT)
        log = (folder / "ki_protokoll.md").read_text()
        self.assertIn("gemma-4 (http://localhost:8081/v1/)", log)
        self.assertIn("R1: Füllwörter und Verzögerungslaute", log)  # the rules are in the logged prompt
        self.assertIn("# Begründung: Wiederholung", log)
        self.assertFalse(self.app.delete_backup)

    def test_several_files_at_once_with_focus_and_detail(self):
        second = "B2: Ich ich weiß nich.\nI: Danke."
        self.app.conn.execute("insert into source (id, name, fulltext) values (2, 'Interview 2.txt', ?)", (second,))
        self.app.conn.commit()
        answers = {TEXT: self.SMOOTH_ANSWER, second: '<aenderungen>[{"regel": 2, "original": "Ich ich", '
                   '"ersetzung": "Ich", "begruendung": "Stottern"}, {"regel": 4, "original": "nich", '
                   '"ersetzung": "nicht", "begruendung": "Umgangssprache"}]</aenderungen>'}
        self.app.ai = SimpleNamespace(invoke_with_logging=lambda messages, context: SimpleNamespace(
            content=answers[messages[1][1]]))
        dialog = ai_edit.AiEditDialog(self.app)
        for i in range(dialog.files.count()):
            dialog.files.item(i).setCheckState(QtCore.Qt.CheckState.Checked)
        self.assertEqual(sorted(dialog.texts), [1, 2])
        dialog.show_result(ai_edit.propose(self.app, "smooth", "", self.jobs(dialog), dialog.current_rules()))
        self.assertIn("2 Dateien", dialog.summary.text())
        last = dialog.list.count() - 1
        self.assertIn("Interview 2.txt", dialog.list.item(last).text())
        dialog.focus_change(last)
        self.assertEqual(dialog.diff_title.text(), "Diff: Interview 2.txt")
        self.assertIn("font-weight:700; background-color:#69f0ae;\">nicht<", dialog.diff.toHtml())  # focused
        self.assertIn("Begründung: Umgangssprache", dialog.detail.text())
        self.assertIn("Regel: Dialekt und Umgangssprache", dialog.detail.text())
        with mock.patch.object(QtWidgets.QMessageBox, "question", return_value=YES):
            dialog.apply()
        texts = dict(self.app.conn.execute("select id, fulltext from source"))
        self.assertEqual(texts[2], "B2: Ich weiß nicht.\nI: Danke.")
        self.assertIn("B1: Also das war sehr anstrengend.", texts[1])
        log = Path(self.tmp, "latex_bridge", "ki_protokoll.md").read_text()
        self.assertEqual(log.count("\n## "), 2)

    def test_unsaved_changes_elsewhere_refuse_instead_of_hanging(self):
        self.app.ai = SimpleNamespace(invoke_with_logging=lambda messages, context: SimpleNamespace(
            content=self.SMOOTH_ANSWER))
        dialog = ai_edit.AiEditDialog(self.app, 1)
        dialog.show_result(ai_edit.propose(self.app, "smooth", "", self.jobs(dialog), dialog.current_rules()))
        self.app.conn.execute("update code_name set name='offen' where cid=1")  # another window, not committed
        with mock.patch.object(QtWidgets.QMessageBox, "question", return_value=YES), \
                mock.patch.object(QtWidgets.QMessageBox, "warning") as warning:
            dialog.apply()
        self.assertIn("nicht gespeicherte Änderungen", warning.call_args[0][2])
        self.assertEqual(self.app.conn.execute("select fulltext from source where id=1").fetchone()[0], TEXT)
        self.app.conn.rollback()

    def test_coding_mode_inserts_after_confirmation(self):
        self.app.ai = SimpleNamespace(invoke_with_logging=lambda messages, context: SimpleNamespace(
            content='["Wir hatten Zeitdruck", "Wie war das?"]'))
        dialog = ai_edit.AiEditDialog(self.app, 1)
        dialog.mode.setCurrentIndex(dialog.mode.findData("code"))
        dialog.code.setCurrentIndex(dialog.code.findData(2))
        dialog.instruction.setText("Zeitdruck")
        dialog.show_result(ai_edit.propose(self.app, "code", "Zeitdruck", self.jobs(dialog), []))
        rejected = dialog.list.findItems("Wie war das?", QtCore.Qt.MatchFlag.MatchContains)[0]
        rejected.setCheckState(QtCore.Qt.CheckState.Unchecked)
        with mock.patch.object(QtWidgets.QMessageBox, "question", return_value=YES):
            dialog.apply()
        rows = self.app.conn.execute("select seltext, memo from code_text where cid=2").fetchall()
        self.assertEqual(rows, [("Wir hatten Zeitdruck", "KI-Vorschlag (Zeitdruck)")])

    def test_remote_model_is_refused(self):
        self.app.ai_models[0]["api_base"] = "https://api.openai.com/v1"
        dialog = ai_edit.AiEditDialog(self.app, 1)
        with mock.patch.object(QtWidgets.QMessageBox, "warning") as warning:
            dialog.request()
        warning.assert_called_once()

    def test_main_window_heartbeat_menu_and_status(self):
        window = QtWidgets.QMainWindow()
        window.ui = SimpleNamespace(menubar=window.menuBar())
        window.app = SimpleNamespace(project_path=self.tmp, version="3.9")
        with mock.patch.object(core, "EDITOR_URL", "http://127.0.0.1:9/qualcoder/health"):
            latex_bridge.install_main(window)
            QtCore.QThreadPool.globalInstance().waitForDone(3000)
            QtWidgets.QApplication.processEvents()
        self.assertTrue(Path(self.tmp, "latex_bridge", "heartbeat.json").exists())
        self.assertIn("LaTeX", [a.text() for a in window.menuBar().actions()])
        label = window.statusBar().findChild(QtWidgets.QLabel)
        self.assertEqual(label.text(), "LaTeX-Editor: nicht erreichbar")
        window.latex_bridge_timer.stop()

    def fake_code_text_dialog(self):
        edit = QtWidgets.QPlainTextEdit()
        edit.setPlainText(TEXT)
        p0 = TEXT.index("Wir hatten")
        return SimpleNamespace(ui=SimpleNamespace(plainTextEdit=edit), app=self.app,
                               file_={"id": 1, "name": "Interview 1.txt", "start": 0},
                               code_text=[{"ctid": 9, "cid": 2, "pos0": p0, "pos1": len(TEXT)}]), p0

    def test_drag_data_and_reference_menu(self):
        dialog, p0 = self.fake_code_text_dialog()
        latex_bridge.install_code_text(dialog)
        cursor = dialog.ui.plainTextEdit.textCursor()
        cursor.setPosition(p0)
        cursor.setPosition(p0 + 10, cursor.MoveMode.KeepAnchor)
        dialog.ui.plainTextEdit.setTextCursor(cursor)
        # copy() asks the (patched) virtual from C++, exactly like a drag does.
        dialog.ui.plainTextEdit.copy()
        gc.collect()  # the mime data must belong to the clipboard now, not to Python
        mime = QtWidgets.QApplication.clipboard().mimeData()
        self.assertEqual(json.loads(bytes(mime.data(core.MIME)).decode())["ctid"], 9)
        self.assertIn("data-qcref", mime.html())
        self.assertEqual(mime.text(), "Wir hatten")

        menu = QtWidgets.QMenu()
        latex_bridge.add_text_menu_actions(dialog, menu, dialog.ui.plainTextEdit.cursorRect().center())
        labels = [a.text() for a in menu.actions()]
        self.assertTrue(any(r"\iref{9}" in label for label in labels), labels)
        menu.actions()[0].trigger()
        self.assertEqual(QtWidgets.QApplication.clipboard().text(), r"\iref{9}")


if __name__ == "__main__":
    unittest.main()
