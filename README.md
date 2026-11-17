# kindle-recover

`kindle_recover.py` to działające tylko odczytowo narzędzie CLI do odzyskiwania książek EPUB i MOBI z obrazu pamięci Kindle sformatowanego jako FAT32.

## Możliwości

- rozpoznawanie geometrii FAT32 bez montowania obrazu,
- kopiowanie książek nadal widocznych w katalogach FAT,
- proste odzyskiwanie skasowanych wpisów katalogowych,
- carving EPUB na podstawie struktury ZIP i obowiązkowego pliku `mimetype`,
- carving MOBI/AZW/PRC na podstawie nagłówków PalmDB i `BOOKMOBI`,
- wykrywanie szyfrowania Mobipocket/DRM,
- kontrola CRC EPUB i struktury rekordów MOBI,
- import oraz deduplikacja wyników PhotoRec,
- manifest JSON z rozmiarem, sumą SHA-256 i wynikiem weryfikacji.

Program wymaga tylko Pythona 3.9 lub nowszego. PhotoRec jest opcjonalny i przydaje się przy plikach pofragmentowanych.

## Użycie

Rozpoznanie obrazu:

```bash
python3 kindle_recover.py inspect kindle.img
python3 kindle_recover.py inspect kindle.img --json
```

Pełne odzyskiwanie:

```bash
python3 kindle_recover.py recover kindle.img -o wynik
```

Można wyłączyć poszczególne etapy:

```bash
python3 kindle_recover.py recover kindle.img -o wynik --skip-fat
python3 kindle_recover.py recover kindle.img -o wynik --skip-epub
python3 kindle_recover.py recover kindle.img -o wynik --skip-mobi
```

Sprawdzenie wcześniej odzyskanych książek i utworzenie `manifest.json`:

```bash
python3 kindle_recover.py verify recovered/books
```

Import wyników PhotoRec:

```bash
python3 kindle_recover.py import-photorec recup_dir.* -o wynik/books
```

Można też połączyć import z głównym odzyskiwaniem:

```bash
python3 kindle_recover.py recover kindle.img -o wynik \
  --photorec-results recup_dir.1 recup_dir.2
```

## Wyniki

Po komendzie `recover` powstaje struktura:

```text
wynik/
├── books/          # zweryfikowane pliki z carvingu
├── fat/            # pliki odczytane z katalogów FAT32
└── manifest.json   # SHA-256, typ, rozmiar i wynik kontroli
```

## Ograniczenia

- Źródłowy obraz nigdy nie jest naprawiany ani modyfikowany.
- Natywny carving odzyskuje EPUB tylko wtedy, gdy ZIP jest zapisany w jednym ciągłym zakresie. Pofragmentowane EPUB-y należy najpierw odzyskać PhotoRec, a potem zaimportować i zweryfikować.
- Przy skasowanym MOBI system FAT nie przechowuje już długości ostatniego rekordu. Narzędzie używa rekordu końcowego lub następnej wiarygodnej granicy i zawsze zapisuje wynik kontroli w manifeście.
- Odzyskanie pliku zabezpieczonego DRM nie usuwa szyfrowania.

## Pliki projektu

- `kindle_recover.py` — główny interfejs CLI,
- `recover_kindle_books.py` — parser i ekstraktor FAT32,
- `carve_mobi.py` — carving PalmDB/MOBI,
- `ODZYSKIWANIE_KSIAZEK.md` — raport z odzyskania badanego obrazu.
