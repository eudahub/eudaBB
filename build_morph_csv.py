#!/usr/bin/env python3
"""
Buduje CSV rodzin morfologicznych z PoliMorf.

Wyjście: form,lemma,family_id
- Bez normalizacji (małe litery, diakrytyki, ł) — to robi Django przy imporcie
- Zawiera tożsamości: pies,pies,1 (forma == lemat)
- Pomija nieodmienne części mowy
- family_id: liczba całkowita unikalna per (lemat, rodzaj_morfologiczny)

Użycie:
    python3 build_morph_csv.py [wejście] [wyjście]
    python3 build_morph_csv.py  # używa domyślnych ścieżek
"""

import csv
import os
import subprocess
import sys
import time

DEFAULT_INPUT  = "/home/andrzej/Downloads/Polimorf/PoliMorf-0.6.7.tab"
DEFAULT_OUTPUT = "morph_families.csv"

# Nieodmienne — pomijamy całkowicie
SKIP_POS = frozenset({
    "adv", "prep", "conj", "part", "qub", "interj",
    "burk", "comp", "aglt", "pred", "ppron3", "ppron12",
})


def adj_gender_families(number: str, gender_field: str) -> list[str]:
    """
    Wyznacza rodziny dla przymiotnika na podstawie liczby i pola gender.
    gender_field może mieć postać 'm1.m2.m3' lub 'n1.n2' itp.
    Zwraca listę (może być więcej niż jedna, gdy forma jest niejednoznaczna).
    """
    genders = gender_field.split(".")
    result: set[str] = set()
    for g in genders:
        if number == "sg":
            if g.startswith("m"):
                result.add("adj:sg:m")
            elif g == "f":
                result.add("adj:sg:f")
            elif g.startswith("n"):
                result.add("adj:sg:n")
            else:
                result.add("adj:sg:other")
        else:  # pl
            if g in ("m1", "p1"):
                result.add("adj:pl:vir")     # męskoosobowy
            else:
                result.add("adj:pl:nonvir")  # niemęskoosobowy
    return sorted(result)


def tag_to_families(tag: str) -> list[str]:
    """
    Mapuje tag PoliMorf na listę nazw rodzin.
    Pusta lista = pomiń (nieodmienna lub nieobsługiwana).
    """
    parts = tag.split(":")
    pos = parts[0]

    if pos in SKIP_POS:
        return []

    # ── Rzeczowniki ──────────────────────────────────────────────
    if pos in ("subst", "depr"):
        number = parts[1] if len(parts) > 1 else "sg"
        # depr to deprecativus pluralis → traktujemy jak subst:pl
        return [f"subst:{number}"]

    # ── Przymiotniki odmienne ─────────────────────────────────────
    if pos == "adj":
        # adj:number:case:gender:degree
        # (case może mieć kropki np. nom.voc — nas interesuje gender = parts[3])
        if len(parts) < 4:
            return ["adj:other"]
        return adj_gender_families(parts[1], parts[3])

    # adja (przymiotnik atrybutywny, nieokreślona forma), adjp, adjc
    # — jedna forma per lemat, ale warto je mieć w tabeli
    if pos in ("adja", "adjp", "adjc"):
        return ["adj:invar"]

    # ── Czasowniki — formy rdzenne ────────────────────────────────
    if pos in ("fin", "praet", "inf", "imps", "pcon", "pant"):
        # fin  = odmiana przez osoby/liczby
        # praet = czas przeszły
        # inf  = bezokolicznik
        # imps = forma nieosobowa (biegano)
        # pcon = imiesłów współczesny (biegając)
        # pant = imiesłów uprzedni (zrobiwszy)
        return ["verb:core"]

    if pos == "impt":
        return ["verb:impt"]

    # Imiesłów czynny (biegający): rozbijamy na aff/neg
    if pos == "pact":
        neg_aff = parts[-1] if parts[-1] in ("neg", "aff") else "aff"
        return [f"pact:{neg_aff}"]

    # Imiesłów bierny (robiony): rozbijamy na aff/neg
    if pos == "ppas":
        neg_aff = parts[-1] if parts[-1] in ("neg", "aff") else "aff"
        return [f"ppas:{neg_aff}"]

    # Rzeczownik odsłowny (bieganie): rozbijamy na aff/neg
    if pos == "ger":
        neg_aff = parts[-1] if parts[-1] in ("neg", "aff") else "aff"
        return [f"ger:{neg_aff}"]

    # Winien/winna/winno — odmiana przez rodzaj jak przymiotnik
    if pos == "winien":
        # winien:number:gender:aspect
        if len(parts) >= 3:
            return adj_gender_families(parts[1], parts[2])
        return ["winien"]

    # Będzie, bedzie — formy czasownika "być" w czasie przyszłym
    if pos == "bedzie":
        return ["verb:core"]

    # Liczebniki — jedna rodzina per lemat
    if pos in ("num", "numcol"):
        return ["num"]

    # Pozostałe odmieniające się (np. nieznane przyszłe tagi)
    return [pos]


def build_csv(input_path: str, output_path: str) -> None:
    # (lemat, nazwa_rodziny) → family_id (int)
    family_id_map: dict[tuple[str, str], int] = {}
    next_id = 0

    def get_id(lemma: str, family_name: str) -> int:
        nonlocal next_id
        key = (lemma, family_name)
        if key not in family_id_map:
            next_id += 1
            family_id_map[key] = next_id
        return family_id_map[key]

    # Piszemy BEZ dedupl do pliku tymczasowego — dedupl przez sort -u
    # (seen set dla 6.5M wierszy zużywałby ~800MB RAM)
    raw_path = output_path + ".raw"
    written = 0
    skipped = 0
    t0 = time.time()

    with (
        open(input_path, encoding="utf-8") as fin,
        open(raw_path, "w", encoding="utf-8", newline="") as fout,
    ):
        writer = csv.writer(fout)

        for lineno, raw in enumerate(fin, 1):
            if lineno % 500_000 == 0:
                elapsed = time.time() - t0
                print(
                    f"  {lineno:,} wierszy, {written:,} wpisów, {elapsed:.0f}s",
                    file=sys.stderr,
                )

            parts = raw.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue

            form, lemma, tag = parts[0], parts[1], parts[2]

            families = tag_to_families(tag)
            if not families:
                skipped += 1
                continue

            for family_name in families:
                fid = get_id(lemma, family_name)
                writer.writerow([form, lemma, fid])
                written += 1

    print(f"\nParsowanie: {written:,} wpisów (z dup.), {skipped:,} pominięto",
          file=sys.stderr)

    # sort -u: leksykograficznie + deduplikacja (~30% redukcja duplikatów)
    print("Sortowanie i deduplikacja (sort -u)...", file=sys.stderr)
    sorted_path = output_path + ".sorted"
    subprocess.run(
        ["sort", "-u", raw_path, "-o", sorted_path],
        check=True,
    )
    os.unlink(raw_path)

    # Dodaj nagłówek
    with (
        open(output_path, "w", encoding="utf-8") as fout,
        open(sorted_path, encoding="utf-8") as fsort,
    ):
        fout.write("form,lemma,family_id\n")
        for chunk in iter(lambda: fsort.read(1 << 20), ""):
            fout.write(chunk)
    os.unlink(sorted_path)

    elapsed = time.time() - t0
    final_rows = sum(1 for _ in open(output_path, encoding="utf-8")) - 1  # bez nagłówka
    print(f"Unikalnych wierszy: {final_rows:,}", file=sys.stderr)
    print(f"Unikalnych rodzin: {len(family_id_map):,}", file=sys.stderr)
    print(f"Czas łącznie: {elapsed:.1f}s", file=sys.stderr)

    # Zapisz mapowanie family_id → (lemat, nazwa) dla debugowania
    debug_path = output_path.replace(".csv", "_family_names.csv")
    with open(debug_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["family_id", "lemma", "family_name"])
        for (lemma, fname), fid in sorted(family_id_map.items(), key=lambda x: x[1]):
            w.writerow([fid, lemma, fname])
    print(f"Mapa rodzin: {debug_path}", file=sys.stderr)


if __name__ == "__main__":
    inp = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INPUT
    out = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUTPUT
    print(f"Wejście:  {inp}", file=sys.stderr)
    print(f"Wyjście:  {out}", file=sys.stderr)
    build_csv(inp, out)
