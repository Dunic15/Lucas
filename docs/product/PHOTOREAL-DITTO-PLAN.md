# Piano: Laura photoreal (Ditto) che parla live nei meeting

_Da altra sessione, 2026-07-14. Stato: tutto fermo (pod spento). Partenza on-demand: dire "Fase 1"._

## Obiettivo
Volto photoreal di Laura che parla da sola nei meeting (Zoom/Meet/Teams), **zero-touch**
(GPU si accende/spegne da sola), alla massima qualità possibile del contesto.

## Diagnosi (perché oggi non funzionava)
- L'architettura c'è già: `face: photoreal` → il bot Recall apre `/photoreal` → **Ditto** anima il
  ritratto con lip-sync → il backend guida la voce (TTS ElevenLabs ✅).
- Immagini Ditto in ghcr: v1–v5. **v3 = verificata buona** (photoreal + emozioni); **v5 = quella sul
  pod, ROTTA** (espone solo `/health`, manca `/stream` → niente frame video).
- Risultato: il pod si accende ma serve solo il ritratto statico (faccia ferma). Voce e GPU ok, video no.
- Contorno: **#193** (bottone web "Talk" → photoreal) è draft non deployata; l'auto-wake della GPU non
  è scattato; audio browser da sbloccare sulla pagina manuale.

## Cosa manca (checklist)
1. **Immagine buona sul pod**: `podEditJob e7jwxnvi5fcf85 → v3` (stesso pod ID, nessun cambio env)
   oppure rebuild **v6** dal `server.py` attuale (che il `/stream` ce l'ha).
2. **Verifica `/stream` reale** (handshake WS + frame che scorrono, non solo `/health`).
3. **Auto-wake Runpod** su sessione `face: photoreal` (confermare/fixare `runpod_runtime` → zero-touch).
4. **Test e2e** in un meeting vero (invito → pod sveglio → photoreal + lip-sync + parla).
5. **Tuning qualità 720p**: inquadratura stretta, luce, `JPEG_QUALITY 82→92`, `DITTO_EMO_INTENSITY` (#130).
6. **Deploy #193** → web call photoreal full-res.

## Fasi
**Fase 1: Sblocco (½ giornata, il vero fix)**
- Ripristina immagine buona (v3) sul pod + verifica `/health` e `/stream`.
- E2e in Meet di test → conferma photoreal-che-parla a 720p.
- Unico costo tempo: pull immagine ~15–35 min (una tantum); poi wake ~90s.

**Fase 2: Zero-touch + qualità (1–2 giorni)**
- Fixa l'auto-wake (niente accensione manuale ogni volta).
- Tuning → "ottimo 720p Ditto".
- Deploy #193.

**Fase 3: Flagship ultra-HD (progetto a parte, dopo)**
- Call WebRTC "chiama Laura" off-meeting → Ditto full-res/4K. È l'unico posto oltre i 720p.

## Verità sulla qualità (da tenere a mente)
- **Dentro un meeting**: cap **720p/15fps** imposto da Zoom/Meet/Teams (non da Recall) → il massimo
  realistico è "ottimo 720p".
- **Fuori dal meeting** (web call tua): nessun cap → **ultra-HD vero**.

## Rischi / note
- Costo GPU: on-demand solo durante il meeting (auto-stop a fine sessione).
- Coordinare con eventuale deploy App Runner (se cambi pod ID va aggiornato `GPU_STREAM_URL`; restando
  su `e7jwxnvi5fcf85` no).
- v5 rotta: capire se rigenerarla o abbandonarla (meglio v3 o una v6 pulita).

> Partenza: dire **"Fase 1"** e la sessione infra la esegue. Per ora tutto fermo; pod spento.
