"""Narra una escena a partir de su plan de narración, con voces clonadas y control de calidad.

    plan de narración (JSON) -> Qwen3-TTS clona cada voz -> un audio por segmento
    -> Whisper transcribe cada segmento y mide el WER -> regenera los malos -> escena completa (WAV)

Basado en sessions/15-texto-a-reel/reel.py del curso (d2cml-ai/Data-Science-Python).

Cada voz va en voces/<nombre>/ con un audio de 10 a 25 s y su transcripción en texto.txt.
voces/ está en .gitignore: las voces de referencia y los audios generados no se publican
sin autorización escrita.

Uso:
    python backend/narrar.py data/plan-narracion-carmelo-cap5.json --corta
    python backend/narrar.py data/plan-narracion-carmelo-cap5.json --corta --motor voxcpm2
    python backend/narrar.py data/plan-narracion-carmelo-cap5.json --motor voxcpm2 --solo 2
"""
import argparse
import json
import re
import subprocess
import time
import unicodedata
from pathlib import Path

import imageio_ffmpeg
import jiwer
import numpy as np
import soundfile as sf
from num2words import num2words

RAIZ = Path(__file__).resolve().parent.parent
VOCES = RAIZ / "voces"
SALIDA = VOCES / "generados"
TTS = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
ASR = "large-v3-turbo"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
EXTENSIONES = (".wav", ".oga", ".ogg", ".opus", ".m4a", ".mp3")
INICIO, FINAL = 0.3, 1.0   # segundos de silencio al inicio y al final de la escena
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


def cargar_audio(ruta, sr=16000):
    """Cualquier audio (wav, ogg de WhatsApp, m4a, mp3) a mono float32 con ffmpeg."""
    crudo = subprocess.run([FFMPEG, "-v", "error", "-i", str(ruta), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                           capture_output=True, check=True).stdout
    return np.frombuffer(crudo, np.float32)


def a16k(w, sr):
    w = np.asarray(w, np.float32)
    return np.interp(np.arange(0, len(w), sr / 16000), np.arange(len(w)), w).astype(np.float32)


# Voces clonadas --------------------------------------------------------------

def referencia(nombre):
    """Audio de referencia (16 kHz) y su transcripción, de voces/<nombre>/."""
    carpeta = VOCES / nombre
    audios = [p for p in sorted(carpeta.iterdir()) if p.suffix.lower() in EXTENSIONES]
    texto = carpeta / "texto.txt"
    if not audios or not texto.exists():
        raise SystemExit(f"Falta el audio o texto.txt en {carpeta}")
    audio = np.clip(cargar_audio(audios[0]), -1, 1)
    return audio, texto.read_text(encoding="utf-8").strip(), audios[0].name


class Voces:
    """Un solo modelo de voz y una referencia por cada narrador.

    motor="qwen":    Qwen3-TTS clona la voz, pero no acepta indicaciones de estilo.
    motor="voxcpm2": VoxCPM2 (OpenBMB, Apache 2.0) clona la voz y sigue un estilo escrito
                     entre paréntesis antes de la frase: «(estilo)frase».
    """

    def __init__(self, nombres, motor="qwen", device="cpu"):
        import torch

        torch.manual_seed(0)
        self.motor = motor
        refs = {n: referencia(n) for n in nombres}
        if motor == "qwen":
            from qwen_tts import Qwen3TTSModel

            self.tts = Qwen3TTSModel.from_pretrained(TTS, device_map=device, dtype=torch.float32)
            self.prompts = {n: self.tts.create_voice_clone_prompt(ref_audio=(a, 16000), ref_text=t)
                            for n, (a, t, _) in refs.items()}
        elif motor == "voxcpm2":
            import tempfile

            from voxcpm import VoxCPM

            self.tts = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False, optimize=False, device=device)
            self.sr = self.tts.tts_model.sample_rate
            tmp = Path(tempfile.mkdtemp())
            self.refs = {}
            for n, (a, _, _) in refs.items():
                self.refs[n] = tmp / f"{n}.wav"
                sf.write(self.refs[n], a, 16000)
        else:
            raise SystemExit(f"Motor desconocido: {motor}")
        for n, (a, _, archivo) in refs.items():
            log(f"voz «{n}» ({motor}) clonada desde {archivo} ({len(a) / 16000:.1f} s)")

    def decir(self, nombre, frases, estilos=None):
        if self.motor == "qwen":   # Qwen3-TTS no acepta estilo al clonar
            wavs, self.sr = self.tts.generate_voice_clone(text=frases, language=["Spanish"] * len(frases),
                                                          voice_clone_prompt=self.prompts[nombre])
            return [np.asarray(w, np.float32) for w in wavs]
        estilos = estilos or [None] * len(frases)
        return [np.asarray(self.tts.generate(text=f"({e}){f}" if e else f, reference_wav_path=str(self.refs[nombre]),
                                             cfg_value=2.0, inference_timesteps=10), np.float32)
                for f, e in zip(frases, estilos)]


# Control de calidad ------------------------------------------------------------

_ASR = None


def whisper():
    global _ASR
    if _ASR is None:
        from faster_whisper import WhisperModel
        _ASR = WhisperModel(ASR, device="cpu", compute_type="int8")
    return _ASR


def normalizar(s):
    """Para comparar lo escrito con lo que oyó Whisper: números en palabras, sin tildes ni puntuación."""
    s = re.sub(r"\d+", lambda m: num2words(int(m.group()), lang="es"), s.lower())
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def oir(w, sr):
    return " ".join(s.text.strip() for s in whisper().transcribe(a16k(w, sr), language="es")[0])


def wer(texto, oido):
    return jiwer.wer(normalizar(texto), normalizar(oido)) if normalizar(oido) else 1.0


# Escena ------------------------------------------------------------------------

def narrar(plan, corta=False, motor="qwen", solo=None, max_wer=None, reintentos=None):
    qc = plan.get("control_calidad", {})
    max_wer = qc.get("max_wer", 0.25) if max_wer is None else max_wer
    reintentos = qc.get("reintentos", 1) if reintentos is None else reintentos
    voz_de = {p["id"]: p["voz"] for p in plan["personajes"]}
    segs = [s for s in plan["segmentos"] if (s.get("version_corta") or not corta) and (not solo or s["n"] in solo)]
    voces = Voces(sorted({voz_de[s["personaje"]] for s in segs}), motor=motor)

    wavs = {}
    for intento in range(reintentos + 1):
        pendientes = [s for s in segs if s["n"] not in wavs or s["wer"] > max_wer]
        if not pendientes:
            break
        if intento:
            log(f"reintento {intento}: segmentos {[s['n'] for s in pendientes]}")
        for nombre in sorted({voz_de[s["personaje"]] for s in pendientes}):
            grupo = [s for s in pendientes if voz_de[s["personaje"]] == nombre]
            frases = [s["texto_tts"] for s in grupo]
            estilos = [s.get("estilo_tts") or s.get("estilo") for s in grupo]
            for s, w in zip(grupo, voces.decir(nombre, frases, estilos)):
                oido = oir(w, voces.sr)
                if s["n"] not in wavs or wer(s["texto_tts"], oido) < s["wer"]:
                    wavs[s["n"]], s["oido"], s["wer"] = w, oido, wer(s["texto_tts"], oido)
                log(f"segmento {s['n']:2d} ({nombre}): {len(w) / voces.sr:4.1f} s, WER {s['wer']:.2f}")

    sr = voces.sr
    partes = [np.zeros(int(INICIO * sr), np.float32)]
    for i, s in enumerate(segs):
        partes.append(wavs[s["n"]])
        pausa = FINAL if i == len(segs) - 1 else s.get("pausa_despues_ms", 400) / 1000
        partes.append(np.zeros(int(pausa * sr), np.float32))
    return np.concatenate(partes), sr, segs, wavs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", type=Path)
    ap.add_argument("--corta", action="store_true", help="solo los segmentos con version_corta: true")
    ap.add_argument("--motor", choices=["qwen", "voxcpm2"], default="qwen",
                    help="qwen: clona sin estilo; voxcpm2: clona y sigue el estilo de cada segmento")
    ap.add_argument("--solo", type=int, nargs="+", help="generar solo estos segmentos (para probar)")
    args = ap.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    audio, sr, segs, wavs = narrar(plan, corta=args.corta, motor=args.motor, solo=args.solo)

    nombre = args.plan.stem.replace("plan-narracion-", "") + ("-corta" if args.corta else "")
    nombre += "" if args.motor == "qwen" else f"-{args.motor}"
    nombre += f"-prueba-{'-'.join(map(str, args.solo))}" if args.solo else ""
    carpeta = SALIDA / nombre
    carpeta.mkdir(parents=True, exist_ok=True)
    sf.write(carpeta / f"{nombre}.wav", audio, sr)
    for s in segs:
        sf.write(carpeta / f"segmento-{s['n']:02d}-{s['personaje']}.wav", wavs[s["n"]], sr)
    informe = [{k: s[k] for k in ("n", "personaje", "texto_tts", "oido", "wer")} for s in segs]
    (carpeta / "control-calidad.json").write_text(json.dumps(informe, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"escena: {len(audio) / sr:.1f} s -> {carpeta / (nombre + '.wav')}")


if __name__ == "__main__":
    main()
