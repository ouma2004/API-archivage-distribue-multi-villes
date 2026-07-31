# -*- coding: utf-8 -*-
"""
form_server.py — Formulaire de choix des pistes (comme e2e_v2)
===============================================================

Sert le RESUME genere en passe 1 (contrats/<api>_resume.md) dans une page web,
avec la liste des PISTES en cases a cocher. L'humain :
  - lit le resume + repond aux questions,
  - COCHE les pistes a tester (1, plusieurs, ou toutes),
  - regle le max de scenarios et l'option prioritaire,
puis valide. Le serveur ecrit contrats/<api>_reponses.md avec, notamment, la
section "=== PISTES CHOISIES ===" que e2e_api.py lit en passe 2.

Expose en public via ngrok (comme dans le pipeline Odoo) pour choisir depuis
n'importe ou. En local, http://localhost:8500 suffit.

Usage :
  python form_server.py
  # puis, dans un autre terminal :  ngrok http 8500   (optionnel)
"""

import os
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
API_NAME = os.environ.get("API_NAME", "doc-archiver")
CONTRATS_DIR = os.path.join(ROOT, "contrats")
PORT = int(os.environ.get("FORM_PORT", "8500"))

RESUME_PATH = os.path.join(CONTRATS_DIR, f"{API_NAME}_resume.md")
REPONSES_PATH = os.path.join(CONTRATS_DIR, f"{API_NAME}_reponses.md")


def parse_pistes(resume_text):
    """Extrait les lignes 'id|nom|depends_on' de la section === PISTES ===."""
    pistes = []
    if "=== PISTES ===" not in resume_text:
        return pistes
    bloc = resume_text.split("=== PISTES ===", 1)[1]
    for ligne in bloc.splitlines():
        l = ligne.strip()
        if not l or l.startswith("(") or l.startswith("==="):
            continue
        parts = l.split("|")
        if len(parts) >= 2:
            pid = parts[0].strip()
            nom = parts[1].strip()
            deps = parts[2].strip() if len(parts) >= 3 else ""
            if pid and re.match(r"^[a-z0-9_]+$", pid):
                pistes.append({"id": pid, "nom": nom, "deps": deps})
    return pistes


def parse_questions(resume_text):
    """Extrait les questions Qn. pour les afficher avec un champ reponse."""
    questions = []
    if "=== MES QUESTIONS" not in resume_text:
        return questions
    bloc = resume_text.split("=== MES QUESTIONS", 1)[1]
    bloc = bloc.split("=== ", 1)[0]
    for m in re.finditer(r"(Q\d+\.\s*.+)", bloc):
        questions.append(m.group(1).strip())
    return questions


HTML = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>E2E API — {api} — Choix des pistes</title>
<style>
  body{{font-family:system-ui,Arial;max-width:820px;margin:24px auto;padding:0 16px;color:#1a1a1a}}
  h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px}}
  pre{{background:#f5f5f5;padding:14px;border-radius:8px;white-space:pre-wrap;font-size:13px}}
  .piste{{border:1px solid #ddd;border-radius:8px;padding:10px 12px;margin:8px 0;display:flex;gap:10px;align-items:flex-start}}
  .piste input{{margin-top:4px}}
  .piste .dep{{color:#888;font-size:12px}}
  .q{{margin:10px 0}} .q textarea{{width:100%;min-height:44px}}
  .row{{display:flex;gap:16px;align-items:center;margin:14px 0;flex-wrap:wrap}}
  button{{background:#111;color:#fff;border:0;border-radius:8px;padding:12px 20px;font-size:15px;cursor:pointer}}
  label.inline{{display:flex;gap:6px;align-items:center}}
  .hint{{color:#666;font-size:13px}}
</style></head>
<body>
<h1>🧪 E2E API — {api}</h1>
<p class="hint">Coche les pistes a tester, reponds aux questions, regle le plafond,
puis valide. Tes choix seront ecrits dans <code>contrats/{api}_reponses.md</code>
et lus par la passe 2.</p>

<h2>Resume genere par l'agent</h2>
<pre>{resume}</pre>

<form method="POST" action="/submit">
  <h2>Pistes a tester</h2>
  {pistes_html}

  <h2>Questions de l'agent</h2>
  {questions_html}

  <h2>Zone libre (corrections / precisions)</h2>
  <textarea name="zone_libre" style="width:100%;min-height:70px" placeholder="Ajoute ce que tu veux..."></textarea>

  <div class="row">
    <label class="inline">Max scenarios : <input type="number" name="max_scenarios" value="0" min="0" style="width:90px"></label>
    <label class="inline"><input type="checkbox" name="priority_only" value="1"> Prioritaires seulement</label>
  </div>

  <button type="submit">✅ Valider mes choix</button>
</form>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _resume(self):
        if not os.path.exists(RESUME_PATH):
            return None
        with open(RESUME_PATH, encoding="utf-8") as f:
            return f.read()

    def do_GET(self):
        resume = self._resume()
        if resume is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<h1>Resume introuvable</h1><p>Lance d'abord : "
                             f"<code>python e2e_api.py --analyze</code></p>".encode("utf-8"))
            return

        pistes = parse_pistes(resume)
        questions = parse_questions(resume)

        pistes_html = ""
        if pistes:
            for p in pistes:
                dep = f'<span class="dep">depend de : {p["deps"]}</span>' if p["deps"] else ""
                pistes_html += (
                    f'<div class="piste"><input type="checkbox" name="piste" value="{p["id"]}" id="{p["id"]}" checked>'
                    f'<label for="{p["id"]}"><b>{p["id"]}</b> — {p["nom"]}<br>{dep}</label></div>'
                )
        else:
            pistes_html = '<p class="hint">Aucune piste detectee dans le resume.</p>'

        questions_html = ""
        for i, q in enumerate(questions):
            questions_html += (
                f'<div class="q"><div>{q}</div>'
                f'<textarea name="rep_{i}"></textarea></div>'
            )
        if not questions:
            questions_html = '<p class="hint">Aucune question.</p>'

        page = HTML.format(api=API_NAME, resume=resume,
                           pistes_html=pistes_html, questions_html=questions_html)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        data = parse_qs(raw)

        pistes = data.get("piste", [])
        max_scenarios = (data.get("max_scenarios", ["0"])[0] or "0").strip()
        priority = "1" if data.get("priority_only") else "0"
        zone_libre = data.get("zone_libre", [""])[0].strip()

        resume = self._resume() or ""
        questions = parse_questions(resume)

        lignes = [f"=== REPONSES — {API_NAME} ===", ""]
        if questions:
            lignes.append("=== REPONSES AUX QUESTIONS ===")
            for i, q in enumerate(questions):
                rep = data.get(f"rep_{i}", [""])[0].strip()
                lignes.append(q)
                lignes.append(f"   -> {rep}")
            lignes.append("")
        if zone_libre:
            lignes.append("=== ZONE LIBRE ===")
            lignes.append(zone_libre)
            lignes.append("")
        lignes.append(f"=== PARAMETRES ===")
        lignes.append(f"max_scenarios={max_scenarios}")
        lignes.append(f"priority_only={priority}")
        lignes.append("")
        lignes.append("=== PISTES CHOISIES ===")
        for p in pistes:
            lignes.append(p)

        os.makedirs(CONTRATS_DIR, exist_ok=True)
        with open(REPONSES_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lignes) + "\n")

        cmd_pistes = ",".join(pistes) if pistes else "(aucune)"
        extra = ""
        if max_scenarios and max_scenarios != "0":
            extra += f" --max-scenarios={max_scenarios}"
        if priority == "1":
            extra += " --priority-only"

        html = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<style>body{{font-family:system-ui;max-width:720px;margin:40px auto;padding:0 16px}}
code{{background:#f5f5f5;padding:2px 6px;border-radius:4px}}
pre{{background:#111;color:#fff;padding:14px;border-radius:8px;white-space:pre-wrap}}</style></head>
<body><h1>✅ Choix enregistres</h1>
<p>Ecrit dans <code>contrats/{API_NAME}_reponses.md</code>.</p>
<p>Pistes choisies : <b>{cmd_pistes}</b></p>
<p>Prochaines commandes :</p>
<pre>python e2e_api.py --generate-scenarios --pistes={cmd_pistes}{extra}
python e2e_api.py --run --pistes={cmd_pistes}{extra}</pre>
<p>(En CI, ces etapes s'enchainent automatiquement.)</p>
</body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, *a):
        pass


def main():
    print(f"[form] Formulaire de pistes pour '{API_NAME}'")
    print(f"[form] Resume attendu : {RESUME_PATH}")
    print(f"[form] Ouvre : http://localhost:{PORT}")
    print(f"[form] (public : lance 'ngrok http {PORT}' dans un autre terminal)")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
