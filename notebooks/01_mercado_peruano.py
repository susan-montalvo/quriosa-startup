"""Análisis de los audiolibros registrados con ISBN en el Perú (2015-2025).

Fuente: data/audiolibros_isbn_peru_2015_2025.csv, catálogo ISBN de la Biblioteca
Nacional del Perú (BNP). Los hallazgos principales están en docs/mercado/04-panorama-mercado-audiolibros.md.

Uso: python3 notebooks/01_mercado_peruano.py
"""

import csv
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

CSV = Path(__file__).resolve().parent.parent / "data" / "audiolibros_isbn_peru_2015_2025.csv"

# Tipo de productor según la editorial que registró el ISBN.
TIPO_EDITORIAL = {
    "Penguin Random House Grupo Editorial S.A.": "Gran grupo editorial",
    "Planeta Perú S.A.": "Gran grupo editorial",
    "Santillana S.A.": "Gran grupo editorial",
    "Ministerio de Cultura": "Estado",
    "Universidad Peruana de Ciencias Aplicadas S.A.C.": "Universidad",
    "Universidad Nacional Agraria de la Selva": "Universidad",
    "Universidad Nacional de Trujillo": "Universidad",
    "Educollage S.A.C.": "Editorial pequeña o institución",
    "INFO XXI S.A.C.": "Editorial pequeña o institución",
    "Asociación Librería Editorial Salesiana": "Editorial pequeña o institución",
    "Centro de Información y Educación para la Prevención del Abuso de Drogas": "Editorial pequeña o institución",
    "Otra Mirada": "Editorial pequeña o institución",
}
# Si la editorial no está en la lista, es una persona natural: autoedición.
AUTOEDICION = "Autoedición (persona natural)"


# Variantes que la regla de clave_autor no une por sí sola.
ALIAS_AUTOR = {"palma|manuel": "palma|ricardo"}  # Manuel Ricardo Palma Soriano = Ricardo Palma


def sin_tildes(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def limpiar_autor(nombre):
    """Quita las marcas del catálogo («Ok.», «...») y espacios sobrantes."""
    nombre = re.sub(r"\.\.\.|,?\s*ok\.?$", "", nombre.strip(), flags=re.IGNORECASE)
    return nombre.strip(" ,")


def clave_autor(nombre):
    """Agrupa variantes del mismo autor: primer apellido + primer nombre.

    «Eslava Calvo, Jorge Pablo» y «Eslava, Jorge» → «eslava|jorge».
    """
    n = sin_tildes(nombre).lower()
    if "," in n:
        apellidos, nombres = [p.strip() for p in n.split(",", 1)]
        clave = f"{apellidos.split()[0]}|{nombres.split()[0] if nombres else ''}"
        return ALIAS_AUTOR.get(clave, clave)
    return n


def leer():
    with open(CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def tabla(titulo, conteo, total, encabezado, n=None):
    print(f"\n### {titulo}\n")
    print(f"| # | {encabezado} | Audiolibros | % |")
    print("|---|---|---|---|")
    for i, (k, v) in enumerate(conteo.most_common(n), 1):
        print(f"| {i} | {k} | {v} | {100 * v / total:.1f} % |")


def main():
    filas = leer()
    total = len(filas)
    titulos_unicos = len({sin_tildes(f["titulo"]).lower().strip() for f in filas})
    print(f"Registros (ISBN): {total}; títulos distintos: {titulos_unicos}")

    # Por año
    por_anio = Counter(f["anio"] for f in filas)
    print("\n### Por año\n\n| Año | Audiolibros |\n|---|---|")
    for anio in sorted(por_anio):
        print(f"| {anio} | {por_anio[anio]} |")

    # Productores
    tabla("Editoriales", Counter(f["editorial"] for f in filas), total, "Editorial")
    tipos = Counter(TIPO_EDITORIAL.get(f["editorial"], AUTOEDICION) for f in filas)
    tabla("Tipo de productor", tipos, total, "Tipo")

    # Productores por año (los 4 principales)
    top = [e for e, _ in Counter(f["editorial"] for f in filas).most_common(4)]
    print("\n### Editoriales principales por año\n")
    print("| Año | " + " | ".join(top) + " | Otras |")
    print("|---" * (len(top) + 2) + "|")
    for anio in sorted(por_anio):
        c = Counter(f["editorial"] for f in filas if f["anio"] == anio)
        otras = sum(v for k, v in c.items() if k not in top)
        print(f"| {anio} | " + " | ".join(str(c[e]) for e in top) + f" | {otras} |")

    # Categorías
    tabla("Materias (las 15 más frecuentes)", Counter(f["materia"] for f in filas), total, "Materia", 15)
    tabla("Público", Counter(f["publico"] or "(sin dato)" for f in filas), total, "Público")
    tabla("Idioma", Counter(f["idioma"] or "(sin dato)" for f in filas), total, "Idioma")

    # Autores
    variantes = defaultdict(Counter)
    sin_autor = 0
    for f in filas:
        if not f["autor"].strip():
            sin_autor += 1
            continue
        for a in f["autor"].split(";"):
            a = limpiar_autor(a)
            if a:
                variantes[clave_autor(a)][a] += 1
    autores = Counter()
    for clave, nombres in variantes.items():
        # Se muestra la variante más completa (la más larga) del nombre.
        nombre = max(nombres, key=len)
        autores[nombre] = sum(nombres.values())
    print(f"\nAutores distintos: {len(autores)}; audiolibros sin autor registrado: {sin_autor}")
    print(f"Autores con un solo audiolibro: {sum(1 for v in autores.values() if v == 1)}")
    con_3 = sum(1 for v in autores.values() if v >= 3)
    tabla("Autores con 3 o más audiolibros", autores, total, "Autor", con_3)


if __name__ == "__main__":
    main()
