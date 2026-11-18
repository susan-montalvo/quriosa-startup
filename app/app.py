"""Quriosa: del texto al audiolibro narrado con voces peruanas clonadas con permiso.

    1. Texto        un fragmento de una obra de dominio público
    2. Plan         Gemini lo divide en segmentos por voz, con estilo, pausas y pronunciación (JSON estructurado)
    3. Reparto      cada personaje recibe una voz clonada con autorización escrita
    4. Narración    audio generado con las voces clonadas (Qwen3-TTS y VoxCPM2, en backend/narrar.py)
    5. Calidad      Whisper transcribe cada segmento y mide el error (WER)

La interfaz está en español y en inglés: el selector de idioma está arriba, y ?lang=en abre la versión en inglés.
La obra, la escena y los audios del demo son en español en las dos versiones.

Uso local: streamlit run app/app.py   (la clave GEMINI_API_KEY va en .env o en los secretos de Streamlit)
"""
import difflib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import streamlit as st

RAIZ = Path(__file__).resolve().parent.parent
DEMO = Path(__file__).resolve().parent / "demo"
ASSETS = Path(__file__).resolve().parent / "assets"
PLAN_DEMO = RAIZ / "data" / "plan-narracion-carmelo-cap5.json"
PROMPT = RAIZ / "ai" / "plan_narracion.md"
# En orden de preferencia; si uno está saturado (503) o tarda, se pasa al siguiente.
MODELOS = ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3-flash-preview",
           "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
ESPERA_MAXIMA = 60  # segundos en total; después se muestra el plan pregenerado
PALABRAS_POR_MINUTO = 131  # medido en el audio del demo (83 palabras en 37.9 s)
IDIOMAS = {"es": "Español", "en": "English"}

st.set_page_config(page_title="Quriosa · audiolibros peruanos", page_icon=str(ASSETS / "icono.png"), layout="wide")

if "lang" not in st.session_state:
    pedido = st.query_params.get("lang", "es")
    st.session_state.lang = pedido if pedido in IDIOMAS else "es"


def t(es, en):
    """Devuelve el texto en el idioma elegido."""
    return en if st.session_state.lang == "en" else es


def precios():  # dólares por hora de audio terminado (README, «Modelo de negocio»)
    return [(t("Voz clonada", "Cloned voice"), 220, 220),
            (t("Híbrido: voz humana en las escenas clave", "Hybrid: human voice in key scenes"), 320, 320)]


def voces():
    return {
        "narrador-1": t("Narrador 1 · voz femenina peruana (clonada con permiso)",
                        "Narrator 1 · Peruvian female voice (cloned with permission)"),
        "narrador-2": t("Narrador 2 · voz masculina peruana (clonada con autorización escrita)",
                        "Narrator 2 · Peruvian male voice (cloned with written authorization)"),
    }


def nombre_personaje(p):
    return {"narrador": t("Narrador", "Narrator"), "juez": t("Juez", "Judge")}.get(p, p.capitalize())


# Datos ---------------------------------------------------------------------------

def plan_demo():
    """Se lee en cada ejecución (es pequeño): así un cambio en el archivo se ve sin reiniciar la app."""
    return json.loads(PLAN_DEMO.read_text(encoding="utf-8"))


def texto_de(plan):
    """Reconstruye el fragmento: los diálogos van en su propio párrafo."""
    partes = []
    for s in plan["segmentos"]:
        es_dialogo = s["texto"].startswith(("–", "—", "-"))
        if not partes or es_dialogo or partes[-1].startswith(("–", "—", "-")):
            partes.append(s["texto"])
        else:
            partes[-1] += " " + s["texto"]
    return "\n\n".join(partes)


def normal(texto):
    return " ".join(texto.split())


def clave():
    try:
        if "GEMINI_API_KEY" in st.secrets:
            return st.secrets["GEMINI_API_KEY"]
    except Exception:
        pass
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    env = RAIZ / ".env"
    if env.exists():
        for linea in env.read_text(encoding="utf-8").splitlines():
            if linea.startswith("GEMINI_API_KEY="):
                return linea.split("=", 1)[1].strip().strip("\"'")
    return None


# Plan de narración con Gemini (salida estructurada) -------------------------------

TEXTO = {"type": "STRING"}
ESQUEMA = {
    "type": "OBJECT",
    "properties": {
        "personajes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "id": TEXTO, "nombre": TEXTO, "descripcion": TEXTO, "estilo_base": TEXTO},
            "required": ["id", "nombre", "descripcion", "estilo_base"]}},
        "segmentos": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "n": {"type": "INTEGER"}, "personaje": TEXTO, "texto": TEXTO, "texto_tts": TEXTO,
            "estilo": TEXTO, "estilo_tts": TEXTO, "pausa_despues_ms": {"type": "INTEGER"},
            "importancia": {"type": "INTEGER"}, "motivo_clave": TEXTO},
            "required": ["n", "personaje", "texto", "texto_tts", "estilo", "estilo_tts", "pausa_despues_ms",
                         "importancia", "motivo_clave"]}},
        "pronunciacion": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "palabra": TEXTO, "como_se_dice": TEXTO, "nota": TEXTO},
            "required": ["palabra", "como_se_dice", "nota"]}},
        "erratas": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "dice": TEXTO, "probablemente": TEXTO, "contexto": TEXTO},
            "required": ["dice", "probablemente", "contexto"]}},
    },
    "required": ["personajes", "segmentos", "pronunciacion", "erratas"],
}
EN_INGLES = ("\n\n## Idioma de la interfaz: inglés (esta regla reemplaza a las anteriores sobre el idioma)\n\n"
             "La persona que usa la app lee en inglés. Escribe en inglés los campos descriptivos: `descripcion` y "
             "`estilo_base` de cada personaje; `estilo` y `motivo_clave` de cada segmento; `nota` de pronunciación; "
             "`contexto` de las erratas. `estilo_tts` sigue en inglés. No traduzcas `texto`, `texto_tts`, `nombre`, "
             "`palabra`, `como_se_dice`, `dice` ni `probablemente`: van en el idioma original del fragmento.")


def armar_plan(texto, obra, autor, ingles=False):
    """Pide el plan a Gemini; prueba los modelos en orden. Devuelve (plan, modelo, segundos)."""
    pedido_texto = f"Obra: {obra}\nAutor: {autor}\n\nFragmento:\n{texto}"
    instrucciones = PROMPT.read_text(encoding="utf-8") + (EN_INGLES if ingles else "")
    cuerpo = {
        "systemInstruction": {"parts": [{"text": instrucciones}]},
        "contents": [{"parts": [{"text": pedido_texto}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": ESQUEMA, "temperature": 0.2},
    }
    errores = []
    limite = time.time() + ESPERA_MAXIMA
    for modelo in MODELOS:
        queda = limite - time.time()
        if queda < 5:
            errores.append("timeout")
            break
        pedido = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent",
            data=json.dumps(cuerpo).encode(), headers={"x-goog-api-key": clave(), "Content-Type": "application/json"})
        inicio = time.time()
        try:
            respuesta = json.load(urllib.request.urlopen(pedido, timeout=min(30, queda)))
            partes = respuesta["candidates"][0]["content"]["parts"]
            plan = json.loads(next(p["text"] for p in reversed(partes) if "text" in p))
            return plan, modelo, time.time() - inicio
        except urllib.error.HTTPError as e:
            errores.append(f"{modelo}: error {e.code}")
        except Exception as e:  # tiempo agotado, respuesta incompleta…
            errores.append(f"{modelo}: {type(e).__name__}")
    raise RuntimeError("; ".join(errores))


def marcar_claves(segmentos, meta=0.30, tope=0.40):
    """Plan híbrido: elige los segmentos más importantes hasta cerca del 30 % de las palabras.

    Gemini solo califica la importancia (1 a 5); la proporción la controla la app. Un segmento no se parte:
    se salta el que haría pasar el total del 40 %, y siempre se marca al menos uno."""
    palabras = {s["n"]: len(s["texto"].split()) for s in segmentos}
    total = sum(palabras.values()) or 1
    marcados, acumulado = set(), 0
    for s in sorted(segmentos, key=lambda s: (-s.get("importancia", 0), s["n"])):
        if acumulado / total >= meta:
            break
        if marcados and (acumulado + palabras[s["n"]]) / total > tope:
            continue
        marcados.add(s["n"])
        acumulado += palabras[s["n"]]
    for s in segmentos:
        s["clave"] = s["n"] in marcados
    return segmentos


def cobertura(texto, plan):
    """Qué parte del texto original reproducen los segmentos, unidos en orden (1.0 = todo, sin cambios)."""
    unido = " ".join(s["texto"] for s in plan["segmentos"])
    return difflib.SequenceMatcher(None, normal(texto), normal(unido), autojunk=False).ratio()


# Página -------------------------------------------------------------------------------

def cambiar_idioma():
    st.query_params["lang"] = st.session_state.lang


logo, selector = st.columns([4, 1])
logo.image(str(ASSETS / "logo.svg"), width=380)  # la app es siempre clara (.streamlit/config.toml)
selector.radio("Idioma / Language", list(IDIOMAS), format_func=IDIOMAS.get, key="lang", horizontal=True,
               on_change=cambiar_idioma)

st.markdown(t("**Convierte los libros infantiles y juveniles de las editoriales escolares peruanas en audiolibros "
              "narrados con voces peruanas replicadas con permiso.**",
              "**Turns children's and young-adult books from Peruvian school publishers into audiobooks narrated "
              "with Peruvian voices cloned with permission.**"))
st.caption(t("Demo con *El caballero Carmelo*, de Abraham Valdelomar (dominio público). "
             "Las voces replicadas con IA son de narradores que autorizaron su uso; en el plan híbrido, el clímax "
             "se escucha también con la voz humana grabada.",
             "Demo with *El caballero Carmelo*, a Peruvian short story by Abraham Valdelomar (public domain), "
             "narrated in Spanish. The AI-cloned voices belong to narrators who authorized their use; in the hybrid "
             "plan, the climax can also be heard in the recorded human voice."))

demo = plan_demo()
texto_demo = texto_de(demo)

# 1. Texto
st.header(t("1. Texto", "1. Text"))
c1, c2 = st.columns(2)
obra = c1.text_input(t("Obra", "Work"), demo["obra"]["titulo"])
autor = c2.text_input(t("Autor", "Author"), demo["obra"]["autor"])
texto = st.text_area(t("Fragmento", "Excerpt"), texto_demo, height=220,
                     help=t("Pega aquí cualquier fragmento de una obra de dominio público.",
                            "Paste any excerpt from a public-domain work here."))
es_demo = normal(texto) == normal(texto_demo)

# 2. Plan de narración
st.header(t("2. Plan de narración", "2. Narration plan"))
st.write(t("Gemini lee el fragmento y devuelve un **JSON estructurado**: quién habla en cada segmento, "
           "con qué estilo, cuánta pausa dejar y qué palabras son difíciles de pronunciar.",
           "Gemini reads the excerpt and returns **structured JSON**: who speaks in each segment, in what style, "
           "how long to pause and which words are hard to pronounce."))

if st.button(t("✨ Armar plan con IA", "✨ Build plan with AI"), type="primary"):
    if not clave():
        st.session_state.plan = (demo, None, None, t("No hay clave de Gemini configurada.",
                                                     "No Gemini key is configured."))
    else:
        with st.spinner(t("Gemini está leyendo el texto…", "Gemini is reading the text…")):
            try:
                plan, modelo, segundos = armar_plan(texto, obra, autor, ingles=st.session_state.lang == "en")
                st.session_state.plan = (plan, modelo, segundos, None)
            except Exception as e:
                aviso = None if es_demo else t("Este es el plan de la escena de ejemplo, no el de tu texto.",
                                               "This is the plan for the sample scene, not for your text.")
                st.session_state.plan = (demo, None, None,
                                         f"{t('Gemini no respondió', 'Gemini did not respond')} ({e}). "
                                         f"{aviso or ''}".strip())

if "plan" in st.session_state:
    plan, modelo, segundos, problema = st.session_state.plan
    marcar_claves(plan["segmentos"])
    if problema:
        st.warning(f"{problema} " + t("Se muestra el plan pregenerado de la escena de ejemplo.",
                                      "Showing the pre-generated plan for the sample scene."))
    else:
        st.success(t(f"Plan armado por **{modelo}** en {segundos:.1f} s · {len(plan['segmentos'])} segmentos · "
                     f"cobertura del texto original: **{cobertura(texto, plan):.0%}**",
                     f"Plan built by **{modelo}** in {segundos:.1f} s · {len(plan['segmentos'])} segments · "
                     f"coverage of the original text: **{cobertura(texto, plan):.0%}**"))
    if st.session_state.lang == "en":
        st.caption("The story is in Spanish, so the segment text stays in Spanish.")

    st.subheader(t("Segmentos", "Segments"))
    tabla = pd.DataFrame(plan["segmentos"])
    voz_hibrido = t("voz (híbrido)", "voice (hybrid)")
    tabla[voz_hibrido] = [t("🎙️ humana", "🎙️ human") if s.get("clave") else t("clonada", "cloned")
                          for s in plan["segmentos"]]
    columnas = [c for c in ["n", "personaje", voz_hibrido, "texto", "estilo", "pausa_despues_ms"] if c in tabla]
    st.dataframe(tabla[columnas].rename(columns={
        "personaje": t("personaje", "character"), "texto": t("texto", "text"), "estilo": t("estilo", "style"),
        "pausa_despues_ms": t("pausa (ms)", "pause (ms)")}), hide_index=True, width="stretch")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader(t("Personajes", "Characters"))
        st.dataframe(pd.DataFrame(plan["personajes"])[["id", "nombre", "descripcion", "estilo_base"]].rename(
            columns={"nombre": t("nombre", "name"), "descripcion": t("descripcion", "description"),
                     "estilo_base": t("estilo_base", "base style")}), hide_index=True, width="stretch")
    with c2:
        st.subheader(t("Pronunciación", "Pronunciation"))
        if plan.get("pronunciacion"):
            st.dataframe(pd.DataFrame(plan["pronunciacion"]).rename(columns={
                "palabra": t("palabra", "word"), "como_se_dice": t("como_se_dice", "how to say it"),
                "nota": t("nota", "note")}), hide_index=True, width="stretch")
        else:
            st.write(t("Sin palabras difíciles.", "No hard words."))
    if plan.get("erratas"):
        st.subheader(t("Posibles erratas (para revisión humana)", "Possible typos (for human review)"))
        st.dataframe(pd.DataFrame(plan["erratas"]).rename(columns={
            "dice": t("dice", "says"), "probablemente": t("probablemente", "probably"),
            "contexto": t("contexto", "context")}), hide_index=True, width="stretch")

    with st.expander(t("Ver el JSON completo", "See the full JSON")):
        st.json(plan)
    st.download_button(t("Descargar el plan (JSON)", "Download the plan (JSON)"),
                       json.dumps(plan, ensure_ascii=False, indent=2), "plan-narracion.json", "application/json")

    # 3. Reparto
    st.header(t("3. Reparto", "3. Casting"))
    st.write(t("Cada personaje recibe una voz. Solo se usan voces de narradores que dieron su autorización escrita.",
               "Each character gets a voice. Only voices from narrators who gave written authorization are used."))
    sugerido = {p["id"]: p.get("voz") for p in demo["personajes"]}
    lista_voces = voces()
    for i, p in enumerate(plan["personajes"]):
        voz = sugerido.get(p["id"]) or ("narrador-2" if p["id"] not in ("narrador", "narradora") and i else "narrador-1")
        st.selectbox(f"{p['nombre']} ({p['id']})", list(lista_voces), index=list(lista_voces).index(voz),
                     format_func=lista_voces.get, key=f"voz-{p['id']}")

    palabras = lambda segs: sum(len(s["texto"].split()) for s in segs)
    claves = [s for s in plan["segmentos"] if s.get("clave")]
    total = palabras(plan["segmentos"])
    with st.expander(t("Opción híbrida: voz humana en las escenas clave", "Hybrid option: human voice in key scenes")):
        st.write(t(f"Gemini calificó la importancia de cada segmento (1 a 5) y la app eligió los más importantes "
                   f"hasta cerca del 30 % del texto: **{len(claves)} de {len(plan['segmentos'])} segmentos** "
                   f"({palabras(claves) / total:.0%} de las palabras), que el locutor graba en estudio. "
                   "El resto lo narra su voz clonada: el oyente escucha una sola voz.",
                   f"Gemini rated each segment's importance (1 to 5) and the app picked the most important ones, "
                   f"up to about 30% of the text: **{len(claves)} of {len(plan['segmentos'])} segments** "
                   f"({palabras(claves) / total:.0%} of the words), which the narrator records in a studio. "
                   "The narrator's cloned voice reads the rest, so the listener hears a single voice."))
        if claves:
            st.dataframe(pd.DataFrame(claves)[["n", "personaje", "importancia", "texto", "motivo_clave"]].rename(
                columns={"personaje": t("personaje", "character"), "importancia": t("importancia", "importance"),
                         "texto": t("texto", "text"),
                         "motivo_clave": t("por qué es importante", "why it matters")}),
                hide_index=True, width="stretch")

    # 4. Narración
    st.header(t("4. Narración", "4. Narration"))
    st.subheader(t("Escena completa con voz clonada", "Full scene with cloned voices"))
    st.markdown(t("🔊 **Voz generada con IA.** Narradora: *narrador 1*. Juez: *narrador 2*. Es el audiolibro que "
                  "entrega Quriosa en el plan por título: 6 segmentos, 37.9 s, sin errores en el control de calidad.",
                  "🔊 **AI-generated voice.** Narrator: *narrator 1*. Judge: *narrator 2*. This is the audiobook "
                  "Quriosa delivers in the per-title plan: 6 segments, 37.9 s, no errors in quality control. "
                  "The audio is in Spanish."))
    if not es_demo:
        st.info(t("Generar la voz de un texto nuevo toma varios minutos en el servidor de voz "
                  "(Qwen3-TTS y VoxCPM2). Aquí se escucha la escena de ejemplo.",
                  "Generating the voice for a new text takes several minutes on the voice server "
                  "(Qwen3-TTS and VoxCPM2). Here you hear the sample scene."))
    st.audio(str(DEMO / "escena.mp3"))
    calidad = json.loads((DEMO / "control-calidad.json").read_text(encoding="utf-8"))
    with st.expander(t("Escuchar cada segmento", "Listen to each segment")):
        for q in calidad:
            st.markdown(f"**{q['n']}. {nombre_personaje(q['personaje'])}** · {q['texto_tts']}")
            st.audio(str(DEMO / f"segmento-{q['n']:02d}-{q['personaje']}.mp3"))
    st.download_button(t("Descargar el audio (MP3)", "Download the audio (MP3)"), (DEMO / "escena.mp3").read_bytes(),
                       "el-caballero-carmelo-cap5.mp3", "audio/mpeg")

    with st.expander(t("Opción híbrida: el clímax con voz grabada y con voz clonada",
                       "Hybrid option: the climax with recorded and cloned voice")):
        st.write(t("En la opción híbrida, el locutor graba en estudio solo las escenas clave, y su voz clonada narra "
                   "el resto. Aquí se escucha el clímax de la escena (segmento 6) en la versión leída por el narrador 1 "
                   "y en la versión generada con su voz clonada:",
                   "In the hybrid option, the narrator records only the key scenes in a studio, and their cloned "
                   "voice reads the rest. Here is the scene's climax (segment 6) as read by narrator 1 and as "
                   "generated with her cloned voice:"))
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(t("🎙️ **Voz humana, grabada** (narrador 1)", "🎙️ **Human voice, recorded** (narrator 1)"))
            st.audio(str(DEMO / "segmento-06-narrador-humano.mp3"))
            st.caption(t("12.2 s de habla · Whisper: WER 0, sin errores", "12.2 s of speech · Whisper: WER 0, no errors"))
        with c2:
            st.markdown(t("🔊 **Voz generada con IA** (clon del narrador 1)",
                          "🔊 **AI-generated voice** (clone of narrator 1)"))
            st.audio(str(DEMO / "segmento-06-narrador.mp3"))
            st.caption(t("8.8 s de habla · Whisper: WER 0, sin errores", "8.8 s of speech · Whisper: WER 0, no errors"))
        st.markdown(t("**Escena completa, versión híbrida.** Segmentos 1 a 5: 🔊 voz generada con IA · "
                      "segmento 6: 🎙️ voz humana grabada.",
                      "**Full scene, hybrid version.** Segments 1 to 5: 🔊 AI-generated voice · "
                      "segment 6: 🎙️ recorded human voice."))
        st.audio(str(DEMO / "escena-hibrida.mp3"))
        st.caption(t("41.2 s · a los segmentos clonados se les redujo el ruido de fondo (RNNoise) para acercarlos a la "
                     "voz grabada · Whisper transcribe la escena completa sin errores (WER 0).",
                     "41.2 s · background noise was reduced in the cloned segments (RNNoise) to match the recorded "
                     "voice · Whisper transcribes the full scene with no errors (WER 0)."))

    # 5. Control de calidad
    st.header(t("5. Control de calidad", "5. Quality control"))
    st.write(t("Whisper (large-v3-turbo) transcribe cada segmento y lo compara con el texto. Si la tasa de error "
               "de palabras (WER) supera el umbral, el segmento se vuelve a generar.",
               "Whisper (large-v3-turbo) transcribes each segment and compares it with the text. If the word error "
               "rate (WER) is above the threshold, the segment is generated again."))
    qc = pd.DataFrame(calidad)
    qc["resultado"] = qc["wer"].map(lambda w: t("✅ aprobado", "✅ passed") if w <= 0.1 else t("⚠️ revisar", "⚠️ review"))
    st.dataframe(qc[["n", "personaje", "wer", "resultado", "texto_tts", "oido"]].rename(columns={
        "personaje": t("personaje", "character"), "resultado": t("resultado", "result"),
        "texto_tts": t("texto esperado", "expected text"), "oido": t("lo que oyó Whisper", "what Whisper heard"),
        "wer": "WER"}), hide_index=True, width="stretch")

    # 6. Licencias de voz
    st.header(t("6. Licencias de voz", "6. Voice licenses"))
    st.write(t("Cada narrador controla su voz clonada: para qué obra y personaje se usa, dónde se publica, "
               "cada audio que se generó con ella y cómo retirarla.",
               "Each narrator controls their cloned voice: which work and character it is used for, where it is "
               "published, every audio file generated with it, and how to withdraw it."))
    licencias = json.loads((DEMO / "licencias.json").read_text(encoding="utf-8"))
    if st.session_state.lang == "en":
        traduccion = json.loads((DEMO / "licencias-en.json").read_text(encoding="utf-8"))
        licencias = {k: {**v, **traduccion.get(k, {})} for k, v in licencias.items()}
    campos = [(t("Autorización", "Authorization"), "autorizacion"), (t("Obra", "Work"), "obra"),
              (t("Personaje", "Character"), "personaje"), (t("Dónde se usa", "Where it is used"), "donde"),
              (t("Uso comercial", "Commercial use"), "uso_comercial"), (t("Pago", "Payment"), "pago"),
              (t("Retiro", "Withdrawal"), "retiro"), (t("Grabación de referencia", "Reference recording"), "referencia"),
              (t("Modelo", "Model"), "motor")]
    for col, lic in zip(st.columns(len(licencias)), licencias.values()):
        with col.container(border=True):
            st.subheader(lic["titulo"])
            st.markdown("\n".join(f"- **{campo}:** {lic[clave_]}" for campo, clave_ in campos))
            usos = pd.DataFrame(lic["usos"])
            st.markdown(t(f"**Registro de usos:** {len(usos)} segmentos · {usos['segundos'].sum():.1f} s de audio "
                          "· todos con el aviso «Voz generada con IA»",
                          f"**Usage log:** {len(usos)} segments · {usos['segundos'].sum():.1f} s of audio "
                          "· all labeled \"AI-generated voice\""))
            st.dataframe(usos.rename(columns={"segmento": t("segmento", "segment"), "fecha": t("fecha", "date"),
                                              "segundos": t("segundos", "seconds")}),
                         hide_index=True, width="stretch")

    horas = total / PALABRAS_POR_MINUTO / 60
    # 7. Costo
    st.header(t("7. Costo", "7. Cost"))
    st.write(t(f"Este texto dura unos **{horas * 60:.1f} minutos** de audio ({total} palabras a "
               f"{PALABRAS_POR_MINUTO} palabras por minuto).",
               f"This text runs about **{horas * 60:.1f} minutes** of audio ({total} words at "
               f"{PALABRAS_POR_MINUTO} words per minute)."))
    monto = lambda x: f"{x:.0f}" if x >= 10 else f"{x:.2f}"
    dolares = lambda bajo, alto: (f"US$ {monto(bajo)}" if bajo == alto
                                  else f"US$ {monto(bajo)} {t('a', 'to')} {monto(alto)}")
    st.dataframe(pd.DataFrame([{t("plan", "plan"): nombre, t("por hora de audio", "per hour of audio"): dolares(bajo, alto),
                                t("este texto", "this text"): dolares(bajo * horas, alto * horas)}
                               for nombre, bajo, alto in precios()]), hide_index=True, width="stretch")
    st.caption(t("El primer título de hasta 30 minutos, como piloto, cuesta US$ 80.",
                 "The first title of up to 30 minutes, as a pilot, costs US$ 80."))
else:
    st.info(t("Presiona **Armar plan con IA** para ver el plan de narración.",
              "Press **Build plan with AI** to see the narration plan."))

st.divider()
st.caption("[github.com/susan-montalvo/quriosa-startup](https://github.com/susan-montalvo/quriosa-startup)")
