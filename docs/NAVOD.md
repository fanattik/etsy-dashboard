# Etsy Dashboard: návod (Mac)

Aplikace v prohlížeči, která hlídá tvoje Etsy shopy: nové objednávky, co je potřeba odeslat,
tržby, poplatky Etsy, výplaty na banku a zůstatek. Běží na pozadí u tebe na Macu,
takže stačí otevřít v prohlížeči **http://127.0.0.1:8765**. Data nikam neodcházejí
(jen se stahují z Etsy).

## 1. Instalace (jednou)
1. Stáhni [Etsy-Dashboard-mac.zip](https://github.com/fanattik/etsy-dashboard/raw/main/dist/Etsy-Dashboard-mac.zip),
   rozbal ho a přetáhni **Etsy Dashboard** do složky **Aplikace**.
2. Otevři ji dvojklikem. macOS napoprvé ukáže, že aplikaci nemůže ověřit (není podepsaná
   u Applu). Klikni **Hotovo**, pak otevři **Nastavení systému → Soukromí a zabezpečení**,
   sjeď dolů a u „Etsy Dashboard“ klikni **Přesto otevřít** a potvrď heslem.
   Tohle se dělá jen jednou.
3. Pokud na Macu chybí Python, aplikace nabídne jeho stažení z python.org
   („macOS installer“). Nainstaluj ho a otevři Etsy Dashboard znovu.
4. Otevře se prohlížeč s dashboardem.

Od té doby dashboard běží sám na pozadí po každém zapnutí Macu. Dashboard otevřeš ikonou
Etsy Dashboard (Launchpad, Dock, Spotlight) nebo záložkou http://127.0.0.1:8765.

## 2. Data z Etsy: nahrání CSV (funguje hned, bez schvalování)
1. Na Etsy otevři **Shop Manager → Settings → Options → Download Data**.
2. Stáhni **Payment Account** CSV (měsíční výpis) za měsíce, které chceš vidět.
   Pro jména zákazníků, přesné položky a stav odeslání stáhni i **Orders**, **Order Items**
   a případně **Payments** (EtsyDirectCheckoutPayments). Na pořadí nahrávání nezáleží.
   Pro stránku Listingy stáhni **Currently for Sale Listings** (EtsyListingsDownload.csv).
   Zobrazení a oblíbené v něm nejsou, ty doplní až Etsy API.
3. V dashboardu klikni **⬆ Nahrát CSV z Etsy**, vyber shopu a soubory přetáhni do okna.
   Každou shopu nahrávej zvlášť (soubory z Etsy neříkají, ke které shopě patří).
   Nahrát stejný soubor znovu nevadí, nic se nezdvojí.

Z CSV nejde zjistit aktuální zůstatek na Etsy a u objednávek jen z výpisu ani stav odeslání.
Nová data se neobjeví sama, musíš je znovu stáhnout a nahrát.

## 3. Automatické hlídání přes Etsy API (až Etsy schválí aplikaci)
1. Otevři https://www.etsy.com/developers/register a zaregistruj aplikaci
   (název např. „My Dashboard“, osobní použití).
2. V detailu aplikace (Your apps → Manage) najdeš **Keystring** a **Shared secret**.
3. Do **Callback URLs** přidej přesně: `https://localhost:3003/etsy`
4. Až Etsy aplikaci schválí: v dashboardu klikni **Nastavení**, vlož Keystring a Shared secret a ulož.
5. Klikni **+ Přihlásit shopu**. Na Etsy se přihlas a potvrď přístup. Prohlížeč pak ukáže
   chybu „nelze se připojit“, to je v pořádku: zkopíruj celou adresu z adresního řádku,
   vlož ji do okénka v Nastavení a klikni **Dokončit přihlášení**.
6. Pro druhou shopu se na Etsy odhlas (nebo otevři odkaz v anonymním okně) a zopakuj krok 5.

Při prvním propojení se stáhne historie za poslední rok a nahradí data z CSV
(shopa se musí v CSV jmenovat stejně jako na Etsy).

## 4. Nové listingy přes Etsy API
Na stránce **Listingy** klikni **+ Nový listing**.
1. Shopy přihlášené před verzí 1.15 mají jen čtecí právo. V Nastavení je jednou přihlas znovu
   (+ Přihlásit shopu), Etsy se zeptá i na právo upravovat listingy.
2. Klikni **Načíst složku s produkty** a vyber složku, kde má každý produkt vlastní podsložku
   s `etsy-listing.md` (sekce Title, Tags, Price, Description), obrázky a soubory ke stažení.
   Hlavní obrázek je `etsy-cover.jpg`. Když popis zmiňuje ZIP, nahrají se ZIPy, jinak PDF.
   Nebo klikni **Prázdný listing** a vyplň ho ručně.
3. Zkontroluj kategorii (dashboard ji jen odhadne podle názvu), cenu v měně shopy, štítky
   (max 13, každý do 20 znaků), obrázky a soubory (max 5, každý do 20 MB).
4. Zvol **Nechat jako koncept** (zdarma, zveřejníš pak na Etsy) nebo **Rovnou zveřejnit**
   (Etsy účtuje $0.20 za listing) a klikni **Vytvořit**.

U každého listingu přepneš **Digitální / Fyzický produkt**. Digitální má soubory ke stažení,
fyzický (3D tisk) profil dopravy a zpracování, které máš nastavené na Etsy.
Po výběru kategorie se ukážou **Atributy** (barva, materiál, svátek…, hvězdička = Etsy je vyžaduje)
a **Varianty**: nejvýš 2, buď vlastnost z Etsy (barva, velikost), nebo vlastní název (např. „Velikost“: S, M, L).
U každé varianty zaškrtni, jestli se podle ní liší cena, množství nebo SKU, a vyplň je v tabulce kombinací.
Kombinaci, kterou neprodáváš, odškrtni.
Atributy s dlouhým seznamem (třeba materiál) vybíráš v rozbalovacím seznamu se zaškrtáváním a hledáním,
vybrané hodnoty se ukážou pod ním (křížkem je odebereš).
**Vlastní volby (personalizace)**: až 5 otázek pro kupujícího, typ Text (např. jméno), Výběr ze seznamu,
Nahrání souborů nebo Nahrání s popisky (nahrávání jen jedno na listing). Pokud tvoje shopa ještě nemá u Etsy
nový systém personalizace, uloží se jen jedno textové pole.

## 5. Úpravy, mazání, hromadné akce a slevy
Klikni na listing v tabulce. V detailu uvidíš atributy a varianty načtené z Etsy a tlačítka
**Upravit**, **Aktivovat / Deaktivovat**, **Sleva…** a **Smazat**.
- **Upravit** otevře stejný formulář jako u nového listingu: změníš název, popis, cenu, štítky,
  kategorii, obrázky, soubory, atributy i varianty a klikneš **Uložit změny**. V poli Stav
  můžeš listing rovnou aktivovat nebo deaktivovat.
- **Hromadně**: zaškrtni v tabulce víc listingů (nebo všechny v záhlaví) a nahoře se objeví lišta
  s akcemi Aktivovat, Deaktivovat, Sleva a Smazat.
- **Mazání** potřebuje nové právo. Shopy přihlášené před verzí 1.17 v Nastavení jednou přihlas znovu,
  Etsy se zeptá i na právo mazat. Smazání nejde vrátit.
- **Aktivace** konceptu nebo vypršelého listingu stojí u Etsy $0.20.
- **Sleva od–do**: zadej procenta a data (konec je včetně). Etsy API neumí vytvořit Sale ani kupon,
  takže dashboard v den začátku sníží ceny všech variant a po konci je vrátí. Na Etsy se proto
  neukáže přeškrtnutá cena a funguje to jen, když je Mac zapnutý (ceny se mění při pravidelné
  kontrole). Když cenu během slevy ručně změníš, dashboard ji na konci nepřepíše.
  Běžící nebo naplánovanou slevu zrušíš v detailu listingu.

## 6. Statistiky
Stránka **Statistiky** ukazuje za zvolené období (třeba posledních 7 dní) grafy zobrazení listingů,
objednávek, konverze a tržeb, dál nové oblíbené, sledující shopy, recenze, opakované kupující, města a země
a tabulku listingů podle zobrazení. Etsy API dává jen celkové počty zobrazení, proto si dashboard ukládá
denní stav a historie začíná dnem, kdy poprvé běžela verze 1.21. Návštěvy shopy, zdroje návštěv
a opuštěné košíky Etsy přes API nesdílí, ty zůstávají jen v Shop Manager → Stats.

## Co na dashboardu najdeš
- Přepínač shop (obě / jen jedna) a období (tento měsíc, minulý, letos, konkrétní měsíc…).
- Tržby, počet objednávek, průměrná objednávka, poplatky Etsy, čistě na účet,
  výplaty na banku, zůstatek na Etsy a **kolik objednávek čeká na odeslání**.
- Graf tržeb za 12 měsíců, nejprodávanější produkty, rozpad poplatků.
- Tabulky objednávek a výpisu s vyhledáváním a tlačítkem **CSV** (otevře se v Excelu/Numbers).
- Nové věci od poslední návštěvy jsou zvýrazněné.

## Upozornění
- Aplikace kontroluje Etsy každých 15 minut (změníš v Nastavení), pokud je Mac zapnutý.
- **🔔 Povolit upozornění**: když je dashboard otevřený v prohlížeči, přijde upozornění na novou objednávku.
- Na mobil: nainstaluj aplikaci **ntfy**, přihlas se k odběru tématu s těžko uhodnutelným
  názvem (např. `moje-shopa-etsy-8f3k2`) a stejný název zadej v Nastavení.

## Dobré vědět
- Jazyk (CZ / EN / DE) přepneš v Nastavení. Podle něj se píšou i upozornění na mobil a hlavičky CSV exportu.
- V Nastavení můžeš zvolit i měnu (např. CZK). Všechny částky se pak přepočítají aktuálním kurzem ECB, takže u starších měsíců jde o orientační hodnoty. CSV export zůstává v původní měně.
- Přihlášení shop vydrží 90 dní od posledního běhu. Když Mac 90 dní nepoběží, přihlas je znovu.
- Klíče a data jsou v `~/Library/Application Support/EtsyDashboard`. Nikomu je neposílej.
- Aktualizace se stahují samy jednou denně z tohoto repozitáře (Nastavení → Zkontrolovat aktualizace). Data zůstanou.
- Odinstalace: v dashboardu Nastavení → Odinstalovat (můžeš zaškrtnout i smazání dat),
  pak přetáhni Etsy Dashboard z Aplikací do koše.
