"""
This file is part of QualCoder (fork: LaTeX bridge, see docs/latex_bridge.md).

QualCoder is free software: you can redistribute it and/or modify it under the
terms of the GNU Lesser General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later version.

The fixed prompts of the AI edit with diff review. They are written into the AI log with every
accepted change, so the smoothing rules can be cited in the thesis. Change them here, not ad hoc.
"""

SMOOTHING = """Du glättest ein Interview-Transkript für eine wissenschaftliche Arbeit. Regeln:
1. Entferne Füllwörter und Verzögerungslaute (z. B. äh, ähm, hm, halt, sozusagen, quasi), wenn sie
   nichts zur Aussage beitragen.
2. Entferne Wortwiederholungen und abgebrochene Satzanfänge, die sofort korrigiert werden.
3. Übertrage Dialekt behutsam ins Standarddeutsche, ohne Wortwahl oder Stil der Person zu ersetzen.
4. Die Bedeutung darf sich nicht ändern: nichts hinzufügen, nichts weglassen, was Inhalt trägt,
   nichts zusammenfassen, nicht umformulieren.
5. Sprecherkennungen (z. B. "I:", "B1:"), Zeilenumbrüche und Reihenfolge exakt beibehalten.
Gib ausschließlich den geglätteten Text zurück, ohne Einleitung und ohne Kommentar."""

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
