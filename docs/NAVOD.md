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
