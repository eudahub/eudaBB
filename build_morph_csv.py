#!/usr/bin/env python3
"""
Buduje CSV rodzin morfologicznych z PoliMorf.

Wyjście:
  morph_families.csv    — form,lemma,family_id (4.7M wierszy)
  morph_suffixes.csv    — suffix_len,suffix,lemma,family_id (sufiksy do analogii)
  morph_indeclinable.csv — form_norm (nieodmienne: adv,prep,conj,... + jednoformowe)

- Bez normalizacji form/lematów — normalizację robi Django przy imporcie
- Zawiera tożsamości: pies,pies,1 (forma == lemat)
- Pomija nieodmienne części mowy w morph_families (trafiają do indeclinable)
- family_id: liczba całkowita unikalna per (lemat, rodzaj_morfologiczny)

Użycie:
    python3 build_morph_csv.py [wejście] [wyjście_families]
    python3 build_morph_csv.py  # używa domyślnych ścieżek
"""

import csv
import os
import subprocess
import sys
import time
import unicodedata
from collections import defaultdict

DEFAULT_INPUT  = "/home/andrzej/Downloads/Polimorf/PoliMorf-0.6.7.tab"
DEFAULT_OUTPUT = "morph_families.csv"
DEFAULT_SUFFIXES    = "morph_suffixes.csv"
DEFAULT_INDECLINABLE = "morph_indeclinable.csv"


def _normalize(text: str) -> str:
    """Mirrors board.search_index.normalize_search_text (lowercase + strip diacritics + ł→l)."""
    text = (text or "").replace("ł", "l").replace("Ł", "L")
    nfkd = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in nfkd if not unicodedata.combining(c))
    return text.lower().strip()

# Nieodmienne — pomijamy całkowicie
SKIP_POS = frozenset({
    "adv", "prep", "conj", "part", "qub", "interj",
    "burk", "comp", "aglt", "pred", "ppron3", "ppron12",
})


def adj_gender_families(number: str, gender_field: str, degree: str = "pos") -> list[str]:
    """
    Wyznacza rodziny dla przymiotnika na podstawie liczby, pola gender i stopnia.
    gender_field może mieć postać 'm1.m2.m3' lub 'n1.n2' itp.
    degree: pos / comp / sup
    Zwraca listę (może być więcej niż jedna, gdy forma jest niejednoznaczna).
    """
    genders = gender_field.split(".")
    result: set[str] = set()
    for g in genders:
        if number == "sg":
            if g.startswith("m"):
                result.add(f"adj:sg:m:{degree}")
            elif g == "f":
                result.add(f"adj:sg:f:{degree}")
            elif g.startswith("n"):
                result.add(f"adj:sg:n:{degree}")
            else:
                result.add(f"adj:sg:other:{degree}")
        else:  # pl
            if g in ("m1", "p1"):
                result.add(f"adj:pl:vir:{degree}")
            else:
                result.add(f"adj:pl:nonvir:{degree}")
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
        degree = parts[4] if len(parts) > 4 and parts[4] in ("pos", "com", "sup") else "pos"
        return adj_gender_families(parts[1], parts[3], degree)

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


def build_csv(input_path: str, output_path: str, skip_pos_out: set | None = None) -> None:
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
                if skip_pos_out is not None and tag.split(":")[0] in SKIP_POS:
                    skip_pos_out.add(_normalize(lemma))
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


def build_auxiliary_csvs(
    families_csv: str,
    suffixes_csv: str,
    indeclinable_csv: str,
    skip_pos_lemmas: set,
    suffix_lens: tuple = (2, 3, 4),
) -> None:
    """
    Czyta morph_families.csv i buduje:
      morph_suffixes.csv    — sufiksy lematów dla analogii morfologicznej
      morph_indeclinable.csv — nieodmienne słowa

    Wywołuj po build_csv (potrzebuje gotowego morph_families.csv).
    skip_pos_lemmas: znormalizowane lematy z SKIP_POS zebrane przez build_csv.
    """
    # Zbieramy (lemma_raw, family_id) → czy ma tylko formę = lemat
    identity_keys: set[tuple[str, int]] = set()     # form == lemma
    nonidentity_keys: set[tuple[str, int]] = set()  # form != lemma

    t0 = time.time()

    # ── Wczytaj nazwy rodzin ─────────────────────────────────────────────────
    family_name_map: dict[int, str] = {}
    family_names_csv = families_csv.replace(".csv", "_family_names.csv")
    if os.path.exists(family_names_csv):
        with open(family_names_csv, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                family_name_map[int(row["family_id"])] = row["family_name"]
        print(f"  Nazwy rodzin: {len(family_name_map):,}", file=sys.stderr)
    else:
        print("  Uwaga: brak _family_names.csv — sufiks bez filtrowania po typie rodziny", file=sys.stderr)

    # Rodziny przymiotnikowe (adj:sg:n, adj:pl:nonvir) — zbieramy wszystkie formy,
    # żeby wybrać najkrótszą jako kotwicę sufiksu zamiast lematu.
    ADJ_FAMILIES = frozenset({"adj:sg:n:pos", "adj:pl:nonvir:pos"})
    adj_fids: set[int] = {
        fid for fid, fname in family_name_map.items() if fname in ADJ_FAMILIES
    }
    adj_family_forms: dict[tuple[str, int], set[str]] = defaultdict(set)

    print("Wczytywanie morph_families.csv (budowa suffix/indeclinable)...", file=sys.stderr)

    with open(families_csv, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader)  # pomiń nagłówek
        for row in reader:
            if len(row) < 3:
                continue
            form, lemma, fid = row[0], row[1], int(row[2])
            key = (lemma, fid)
            if form == lemma:
                identity_keys.add(key)
            else:
                nonidentity_keys.add(key)
            if fid in adj_fids:
                adj_family_forms[key].add(form)

    # Rodziny wieloformowe z tożsamością (subst z mianownikiem = lemat)
    multi_with_identity = identity_keys & nonidentity_keys
    print(f"  Rodzin subst z tożsamością i >1 formą: {len(multi_with_identity):,}", file=sys.stderr)
    print(f"  Rodzin przymiotnikowych (adj:sg:n+pl:nonvir): {len(adj_family_forms):,}", file=sys.stderr)

    # ── Buduj morph_suffixes.csv ─────────────────────────────────────────────
    # subst:sg — kotwica = lemat (forma mianownikowa)
    # adj:sg:n / adj:pl:nonvir — kotwica = najkrótsza forma (mianownik nijaki/niemęskoos.)
    suffix_count = 0
    with open(suffixes_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["suffix_len", "suffix", "lemma", "family_id"])

        for (lemma, fid) in multi_with_identity:
            if family_name_map and family_name_map.get(fid) != "subst:sg":
                continue
            anchor_norm = _normalize(lemma)   # dla subst kotwica = lemat
            for slen in suffix_lens:
                if len(anchor_norm) <= slen:
                    continue
                w.writerow([slen, anchor_norm[-slen:], lemma, fid])
                suffix_count += 1

        for (lemma, fid), forms in adj_family_forms.items():
            if not forms:
                continue
            # najkrótsza forma ≈ mianownik (niebieskie, białe, dobre …)
            anchor_norm = _normalize(min(forms, key=len))
            for slen in suffix_lens:
                if len(anchor_norm) <= slen:
                    continue
                w.writerow([slen, anchor_norm[-slen:], lemma, fid])
                suffix_count += 1

    print(f"Suffix CSV: {suffix_count:,} wierszy (subst:sg + adj:sg:n/pl:nonvir) → {suffixes_csv}", file=sys.stderr)

    # ── Buduj morph_indeclinable.csv ─────────────────────────────────────────
    # Jednoformowe rzeczowniki/przymiotniki: tylko forma = lemat, brak innych
    identity_lemmas = {_normalize(lemma) for (lemma, fid) in identity_keys}
    nonidentity_lemmas = {_normalize(lemma) for (lemma, fid) in nonidentity_keys}
    indeclinable_nouns = identity_lemmas - nonidentity_lemmas

    all_indeclinable = indeclinable_nouns | skip_pos_lemmas

    with open(indeclinable_csv, "w", encoding="utf-8") as f:
        f.write("form_norm\n")
        for word in sorted(all_indeclinable):
            f.write(word + "\n")

    elapsed = time.time() - t0
    print(
        f"Indeclinable CSV: {len(all_indeclinable):,} słów "
        f"({len(indeclinable_nouns):,} jednoformowe + {len(skip_pos_lemmas):,} SKIP_POS) "
        f"→ {indeclinable_csv}  [{elapsed:.1f}s]",
        file=sys.stderr,
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input",   nargs="?", default=DEFAULT_INPUT,    help="PoliMorf .tab")
    parser.add_argument("output",  nargs="?", default=DEFAULT_OUTPUT,   help="morph_families.csv")
    parser.add_argument("--aux-only", action="store_true",
                        help="Pomiń parsowanie PoliMorfa; przebuduj tylko suffix/indeclinable z gotowego families CSV")
    args = parser.parse_args()

    out_suf    = DEFAULT_SUFFIXES
    out_indecl = DEFAULT_INDECLINABLE

    if args.aux_only:
        print(f"Families CSV:  {args.output}", file=sys.stderr)
        print(f"Suffixes CSV:  {out_suf}", file=sys.stderr)
        print(f"Indeclinable:  {out_indecl}", file=sys.stderr)
        build_auxiliary_csvs(args.output, out_suf, out_indecl, skip_pos_lemmas=set())
    else:
        print(f"Wejście:       {args.input}", file=sys.stderr)
        print(f"Families CSV:  {args.output}", file=sys.stderr)
        print(f"Suffixes CSV:  {out_suf}", file=sys.stderr)
        print(f"Indeclinable:  {out_indecl}", file=sys.stderr)
        skip_pos_lemmas: set[str] = set()
        build_csv(args.input, args.output, skip_pos_out=skip_pos_lemmas)
        build_auxiliary_csvs(args.output, out_suf, out_indecl, skip_pos_lemmas)
