# Usarlo da qualsiasi posto, gratis e senza carta: Hugging Face Spaces

Così il programma gira su un server di Hugging Face invece che sul tuo PC: dal telefono funziona ovunque
(anche con la rete dati) e il PC può restare spento. Serve solo un indirizzo email: nessuna carta di pagamento.

## Cosa devi sapere prima
- **Il disco si azzera a ogni riavvio** del server. Scarica i risultati appena sono pronti. Le chiavi vanno nei **Secrets** (passo 5), non nel pannello «Account e chiavi».
- Se resta **inattivo a lungo** va in pausa: alla prima visita si risveglia (circa un minuto).
- Se si riavvia **mentre un lavoro è in corso**, il lavoro continua su Kaggle ma la webapp perde il filo: i file restano nel tuo account Kaggle (kaggle.com → Code → il notebook «GPool …» → Output).
- L'indirizzo del server è pubblico: lo protegge il **codice segreto** (passo 5). Usane uno lungo e non condividerlo.
- Non l'ho potuto provare su Hugging Face vero (da qui non è raggiungibile): se un passo non torna, mandami cosa vedi.

## Passi (non serve il terminale)
1. **Account**: crea un account gratuito su https://huggingface.co/join (basta l'email).
2. **ATTENZIONE alle chiavi**: scarica da GitHub uno **ZIP nuovo** del progetto (Code → Download ZIP) ed estrailo.
   **Non usare la cartella con cui lavori sul PC**: contiene `config/secrets.env` con le tue chiavi, e su un server pubblico si vedrebbero.
   (Se per errore le carichi, il programma le cancella all'avvio, ma resterebbero visibili nella cronologia: in quel caso revocale e creane di nuove.)
3. **Crea il Space**: https://huggingface.co/new-space → Nome `gpu-pool` · SDK **Docker** → *Blank* · Hardware **CPU basic · FREE** · Visibilità **Public** → *Create Space*.
4. **Carica i file**: scheda *Files* → *Add file* → *Upload files* → trascina **tutto il contenuto** della cartella `universal_gpu_pool`
   (compresi `Dockerfile` e `README.md`, che sostituisce quello creato) → *Commit changes*.
5. **Segreti**: scheda *Settings* → *Variables and secrets* → *New secret*:
   | Nome | Valore |
   |---|---|
   | `GPOOL_TOKEN` | il codice segreto: generalo dalla webapp sul PC → pulsante «📱 Telefono» → «Genera codice segreto» (almeno 24 caratteri) |
   | `KAGGLE_API_TOKEN` | il tuo token di kaggle.com/settings/api |
   | `GROQ_API_KEY` *(facoltativo)* | la chiave Groq (o `CEREBRAS_API_KEY`, `GEMINI_API_KEY`) per l'analisi dei progetti e le domande |
6. **Attendi** 2-5 minuti: in alto lo stato passa da *Building* a *Running*.
7. **Apri** `https://NOMEUTENTE-gpu-pool.hf.space/#t=IL_TUO_CODICE` sul telefono (il tuo nome utente al posto di `NOMEUTENTE`) e aggiungilo ai preferiti / alla schermata Home.

Se sbagli il codice 8 volte di fila, l'accesso si blocca per 10 minuti (protezione contro i tentativi a caso).
