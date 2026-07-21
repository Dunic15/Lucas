# Test MuseTalk su Clariden (il cluster di Helen): gratis

Obiettivo: generare la clip **foto + voce → Laura che parla** sui GH200 di Clariden
(costo zero), giudicare la qualità, e SOLO se convince fare il deploy live su Runpod
(setup x86 già pronto in `gpu/musetalk_runpod_setup.sh`).

## Cosa serve PRIMA (una volta sola: la parte "umana")
1. **Helen ti aggiunge al progetto CSCS** → ti serve: username CSCS + nome account
   del progetto (es. `infra01`) da usare in `sbatch --account=...`.
2. Sul TUO Mac, setup accesso (dal repo di Helen):
   ```bash
   curl -sL https://raw.githubusercontent.com/swiss-ai/reasoning_getting-started/main/{cscs-cl_setup.sh,user.env} -OO
   # segui il setup; poi:
   cscs-cl    # genera le chiavi (valgono 24h; rilanciarlo quando scadono)
   ```
   Da lì in poi esiste `ssh clariden`.

## Passi del test (copiaincolla)
```bash
# 1) porta su Clariden: input (foto+voce dal bundle sul Desktop) + script
ssh clariden 'mkdir -p /iopsstor/scratch/cscs/$USER/musetalk ~/.edf'
scp ~/Desktop/laura-runpod-test/laura_face.jpg \
    ~/Desktop/laura-runpod-test/laura_voice.wav \
    ~/Desktop/Laura/gpu/clariden-test/setup_and_run_clariden.sh \
    clariden:/iopsstor/scratch/cscs/$USER/musetalk/
scp ~/Desktop/Laura/gpu/clariden-test/musetalk.toml clariden:~/.edf/
scp ~/Desktop/Laura/gpu/clariden-test/musetalk_test.sbatch clariden:~/

# 2) lancia il job (sostituisci l'account!)
ssh clariden 'sbatch --account=<ACCOUNT> ~/musetalk_test.sbatch'

# 3) segui il log (il numero job lo stampa sbatch)
ssh clariden 'tail -f /iopsstor/scratch/cscs/$USER/musetalk/job_<JOBID>.out'

# 4) scarica la clip e guardala
scp 'clariden:/iopsstor/scratch/cscs/$USER/musetalk/results/laura_720p.mp4' ~/Desktop/
open ~/Desktop/laura_720p.mp4
```

## Aspettative oneste (ARM)
- I GH200 sono **aarch64**: `mmcv` si compila da sorgente dentro il job (~10-25 min).
  È il punto che può rompersi (version mismatch con il torch dell'immagine NGC).
  Se fallisce: incollare l'errore in chat; di solito si risolve cambiando pin di
  mmcv o immagine NGC. Se combatte troppo → piano B Runpod ($0.19/h, ~$0.15 totali).
- Scratch (`/iopsstor`) si **auto-pulisce a 30 giorni**: la clip va scaricata, i pesi
  eventualmente ri-scaricati in run futuri lontani.
- Primo run ~40-80 min (download+compile+inferenza). Run successivi: minuti.

## Poi (se la qualità convince)
Deploy live: `gpu/musetalk_runpod_setup.sh` su un pod Runpod x86 (A4500 $0.19/h),
`gpu/server.py` + `AVATAR_PAGE=photoreal` + `GPU_STREAM_URL`: vedi gpu/README.md.
