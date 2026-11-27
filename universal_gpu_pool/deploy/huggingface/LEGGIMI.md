# Copia personale gratuita su Hugging Face Spaces (senza carta)

Così il programma gira su un server di Hugging Face: dal telefono funziona ovunque e il PC può restare spento.

## Per chi lo usa (3 minuti)
Dal README di GitHub tocca il pulsante **«Duplicate this Space»** e segui i 5 passi: non servono segreti né password.
La copia **si attiva da sola** la prima volta che la apri: il tuo dispositivo genera un codice casuale e lo usa come chiave di accesso
(lo vedi, se vuoi, in «📱 Telefono» come link personale). Poi collega Kaggle dall'app.

- Il server gratuito **dimentica tutto quando si riavvia o va in pausa**: il tuo telefono ricorda token Kaggle e chiavi (solo nel browser)
  e le rimette da solo alla prima apertura. Scarica comunque i risultati appena sono pronti: i file sul server non sono permanenti.
- Se qualcuno aprisse l'indirizzo **prima di te** dopo un riavvio, attiverebbe lui l'istanza: il tuo telefono non entrerebbe
  e **non gli manderebbe nessun segreto**. In quel caso: Settings → *Restart this Space* e riaprilo subito.
- Per un'istanza «bloccata» che non si può attivare da altri: in Settings → *Variables and secrets* aggiungi il segreto `GPOOL_TOKEN`
  (24+ caratteri; generalo da «📱 Telefono → Genera codice segreto») e apri l'app con `https://…hf.space/#t=IL_CODICE`.

## Per chi pubblica il progetto (una volta sola, 5 minuti)
Il pulsante del README funziona solo se esiste un Space «modello» pubblico, creato da te:
1. Su https://huggingface.co/new-space: nome `gpu-pool`, SDK **Docker → Blank**, hardware **CPU basic (FREE)**, visibilità **Public**.
2. *Files → Add file → Upload files*: trascina il contenuto di una copia **nuova** di `universal_gpu_pool` (da ZIP appena scaricato, non la cartella di lavoro:
   contiene le tue chiavi). Il programma cancella comunque `secrets.env`/`local_config.yaml` all'avvio, ma resterebbero nella cronologia.
3. *Settings → Variables and secrets → New secret*: `GPOOL_TOKEN` = un codice lungo qualsiasi (almeno 24 caratteri) **che non userai**: serve a
   impedire che qualcuno attivi il modello. Le copie non ereditano i segreti.
4. Nel `README.md` di GitHub sostituisci `TUO_UTENTE_HF` con il tuo nome utente di Hugging Face.

## Limiti onesti
- Non provato su Hugging Face vero (da qui non è raggiungibile) e l'immagine Docker non è stata costruita nell'ambiente di sviluppo.
- Dopo molta inattività il server va in pausa e si risveglia alla prima visita (circa un minuto).
- Se si riavvia **durante un lavoro**, il lavoro continua su Kaggle ma l'app perde il filo: i file restano nel tuo account Kaggle (Output del notebook «GPool …»).
- Dopo 8 codici sbagliati l'accesso si blocca per 10 minuti.
