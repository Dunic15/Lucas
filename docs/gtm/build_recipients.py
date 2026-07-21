#!/usr/bin/env python3
"""Genera docs/gtm/recipients.csv (email,subject,body) dai due xlsx di lead.

Corpo personalizzato per azienda + pain, con link Calendly e riga "CV in allegato"
(l'allegato lo mette send_outreach.py al momento dell'invio). SaaS in inglese,
HR-Italia in italiano.

    python docs/gtm/build_recipients.py
"""
from __future__ import annotations

import csv
import glob
import zipfile
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
CALENDLY = "https://calendly.com/ducciprofeti/30min"
SITE = "https://lauravatar.com/"


def read_xlsx(path: str) -> list[dict]:
    z = zipfile.ZipFile(path)
    ss: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{NS}si"):
            ss.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))

    def col_letters(ref: str) -> str:
        return "".join(c for c in ref if c.isalpha())

    def col_idx(letters: str) -> int:
        n = 0
        for c in letters:
            n = n * 26 + (ord(c.upper()) - 64)
        return n - 1

    def val(c) -> str:
        t = c.get("t")
        if t == "inlineStr":
            return "".join(x.text or "" for x in c.iter(f"{NS}t"))
        v = c.find(f"{NS}v")
        if v is None:
            return ""
        return ss[int(v.text)] if t == "s" else (v.text or "")

    rows: list[list[str]] = []
    for r in sheet.findall(f".//{NS}row"):
        d: dict[int, str] = {}
        for c in r.findall(f"{NS}c"):
            d[col_idx(col_letters(c.get("r", "A1")))] = val(c)
        if d:
            rows.append([d.get(i, "") for i in range(max(d) + 1)])
    header = rows[0]
    return [
        rec for rec in (dict(zip(header, r)) for r in rows[1:])
        if "@" in rec.get("Work email", "")
    ]


def first_name(full: str) -> str:
    return full.split()[0] if full else "there"


def saas_body(r: dict) -> str:
    company = r["Company name"]
    pain = r.get("Relevant pain point", "").strip().rstrip(".")
    return f"""Hi {first_name(r.get('Best contact person', ''))},

I'm Duccio, an MSc student at Politecnico di Milano (previously on Microsoft's AI pre-sales team). I'm building an AI teammate for meetings and think {company} would be a great first pilot.

It joins your calls on Zoom/Meet/Teams, answers questions live from your own docs, and afterwards writes up the decisions, action items and owners. The fit for {company}. {pain}.

I'm taking on a few companies as free, hands-on first testers. You can see it here: {SITE}; if it's useful, grab 30 min ({CALENDLY}) or just reply. I've attached my CV for context.

Thanks,
Duccio Profeti
MSc, Politecnico di Milano · Duccio@sffstudio.com"""


def hr_body(r: dict) -> str:
    company = r["Company name"]
    pain = r.get("Relevant pain point", "").strip().rstrip(".")
    return f"""Ciao {first_name(r.get('Best contact person', ''))},

sono Duccio, studente magistrale al Politecnico di Milano (in passato nel team AI pre-sales di Microsoft). Sto costruendo un collega AI per le riunioni e penso che {company} sarebbe un ottimo primo pilot.

Si unisce alle vostre call su Zoom/Meet/Teams, risponde in diretta dai vostri documenti, e a fine riunione produce il verbale con decisioni, prossimi passi e responsabili. Il punto per {company}. {pain}.

Sto prendendo poche aziende come primi tester gratuiti. Puoi vederla qui: {SITE}; se ti va prenota 30 min ({CALENDLY}) o rispondi a questa mail. In allegato il mio CV.

Grazie,
Duccio Profeti
MSc, Politecnico di Milano · Duccio@sffstudio.com"""


def main() -> None:
    out: list[dict] = []
    for f in sorted(glob.glob("docs/gtm/niche1-saas-*.xlsx")):
        for r in read_xlsx(f):
            out.append(dict(email=r["Work email"],
                            subject=f"a free pilot idea for {r['Company name']}",
                            body=saas_body(r)))
    for f in sorted(glob.glob("docs/gtm/niche4-hr-*.xlsx")):
        for r in read_xlsx(f):
            out.append(dict(email=r["Work email"],
                            subject=f"un'idea di pilot per {r['Company name']}",
                            body=hr_body(r)))
    with open("docs/gtm/recipients.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["email", "subject", "body"])
        w.writeheader()
        w.writerows(out)
    print(f"scritte {len(out)} righe in docs/gtm/recipients.csv")


if __name__ == "__main__":
    main()
