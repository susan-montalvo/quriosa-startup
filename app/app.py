"""Quriosa: del texto al audiolibro narrado con voces peruanas clonadas con permiso.

    1. Texto        un fragmento de una obra de dominio público
    2. Plan         Gemini lo divide en segmentos por voz, con estilo, pausas y pronunciación (JSON estructurado)
    3. Reparto      cada personaje recibe una voz clonada con autorización escrita
    4. Narración    audio generado con las voces clonadas (Qwen3-TTS y VoxCPM2, en backend/narrar.py)
    5. Calidad      Whisper transcribe cada segmento y mide el error (WER)

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
VOCES = {
    "narrador-1": "Narrador 1 · voz femenina peruana (clonada con permiso)",
    "narrador-2": "Narrador 2 · voz masculina peruana (clonada con autorización escrita)",
}

st.set_page_config(page_title="Quriosa · audiolibros peruanos", page_icon=str(ASSETS / "icono.png"), layout="wide")


# Datos ---------------------------------------------------------------------------

@st.cache_data
def plan_demo():
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


def normal(t):
    return " ".join(t.split())


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
            "estilo": TEXTO, "estilo_tts": TEXTO, "pausa_despues_ms": {"type": "INTEGER"}},
            "required": ["n", "personaje", "texto", "texto_tts", "estilo", "estilo_tts", "pausa_despues_ms"]}},
        "pronunciacion": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "palabra": TEXTO, "como_se_dice": TEXTO, "nota": TEXTO},
            "required": ["palabra", "como_se_dice", "nota"]}},
        "erratas": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "dice": TEXTO, "probablemente": TEXTO, "contexto": TEXTO},
            "required": ["dice", "probablemente", "contexto"]}},
    },
    "required": ["personajes", "segmentos", "pronunciacion", "erratas"],
}


def armar_plan(texto, obra, autor):
    """Pide el plan a Gemini; prueba los modelos en orden. Devuelve (plan, modelo, segundos)."""
    cuerpo = {
        "systemInstruction": {"parts": [{"text": PROMPT.read_text(encoding="utf-8")}]},
        "contents": [{"parts": [{"text": f"Obra: {obra}\nAutor: {autor}\n\nFragmento:\n{texto}"}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": ESQUEMA, "temperature": 0.2},
    }
    errores = []
    limite = time.time() + ESPERA_MAXIMA
    for modelo in MODELOS:
        queda = limite - time.time()
        if queda < 5:
            errores.append("se agotó el tiempo de espera")
            break
        pedido = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent",
            data=json.dumps(cuerpo).encode(), headers={"x-goog-api-key": clave(), "Content-Type": "application/json"})
        t = time.time()
        try:
            respuesta = json.load(urllib.request.urlopen(pedido, timeout=min(30, queda)))
            partes = respuesta["candidates"][0]["content"]["parts"]
            plan = json.loads(next(p["text"] for p in reversed(partes) if "text" in p))
            return plan, modelo, time.time() - t
        except urllib.error.HTTPError as e:
            errores.append(f"{modelo}: error {e.code}")
        except Exception as e:  # tiempo agotado, respuesta incompleta…
            errores.append(f"{modelo}: {type(e).__name__}")
    raise RuntimeError("; ".join(errores))


def cobertura(texto, plan):
    """Qué parte del texto original reproducen los segmentos, unidos en orden (1.0 = todo, sin cambios)."""
    unido = " ".join(s["texto"] for s in plan["segmentos"])
    return difflib.SequenceMatcher(None, normal(texto), normal(unido), autojunk=False).ratio()


# Página -------------------------------------------------------------------------------

tema = getattr(getattr(st.context, "theme", None), "type", None)  # el logo cambia con el modo claro u oscuro
st.image(str(ASSETS / ("logo-oscuro.svg" if tema == "dark" else "logo.svg")), width=380)
st.markdown("**Convierte los libros infantiles y juveniles de las editoriales escolares peruanas en audiolibros "
            "narrados con voces peruanas clonadas con permiso.**")
st.caption("Demo con *El caballero Carmelo*, de Abraham Valdelomar (dominio público). "
           "Todas las voces son generadas con IA a partir de narradores que autorizaron su clonación.")

demo = plan_demo()
texto_demo = texto_de(demo)

# 1. Texto
st.header("1. Texto")
c1, c2 = st.columns(2)
obra = c1.text_input("Obra", demo["obra"]["titulo"])
autor = c2.text_input("Autor", demo["obra"]["autor"])
texto = st.text_area("Fragmento", texto_demo, height=220,
                     help="Pega aquí cualquier fragmento de una obra de dominio público.")
es_demo = normal(texto) == normal(texto_demo)

# 2. Plan de narración
st.header("2. Plan de narración")
st.write("Gemini lee el fragmento y devuelve un **JSON estructurado**: quién habla en cada segmento, "
         "con qué estilo, cuánta pausa dejar y qué palabras son difíciles de pronunciar.")

if st.button("✨ Armar plan con IA", type="primary"):
    if not clave():
        st.session_state.plan = (demo, "plan pregenerado", None, "No hay clave de Gemini configurada.")
    else:
        with st.spinner("Gemini está leyendo el texto…"):
            try:
                plan, modelo, segundos = armar_plan(texto, obra, autor)
                st.session_state.plan = (plan, modelo, segundos, None)
            except Exception as e:
                aviso = None if es_demo else "Este es el plan de la escena de ejemplo, no el de tu texto."
                st.session_state.plan = (demo, "plan pregenerado", None,
                                         f"Gemini no respondió ({e}). {aviso or ''}".strip())

if "plan" in st.session_state:
    plan, modelo, segundos, problema = st.session_state.plan
    if problema:
        st.warning(f"{problema} Se muestra el plan pregenerado de la escena de ejemplo.")
    else:
        st.success(f"Plan armado por **{modelo}** en {segundos:.1f} s · {len(plan['segmentos'])} segmentos · "
                   f"cobertura del texto original: **{cobertura(texto, plan):.0%}**")

    st.subheader("Segmentos")
    tabla = pd.DataFrame(plan["segmentos"])
    columnas = [c for c in ["n", "personaje", "texto", "estilo", "pausa_despues_ms"] if c in tabla]
    st.dataframe(tabla[columnas].rename(columns={"pausa_despues_ms": "pausa (ms)"}),
                 hide_index=True, width="stretch")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Personajes")
        st.dataframe(pd.DataFrame(plan["personajes"])[["id", "nombre", "descripcion", "estilo_base"]],
                     hide_index=True, width="stretch")
    with c2:
        st.subheader("Pronunciación")
        if plan.get("pronunciacion"):
            st.dataframe(pd.DataFrame(plan["pronunciacion"]), hide_index=True, width="stretch")
        else:
            st.write("Sin palabras difíciles.")
    if plan.get("erratas"):
        st.subheader("Posibles erratas (para revisión humana)")
        st.dataframe(pd.DataFrame(plan["erratas"]), hide_index=True, width="stretch")

    with st.expander("Ver el JSON completo"):
        st.json(plan)
    st.download_button("Descargar el plan (JSON)", json.dumps(plan, ensure_ascii=False, indent=2),
                       "plan-narracion.json", "application/json")

    # 3. Reparto
    st.header("3. Reparto")
    st.write("Cada personaje recibe una voz. Solo se usan voces de narradores que dieron su autorización escrita.")
    sugerido = {p["id"]: p.get("voz") for p in demo["personajes"]}
    for i, p in enumerate(plan["personajes"]):
        voz = sugerido.get(p["id"]) or ("narrador-2" if p["id"] not in ("narrador", "narradora") and i else "narrador-1")
        st.selectbox(f"{p['nombre']} ({p['id']})", list(VOCES), index=list(VOCES).index(voz),
                     format_func=VOCES.get, key=f"voz-{p['id']}")
else:
    st.info("Presiona **Armar plan con IA** para ver el plan de narración.")

# 4. Narración
st.header("4. Narración")
st.markdown("🔊 **Voz generada con IA.** Narradora: *narrador 1*. Juez: *narrador 2*.")
if not es_demo:
    st.info("Generar la voz de un texto nuevo toma varios minutos en el servidor de voz "
            "(Qwen3-TTS y VoxCPM2). Aquí se escucha la escena de ejemplo.")
st.audio(str(DEMO / "escena.mp3"))
calidad = json.loads((DEMO / "control-calidad.json").read_text(encoding="utf-8"))
with st.expander("Escuchar cada segmento"):
    for q in calidad:
        st.markdown(f"**{q['n']}. {q['personaje'].capitalize()}** · {q['texto_tts']}")
        st.audio(str(DEMO / f"segmento-{q['n']:02d}-{q['personaje']}.mp3"))
st.download_button("Descargar el audio (MP3)", (DEMO / "escena.mp3").read_bytes(),
                   "el-caballero-carmelo-cap5.mp3", "audio/mpeg")

# 5. Control de calidad
st.header("5. Control de calidad")
st.write("Whisper (large-v3-turbo) transcribe cada segmento y lo compara con el texto. Si la tasa de error "
         "de palabras (WER) supera el umbral, el segmento se vuelve a generar.")
qc = pd.DataFrame(calidad)
qc["resultado"] = qc["wer"].map(lambda w: "✅ aprobado" if w <= 0.1 else "⚠️ revisar")
st.dataframe(qc[["n", "personaje", "texto_tts", "oido", "wer", "resultado"]].rename(
    columns={"texto_tts": "texto esperado", "oido": "lo que oyó Whisper", "wer": "WER"}),
    hide_index=True, width="stretch")

# 6. Licencias de voz
st.header("6. Licencias de voz")
st.write("Cada narrador controla su voz clonada: para qué obra y personaje se usa, dónde se publica, "
         "cada audio que se generó con ella y cómo retirarla.")
licencias = json.loads((DEMO / "licencias.json").read_text(encoding="utf-8"))
for col, lic in zip(st.columns(len(licencias)), licencias.values()):
    with col.container(border=True):
        st.subheader(lic["titulo"])
        st.markdown("\n".join(f"- **{campo}:** {lic[clave_]}" for campo, clave_ in [
            ("Autorización", "autorizacion"), ("Obra", "obra"), ("Personaje", "personaje"),
            ("Dónde se usa", "donde"), ("Uso comercial", "uso_comercial"), ("Pago", "pago"),
            ("Retiro", "retiro"), ("Grabación de referencia", "referencia"), ("Modelo", "motor")]))
        usos = pd.DataFrame(lic["usos"])
        st.markdown(f"**Registro de usos:** {len(usos)} segmentos · {usos['segundos'].sum():.1f} s de audio "
                    "· todos con el aviso «Voz generada con IA»")
        st.dataframe(usos, hide_index=True, width="stretch")

st.divider()
st.caption("Quriosa · proyecto final de la Diplomatura de Ciencia de Datos de Q-Lab PUCP · "
           "[código](https://github.com/susan-montalvo/quriosa-startup)")
