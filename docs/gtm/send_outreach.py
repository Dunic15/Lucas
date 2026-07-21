#!/usr/bin/env python3
"""
Invio outreach personalizzato con CV allegato, dal TUO account Gmail.

PERCHE' ESISTE: il connettore Gmail di Claude crea solo BOZZE e non allega file.
Questo script fa cio' che al connettore manca; invia davvero e allega il CV -
usando la tua Gmail (o l'account @sffstudio.com) via SMTP. Lo esegui TU, con una
tua "app password": le credenziali restano sul tuo computer, non passano da me.

USO
  1. App Password Google (serve la 2FA attiva):
        https://myaccount.google.com/apppasswords
     Copia i 16 caratteri.
  2. Esporta le credenziali (NON scriverle nel file):
        export GMAIL_USER="Duccio@sffstudio.com"        # l'indirizzo mittente
        export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
        export CV_PATH="/percorso/al/CV_DUCCIO_PROFETI.pdf"
  3. Prepara recipients.csv con intestazione:  email,subject,body
     (te lo genero io dalle 32 bozze / dagli xlsx: chiedimelo)
  4. Prova SENZA inviare:   python docs/gtm/send_outreach.py
  5. Invia davvero:         python docs/gtm/send_outreach.py --send

SICUREZZA / DELIVERABILITY
  - DRY-RUN di default: senza --send non parte NULLA.
  - Cap giornaliero + pausa tra invii: Gmail limita il cold-sending; oltre
    ~20-30 cold email/giorno da un account "freddo" finisci in spam o vieni
    frenato. Manda a ondate; per volumi seri usa un dominio secondario + tool
    dedicato (Instantly/Smartlead) con warm-up.
  - Consenso/GDPR e' responsabilita' tua: B2B legittimo interesse + opt-out.
"""
from __future__ import annotations

import argparse
import csv
import os
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path

CV_PATH = Path(os.getenv("CV_PATH", "/Users/duccioo/Desktop/CV/CV_DUCCIO PROFETI.S.pdf"))
RECIPIENTS = Path(os.getenv("RECIPIENTS", "docs/gtm/recipients.csv"))
DAILY_CAP = int(os.getenv("DAILY_CAP", "20"))       # non superare in un giorno
SLEEP_SECONDS = float(os.getenv("SLEEP_SECONDS", "45"))  # pausa tra un invio e l'altro


def main() -> None:
    ap = argparse.ArgumentParser(description="Invia l'outreach con CV allegato.")
    ap.add_argument("--send", action="store_true",
                    help="invia davvero (senza questo flag e' una prova a vuoto)")
    args = ap.parse_args()

    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if args.send and (not user or not pw):
        sys.exit("Imposta GMAIL_USER e GMAIL_APP_PASSWORD prima di --send.")

    if not RECIPIENTS.exists():
        sys.exit(f"Manca {RECIPIENTS} (colonne: email,subject,body).")
    rows = [r for r in csv.DictReader(RECIPIENTS.open(encoding="utf-8")) if r.get("email")]
    if not rows:
        sys.exit(f"Nessun destinatario in {RECIPIENTS}.")

    cv_bytes = CV_PATH.read_bytes() if CV_PATH.exists() else None
    if cv_bytes is None:
        print(f"[attenzione] CV non trovato in {CV_PATH} — invio SENZA allegato.")
    else:
        print(f"[CV] verra' allegato: {CV_PATH.name} ({len(cv_bytes) // 1024} KB)")

    sent = 0
    for row in rows:
        if sent >= DAILY_CAP:
            print(f"[stop] raggiunto il cap giornaliero ({DAILY_CAP}). Riprendi domani.")
            break

        msg = EmailMessage()
        msg["From"] = user or "you@example.com"
        msg["To"] = row["email"]
        msg["Subject"] = row.get("subject", "")
        msg.set_content(row.get("body", ""))
        if cv_bytes is not None:
            msg.add_attachment(cv_bytes, maintype="application",
                               subtype="pdf", filename=CV_PATH.name)

        if not args.send:
            print(f"[prova] -> {row['email']} | {msg['Subject']}")
            continue

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pw)
            s.send_message(msg)
        sent += 1
        print(f"[inviata {sent}/{DAILY_CAP}] -> {row['email']}")
        time.sleep(SLEEP_SECONDS)

    if not args.send:
        print(f"\nPROVA a vuoto: {len(rows)} email pronte. Aggiungi --send per inviare.")
    else:
        print(f"\nFatto: {sent} inviate.")


if __name__ == "__main__":
    main()
