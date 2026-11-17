# Odzyskiwanie książek z obrazu Kindle

## Cel i wynik

Źródłem był obraz pamięci Kindle:

```text
/home/marcin/kindle-recovery/kindle.img
```

Obraz był analizowany wyłącznie w trybie odczytu. Odzyskane książki zostały zapisane w:

```text
/home/marcin/kindle-recovery/recovered/books
```

Ostatecznie odzyskano:

- 6 plików MOBI,
- 2 pliki EPUB.

Dwa pliki MOBI są słownikami zabezpieczonymi DRM. Ich dane udało się odzyskać, ale do otwarcia mogą wymagać właściwego konta lub urządzenia Kindle.

## 1. Rozpoznanie obrazu

Najpierw sprawdziłem typ pliku i geometrię obrazu:

```bash
file kindle.img
fdisk -l kindle.img
blkid -p kindle.img
```

Wynik pokazał, że `kindle.img` nie zawiera zestawu osobnych partycji. Cały obraz jest jednym systemem plików FAT32 o etykiecie `Kindle`:

```text
FAT32
rozmiar sektora: 512 B
rozmiar klastra: 8192 B
obszar danych od bajtu: 8884224
```

Następnie sprawdziłem spójność FAT32 bez dokonywania napraw:

```bash
fsck.fat -n -v kindle.img
```

Opcja `-n` jest istotna — zabrania zapisywania zmian do obrazu. Kontrola wykazała ustawiony znacznik nieprawidłowego odmontowania, ale struktura systemu plików była czytelna.

## 2. Odczyt zachowanych katalogów FAT32

Ponieważ zamontowanie obrazu nie było konieczne, napisałem prosty ekstraktor FAT32 działający bezpośrednio na pliku obrazu:

```text
recover_kindle_books.py
```

Skrypt:

1. odczytuje sektor startowy FAT32,
2. wylicza położenie tablic FAT i obszaru danych,
3. przechodzi po łańcuchach klastrów,
4. odczytuje katalogi oraz długie nazwy VFAT,
5. wyszukuje rozszerzenia `.epub`, `.mobi`, `.azw`, `.azw3` i `.prc`,
6. próbuje także odczytać proste, skasowane wpisy katalogowe.

Uruchomienie:

```bash
python3 recover_kindle_books.py kindle.img recovered/fat_extract
```

W zachowanej strukturze katalogów znalazł się tylko plik:

```text
._Społeczeństwo zmęczenia i inne eseje.epub
```

Miał 4096 bajtów i był plikiem metadanych AppleDouble utworzonym przez macOS, a nie właściwą książką. Oznaczało to, że same wpisy katalogowe FAT nie wystarczały do odzyskania treści.

## 3. Skanowanie nieprzydzielonego miejsca

Kolejnym krokiem był skan sygnaturowy przy użyciu PhotoRec 7.2. Przeskanowałem nieprzydzieloną przestrzeń FAT32, czyli klastry oznaczone jako wolne po usunięciu plików.

W PhotoRec zostały wybrane:

```text
obraz: kindle.img
system plików: Other (FAT/NTFS/HFS+/...)
zakres: Free
```

PhotoRec odzyskał wiele elementów pamięci podręcznej Kindle, przede wszystkim miniatury JPG, ale wśród wyników znalazł także dwa pliki EPUB:

```text
recup_dir.1/f0029208.epub
recup_dir.1/f0038328.epub
```

EPUB jest archiwum ZIP o określonej strukturze. Oba pliki zostały sprawdzone poleceniami:

```bash
unzip -t recup_dir.1/f0029208.epub
unzip -t recup_dir.1/f0038328.epub

unzip -p recup_dir.1/f0029208.epub mimetype
unzip -p recup_dir.1/f0038328.epub mimetype
```

W obu przypadkach test archiwum zakończył się bez błędów, a plik `mimetype` zawierał:

```text
application/epub+zip
```

Tytuły i autorów odczytałem z plików OPF wskazanych przez `META-INF/container.xml`. Na tej podstawie pliki otrzymały nazwy:

```text
Maria Krüger - Karolcia.epub
Joe Dispenza - Jak przeprogramować swój mózg.epub
```

## 4. Wyszukanie kontenerów MOBI

PhotoRec nie oznaczył plików MOBI właściwym rozszerzeniem. Dlatego cały obraz został przeszukany pod kątem wewnętrznej sygnatury kontenera Mobipocket:

```bash
rg -aob 'BOOKMOBI' kindle.img
```

Znaleziono dziewięć wystąpień `BOOKMOBI`. Nie każde wystąpienie musi być początkiem osobnego pliku — sygnatura może znajdować się również wewnątrz większego lub częściowo nadpisanego obiektu.

Do wydobycia poprawnych kontenerów powstał skrypt:

```text
carve_mobi.py
```

Skrypt wykonuje carving, czyli odzyskuje pliki na podstawie ich struktury zamiast nazw katalogowych. Dla każdego wystąpienia `BOOKMOBI`:

1. cofa się o 60 bajtów do początku nagłówka Palm Database,
2. sprawdza liczbę i kolejność rekordów PalmDB,
3. potwierdza obecność nagłówka `MOBI` w pierwszym rekordzie,
4. odczytuje tytuł, informacje o szyfrowaniu i tablicę rekordów,
5. ustala koniec kontenera na podstawie rekordu końcowego lub następnej wiarygodnej granicy,
6. zapisuje wynik z rozszerzeniem `.mobi`.

Uruchomienie:

```bash
python3 carve_mobi.py kindle.img recovered/books
```

Spośród dziewięciu surowych sygnatur sześć miało poprawny nagłówek PalmDB/MOBI i spójną tablicę rekordów. Odzyskano:

```text
Energia, nie czas. Nawyki, które chronią przed wypaleniem zawodowym i ciągłym przemęczeniem pracą.mobi
How to be enough. Wyłącz samokrytykę, ogranicz perfekcjonizm i żyj w zgodzie ze sobą.mobi
Jak przeprogramować swój mózg.mobi
Karolcia.mobi
Oxford Hachette French-English Dictionary.mobi
现代汉英词典.mobi
```

Dwa ostatnie pliki są słownikami z szyfrowaniem Mobipocket typu 2 (DRM). Pozostałe cztery kontenery nie zgłaszały szyfrowania.

## 5. Weryfikacja odzyskanych plików

EPUB-y zostały sprawdzone przez pełny test archiwum:

```bash
for f in recovered/books/*.epub; do
    unzip -t "$f"
done
```

Wszystkie wpisy wewnątrz obu archiwów przeszły kontrolę CRC.

Pliki MOBI zostały rozpoznane przez `file` jako książki Mobipocket:

```bash
file recovered/books/*.mobi
```

Metadane każdego kontenera odczytałem także programem Calibre:

```bash
for f in recovered/books/*.mobi; do
    ebook-meta "$f"
done
```

`ebook-meta` poprawnie odczytał tytuły, autorów, języki i — tam, gdzie były dostępne — identyfikatory ISBN lub ASIN. Była to dodatkowa kontrola, że nagłówki oraz tablice rekordów odzyskanych kontenerów są czytelne.

Na końcu obliczyłem sumy SHA-256 plików EPUB:

```text
6ae5f4c004cb8d5ec1f0a2ec86802a7c571aede68ce195efb4bc9b1bebef4443
  Joe Dispenza - Jak przeprogramować swój mózg.epub

4fc5c7d20c6867d293b556bf188c09fa1e1a01e01d3e1c29f3543f93af7ac84e
  Maria Krüger - Karolcia.epub
```

## Ważne uwagi

- `kindle.img` nie został zmodyfikowany.
- Nie należy uruchamiać `fsck.fat` bez opcji `-n` na jedynej kopii obrazu.
- Carving zwykle nie zachowuje pierwotnych nazw ani ścieżek. Nazwy zostały odtworzone z metadanych książek.
- Poprawne odzyskanie zaszyfrowanego pliku nie usuwa DRM.
- Katalogi `recup_dir.*` zawierają surowe wyniki PhotoRec, głównie obrazy i pliki pamięci podręcznej. Gotowe książki znajdują się wyłącznie w `recovered/books`.

## Pliki pomocnicze

- `recover_kindle_books.py` — odczyt i proste odzyskiwanie wpisów FAT32,
- `carve_mobi.py` — wyszukiwanie i carving kontenerów PalmDB/MOBI,
- `ODZYSKIWANIE_KSIAZEK.md` — niniejszy raport.
