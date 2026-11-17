"""
This file is part of QualCoder (fork: LaTeX bridge, see docs/latex_bridge.md).

QualCoder is free software: you can redistribute it and/or modify it under the
terms of the GNU Lesser General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later version.

The prompts of the AI edit with diff review. They are written into the AI log with every accepted
change, so the smoothing rules can be cited in the thesis. The smoothing rules themselves are the
project's (latex_bridge/glaettungsregeln.txt); DEFAULT_RULES only seeds a new project.
"""

# Default smoothing rules for a new project; the user edits them in the dialog, one per line.
DEFAULT_RULES = [
    "Füllwörter und Verzögerungslaute entfernen (z. B. äh, ähm, hm, halt, sozusagen, quasi), wenn sie "
    "nichts zur Aussage beitragen.",
    "Wortwiederholungen und Stottern entfernen (z. B. „sehr sehr“ → „sehr“, „ich ich“ → „ich“).",
    "Abgebrochene Satzanfänge entfernen, die sofort neu begonnen oder korrigiert werden.",
    "Dialekt und Umgangssprache behutsam ins Standarddeutsche übertragen (z. B. „nich“ → „nicht“, "
    "„hab“ → „habe“), ohne Wortwahl oder Stil der Person zu ersetzen.",
]

SMOOTHING = """Du glättest einen Ausschnitt eines Interview-Transkripts für eine wissenschaftliche Arbeit.

Glättungsregeln (nur diese anwenden):
{rules}

Grenzen, die immer gelten und Vorrang vor jeder Regel haben:
- Die Bedeutung bleibt gleich: nichts hinzufügen, nichts Inhaltliches weglassen, nichts zusammenfassen.
- Sprecherkennungen (z. B. "I:", "B1:") und Zeilenumbrüche bleiben unverändert.
- Ändere nur, was eine der Regeln verlangt. Im Zweifel nicht ändern.

Vorgehen:
1. Gehe den Text Satz für Satz durch und prüfe jeden Satz gegen jede Regel. Schreibe diese Prüfung
   knapp zwischen <pruefung> und </pruefung>.
2. Kontrolliere jede geplante Änderung: Verlangt eine Regel sie? Bleibt die Bedeutung gleich? Steht
   der Originalausschnitt wörtlich so im Text?
3. Gib danach alle Änderungen in Textreihenfolge zwischen <aenderungen> und </aenderungen> als
   JSON-Array aus, z. B.:
   [{{"regel": 1, "original": "äh also", "ersetzung": "also", "begruendung": "Verzögerungslaut"}}]
   - "regel": Nummer der angewendeten Regel.
   - "original": wörtlicher, zusammenhängender Ausschnitt aus dem Text mit Satzzeichen, so kurz wie
     möglich und lang genug, dass die Stelle eindeutig ist.
   - "ersetzung": der neue Text; leer ("") zum Entfernen.
   - Jede Stelle nur einmal; Änderungen dürfen sich nicht überschneiden.
   Ist nichts zu ändern: <aenderungen>[]</aenderungen>"""


def smoothing(rules: list[str]) -> str:
    """The smoothing prompt with the rules numbered R1, R2, … (the numbers the answer refers to)."""
    return SMOOTHING.format(rules="\n".join(f"R{i}: {rule}" for i, rule in enumerate(rules, 1)))


SUMMARIZE = """Fasse den folgenden Ausschnitt eines Interview-Transkripts knapp und sachlich zusammen.
Behalte die Sprecherkennung des Abschnitts am Anfang bei. Gib ausschließlich die Zusammenfassung
zurück, ohne Einleitung und ohne Kommentar."""

EDIT = """Du bearbeitest einen Ausschnitt eines Interview-Transkripts nach der folgenden Anweisung.
Sprecherkennungen und Zeilenumbrüche bleiben erhalten, sofern die Anweisung nichts anderes verlangt.
Gib ausschließlich den bearbeiteten Text zurück, ohne Einleitung und ohne Kommentar.

Anweisung: {instruction}"""

CODING = """Du hilfst beim qualitativen Codieren eines Interview-Transkripts. Finde alle Stellen im
Text, auf die diese Beschreibung zutrifft: {instruction}
Gib ausschließlich ein JSON-Array mit wörtlichen Zitaten zurück, z. B. ["Zitat eins", "Zitat zwei"].
Jedes Zitat muss genau so zusammenhängend im Text stehen (ganze Sätze oder Satzteile). Keine
Treffer: []."""
