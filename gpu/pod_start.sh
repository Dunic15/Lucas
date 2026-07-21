#!/bin/bash
# Entrypoint del pod Runpod per l'immagine laura-ditto.
# 1) sshd con la PUBLIC_KEY che Runpod passa in env (le immagini custom
#    devono gestirsela da sole; quelle ufficiali lo fanno nel loro start.sh)
# 2) poi il server della faccia, come PID 1 effettivo del servizio.
mkdir -p ~/.ssh && chmod 700 ~/.ssh
if [ -n "$PUBLIC_KEY" ]; then
  echo "$PUBLIC_KEY" >> ~/.ssh/authorized_keys
  chmod 600 ~/.ssh/authorized_keys
fi
mkdir -p /run/sshd
/usr/sbin/sshd || true
exec python3 /app/server.py
