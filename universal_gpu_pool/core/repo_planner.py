"""Da un repository GitHub a un «piano» eseguibile, senza che l'utente scriva codice.

Piano = {
  title, summary,                   testo in italiano per chi non è tecnico
  gpu,                              serve una GPU?
  setup: [comandi shell],           installazione (pip install ..., scarico pesi)
  run: "comando con {segnaposto}",  cosa eseguire
  inputs: [{key,label,help,type,default,required,choices}],   cosa chiedere all'utente
  outputs: [glob],                  dove il programma scrive i risultati (oltre a {OUT})
  timeout_min, warnings, source
}

Due strade: (1) l'IA gratuita già collegata legge README e struttura e propone il piano;
(2) senza IA, un'analisi semplice basata su regole (comandi d'esempio del README, argparse).
In ogni caso il piano passa da `normalize_plan`, che scarta tutto ciò che non è sicuro/valido.
"""
import json
import re
import shlex

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
RESERVED = ("OUT", "REPO", "INPUTS")
TYPES = ("text", "textarea", "url", "number", "choice", "bool")
GPU_HINT_RE = re.compile(r"\b(torch|tensorflow|jax|cuda|transformers|diffusers|xformers|flash.?attn|onnxruntime-gpu|"
                         r"nvidia|vllm|accelerate|bitsandbytes|cupy)\b", re.I)


# ---------------------------------------------------------------------------------------------
# normalizzazione e validazione (vale per ogni piano, da IA o da regole)
# ---------------------------------------------------------------------------------------------
def _s(v, n):
    return str(v if v is not None else "").strip()[:n]


def normalize_plan(d, source="ia"):
    if not isinstance(d, dict):
        raise ValueError("il piano non è un oggetto")
    warnings = [_s(w, 300) for w in (d.get("warnings") or []) if str(w).strip()][:6] if isinstance(d.get("warnings"), list) else []
    inputs, seen = [], set()
    for it in (d.get("inputs") or [])[:6]:
        if not isinstance(it, dict):
            continue
        key = _s(it.get("key"), 40).lower().replace("-", "_")
        if not KEY_RE.match(key) or key in seen:
            continue
        typ = _s(it.get("type"), 12).lower() or "text"
        typ = {"file": "url", "image": "url", "video": "url", "int": "number", "integer": "number", "float": "number",
               "select": "choice", "boolean": "bool", "string": "text"}.get(typ, typ)
        if typ not in TYPES:
            typ = "text"
        choices = [_s(c, 80) for c in (it.get("choices") or [])[:20]] if typ == "choice" else []
        if typ == "choice" and not choices:
            typ = "text"
        seen.add(key)
        inputs.append({"key": key, "label": _s(it.get("label"), 120) or key.replace("_", " "),
                       "help": _s(it.get("help"), 300), "type": typ, "default": _s(it.get("default"), 500),
                       "required": bool(it.get("required", True)), "choices": choices})

    def cmds(v, maxn, maxlen):
        if isinstance(v, str):
            v = [x for x in v.splitlines()]
        out = []
        for c in (v or [])[:maxn]:
            c = _s(c, maxlen)
            if c and not c.startswith("#"):
                out.append(c)
        return out

    setup = cmds(d.get("setup"), 14, 600)
    run = _s(d.get("run"), 2000)
    outputs = []
    for o in (d.get("outputs") or [])[:10]:
        o = _s(o, 200)
        if o and not o.startswith(("/", "~")) and ".." not in o.split("/") and "{" not in o and "}" not in o:
            outputs.append(o)
    try:
        tmin = int(d.get("timeout_min") or 120)
    except (TypeError, ValueError):
        tmin = 120
    tmin = max(5, min(tmin, 540))
    known = {i["key"] for i in inputs} | set(RESERVED)
    unknown = sorted({m for text in setup + [run] for m in PLACEHOLDER_RE.findall(text)} - known)
    if unknown:
        warnings.append("Nel comando ci sono segnaposto sconosciuti che resteranno com'erano: " + ", ".join(unknown))
    if re.search(r"gradio|streamlit|\.launch\(|--share\b|serve_forever|app\.run\(", run):
        warnings.append("Questo comando potrebbe avviare un sito web interattivo: su Kaggle non puoi aprirlo. "
                        "Meglio un comando che elabora i file e salva il risultato.")
    return {"title": _s(d.get("title"), 100), "summary": _s(d.get("summary"), 900),
            "gpu": bool(d.get("gpu", True)), "setup": setup, "run": run, "inputs": inputs, "outputs": outputs,
            "timeout_min": tmin, "warnings": warnings, "source": source}


def render(template, values, out_dir="/kaggle/working/outputs", repo_dir="repo", inputs_dir="inputs"):
    """Sostituisce {chiave} con il valore, già protetto per la shell. Segnaposto sconosciuti: invariati."""
    vals = dict(values)
    vals.update({"OUT": out_dir, "REPO": repo_dir, "INPUTS": inputs_dir})

    def sub(m):
        k = m.group(1)
        return shlex.quote(str(vals[k])) if k in vals else m.group(0)
    return PLACEHOLDER_RE.sub(sub, template)


def validate_values(plan, values):
    """Controlla e completa i valori inseriti dall'utente. Ritorna il dizionario pulito o solleva ValueError."""
    out = {}
    for it in plan["inputs"]:
        v = values.get(it["key"], None)
        v = it["default"] if (v is None or str(v).strip() == "") else v
        v = str(v).strip()
        if it["type"] == "bool":
            v = "1" if str(v).lower() in ("1", "true", "si", "sì", "yes", "on") else "0"
        if not v and it["required"] and it["type"] != "bool":
            raise ValueError(f"Compila il campo «{it['label']}».")
        if it["type"] == "number" and v:
            try:
                float(v)
            except ValueError:
                raise ValueError(f"«{it['label']}» deve essere un numero.") from None
        if it["type"] == "choice" and v and v not in it["choices"]:
            raise ValueError(f"«{it['label']}»: scegli uno dei valori proposti.")
        if it["type"] == "url" and v:
            if not re.match(r"^https?://\S+$", v):
                raise ValueError(f"«{it['label']}» deve essere un link che inizia con https://")
            from core.linkfix import direct_url
            try:
                v = direct_url(v)[0]
            except ValueError as e:
                raise ValueError(f"«{it['label']}»: {e}") from None
        out[it["key"]] = v[:5000]
    return out


# ---------------------------------------------------------------------------------------------
# strada 1: IA
# ---------------------------------------------------------------------------------------------
PROMPT_SYSTEM = """Sei un ingegnere ML esperto. Ricevi la descrizione di un repository GitHub (README, struttura, \
file chiave) e devi dire come eseguirlo AUTOMATICAMENTE su un notebook Kaggle (Linux Ubuntu, Python 3.11, internet \
attivo, GPU NVIDIA T4/P100 16 GB, PyTorch/CUDA/transformers/numpy/opencv già installati, utente root, nessuna \
interattività, nessuna interfaccia grafica, nessun sito web apribile).
Rispondi SOLO con un oggetto JSON (niente testo fuori dal JSON, niente ```), con queste chiavi:
{
 "title": "nome breve del progetto",
 "summary": "1-3 frasi in italiano semplice per una persona NON tecnica: cosa fa il programma e cosa produce",
 "gpu": true/false,
 "setup": ["comandi shell di installazione, uno per elemento; già in cartella del repo; es. pip install -r requirements.txt"],
 "run": "UN comando shell che elabora i dati e SALVA i risultati in {OUT}; usa {chiave} per i valori scelti dall'utente",
 "inputs": [{"key":"snake_case","label":"etichetta italiana breve","help":"aiuto italiano 1 frase","type":"text|textarea|url|number|choice|bool","default":"","required":true,"choices":[]}],
 "outputs": ["eventuali percorsi/glob relativi al repo dove il programma scrive risultati, se non usa {OUT}"],
 "timeout_min": 60,
 "warnings": ["limiti importanti in italiano, es. servono pesi enormi, funziona solo con video"]
}
Regole: preferisci il comando di inferenza/demo indicato nel README e adattalo per salvare in {OUT}. Non reinstallare \
torch se non è indispensabile. Se servono pesi, scaricali in "setup" come dice il README (huggingface-cli, wget, \
python -c ...). Non avviare gradio/streamlit/viser/server: usa le opzioni da riga di comando o crea un piccolo script \
python in "run" con python - <<'PY' ... PY solo se strettamente necessario. Gli input utente devono essere ciò che una \
persona non tecnica fornisce davvero (testo, link a immagine/video/zip); i file si danno come link diretti (type url): \
nel comando {chiave} diventerà il percorso locale del file scaricato. Massimo 5 input. Tutti i comandi devono funzionare \
senza input da tastiera (usa -y, --yes, --no-input). Se il progetto è solo una libreria senza programma eseguibile, \
scrivi in "warnings" che non è eseguibile e proponi come "run" un esempio minimo dal README."""


def _extract_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("nessun JSON nella risposta")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("JSON incompleto")


def build_prompt(info):
    tree = "\n".join(info["tree"][:220])
    files = "\n\n".join(f"### {p}\n{t[:2500]}" for p, t in list(info["files"].items())[:8])
    return (f"Repository: {info['url']} (ramo {info['ref']})\nDescrizione GitHub: {info['description']}\n"
            f"Linguaggio: {info['language']} · Licenza: {info['license'] or 'n/d'}\n\n"
            f"## README\n{info['readme'][:9000]}\n\n## File del repository (parziale)\n{tree}\n\n"
            f"## File chiave\n{files}")


def llm_plan(info, backend):
    msgs = [{"role": "system", "content": PROMPT_SYSTEM}, {"role": "user", "content": build_prompt(info)}]
    r = backend.generate(msgs, temperature=0.1, max_tokens=2200)
    plan = normalize_plan(_extract_json(r["text"]), source="ia")
    if not plan["run"]:
        raise ValueError("l'IA non ha indicato cosa eseguire")
    plan["ia_provider"] = r.get("provider", "")
    return plan


# ---------------------------------------------------------------------------------------------
# strada 2: regole semplici
# ---------------------------------------------------------------------------------------------
def _first_paragraph(readme):
    text = re.sub(r"<[^>]+>", " ", readme or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[!\[.*?\]\(.*?\)\]\(.*?\)", " ", text)
    for block in re.split(r"\n\s*\n", text):
        b = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", block)
        b = re.sub(r"[#*`>|_-]{1,}", " ", b)
        b = re.sub(r"\s+", " ", b).strip()
        if len(b) > 40 and not b.lower().startswith(("table of contents", "contents")):
            return b[:400]
    return ""


def _readme_commands(readme):
    cmds = []
    for block in re.findall(r"```[^\n]*\n(.*?)```", readme or "", re.S):
        lines = block.splitlines()
        i = 0
        while i < len(lines):
            ln = lines[i].strip().lstrip("$ ").strip()
            if re.match(r"^python3?\s+\S+", ln):
                cmd = ln
                while cmd.endswith("\\") and i + 1 < len(lines):      # comando su più righe
                    i += 1
                    cmd = cmd[:-1].rstrip() + " " + lines[i].strip()
                cmds.append(re.sub(r"\s+", " ", cmd))
            i += 1
    return cmds


PLACEHOLDER_ARG_RE = re.compile(r"^(?:<[^>]+>|\[[^\]]+\]|/?path/to/\S*|/path/\S*|\S*your[_-]\S*|\.\.\.|xxx\S*|YOUR_\S*)$", re.I)


def _score_command(c):
    low = c.lower()
    s = 0
    if re.search(r"infer|demo|predict|generate|run|main|sample|test|reconstruct|process|eval", low):
        s += 3
    if re.search(r"train|finetune|fine-tune|pretrain|benchmark|preprocess", low):
        s -= 4
    if re.search(r"gradio|streamlit|webui|--share|--port|viser|server|serve|flask|uvicorn", low):
        s -= 5
    if re.search(r"pip |install|setup\.py", low):
        s -= 6
    return s


def _command_to_plan_parts(cmd):
    """Trasforma i valori d'esempio (path/to/…, <…>) in campi da compilare."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return cmd, []
    inputs, out, last_flag = [], [], None
    for t in toks:
        if t.startswith("-"):
            last_flag = t.lstrip("-").replace("-", "_").lower()
            out.append(t)
            continue
        if PLACEHOLDER_ARG_RE.match(t):
            key = re.sub(r"[^a-z0-9_]", "", (last_flag or f"valore{len(inputs) + 1}"))[:30] or f"valore{len(inputs) + 1}"
            if not KEY_RE.match(key) or any(i["key"] == key for i in inputs):
                key = f"valore{len(inputs) + 1}"
            typ = "url" if re.search(r"image|img|video|photo|file|input|path|dir|folder|zip|audio|model", key) else "text"
            inputs.append({"key": key, "label": key.replace("_", " ").capitalize(), "type": typ, "required": True,
                           "help": "Link diretto al file" if typ == "url" else "Valore richiesto dal programma",
                           "default": ""})
            out.append("{" + key + "}")
        else:
            out.append(shlex.quote(t))
            last_flag = last_flag if t.startswith("-") else None
    return " ".join(out), inputs


def _argparse_required(src):
    """Argomenti obbligatori di uno script argparse -> [(flag, tipo)]."""
    found = []
    for m in re.finditer(r"add_argument\(\s*[\"'](--?[\w-]+)[\"']([^)]*)\)", src or ""):
        flag, rest = m.group(1), m.group(2)
        if re.search(r"required\s*=\s*True", rest):
            found.append(flag)
    return found


def heuristic_plan(info):
    files = info["files"]
    setup = []
    if any(p.startswith("requirements") for p in files):
        req = "requirements.txt" if "requirements.txt" in files else sorted(p for p in files if p.startswith("requirements"))[0]
        setup.append(f"pip install -q -r {req}")
    elif "pyproject.toml" in files or "setup.py" in files:
        setup.append("pip install -q -e .")
    elif "environment.yml" in files or "environment.yaml" in files:
        setup.append("# environment.yml (conda) non supportato: installa a mano le librerie in Dettagli tecnici")

    cmds = sorted(_readme_commands(info["readme"]), key=_score_command, reverse=True)
    run, inputs, warnings = "", [], []
    if cmds and _score_command(cmds[0]) > -4:
        run, inputs = _command_to_plan_parts(cmds[0])
        if info["readme"] and "python" in run:
            warnings.append("Comando preso dagli esempi del README: controllalo in «Dettagli tecnici».")
    else:
        entries = [p for p in files if p.endswith(".py") and ENTRY_NAME.search(p)]
        entries.sort(key=lambda p: (-_score_command(p), p.count("/"), p))
        if entries:
            e = entries[0]
            req = _argparse_required(files[e])
            parts = [f"python {shlex.quote(e)}"]
            for flag in req:
                key = re.sub(r"[^a-z0-9_]", "", flag.lstrip("-").replace("-", "_").lower()) or "valore"
                typ = "url" if re.search(r"image|img|video|photo|file|input|path|dir|folder|zip", key) else "text"
                inputs.append({"key": key, "label": key.replace("_", " ").capitalize(), "type": typ, "required": True,
                               "help": "", "default": ""})
                parts.append(f"{flag} {{{key}}}")
            run = " ".join(parts)
            warnings.append(f"Ho scelto «{e}» come programma principale: se non è quello giusto, cambialo in «Dettagli tecnici».")
        else:
            warnings.append("Non ho trovato un comando da eseguire: scrivilo tu in «Dettagli tecnici».")
    blob = " ".join(files.get(p, "") for p in files if p.startswith(("requirements", "pyproject", "setup")))
    gpu = bool(GPU_HINT_RE.search(blob + " " + info["readme"][:4000]))
    summary = info["description"] or _first_paragraph(info["readme"]) or "Progetto GitHub senza descrizione."
    if _first_paragraph(info["readme"]) and info["description"]:
        summary = info["description"]
    return normalize_plan({"title": info["repo"], "summary": summary, "gpu": gpu, "setup": setup, "run": run,
                           "inputs": inputs, "warnings": warnings, "timeout_min": 90}, source="semplice")


ENTRY_NAME = re.compile(r"(^|/)(app|demo|infer|inference|main|run|predict|generate|sample|cli|reconstruct\w*|process\w*)[\w-]*\.py$", re.I)


def make_plan(info, backend=None):
    """-> piano. Prova con l'IA (se c'è un account collegato), altrimenti/in caso di errore usa le regole."""
    note = ""
    if backend is not None:
        try:
            if backend.active_providers():
                plan = llm_plan(info, backend)
                plan["note"] = ""
                return plan
            note = "Nessun account IA collegato: analisi semplice. Collega Groq/Cerebras/Gemini in «Account e chiavi» per risultati migliori."
        except Exception as e:   # IA assente, quota finita, risposta non valida...
            note = f"L'IA non ha risposto in modo utilizzabile ({str(e)[:120]}): uso un'analisi semplice."
    else:
        note = "Analisi semplice."
    plan = heuristic_plan(info)
    plan["note"] = note
    return plan
