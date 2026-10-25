# Spec-ändringar efter probe 2026-10-25 (lista "Hemmet", 55 produkter)

## Bakgrund (verifierat mot riktig amazon.se)
- Listsidan visar INTE Prime-status. Prime-information finns bara på produktsidan.
- Minst en produkt med "saknar pris" var "Currently unavailable" på produktsidan.
- Amazon kan visa priser i engelskt format ("kr115.66") beroende på språkinställning.
- Sparade sidor är korrekt UTF-8; trasiga tecken vid visning beror på webbläsaren.

## 1. Prime-status: hämtning vid behov + cache
- Listsidan är källan till pris i varje runda. Ingen produktsida hämtas för pris.
- Prime-cache per ASIN: status (prime / inte_prime / okänd) + tidpunkt för kontroll.
- Produktsida hämtas när:
  a) Priset når larmvillkoret (målpris eller procent) OCH Prime-status är
     okänd eller äldre än `prime_fresh_for_alert`.
     Larm skickas först efter kontrollen och bara om status = prime.
  b) Bakgrundsuppdatering: cache äldre än `prime_ttl`,
     högst `max_product_pages_per_round` per runda, äldst först.
- Samma väntetid (5–15 s) gäller produktsidor som listsidor.
- CAPTCHA eller HTTP-fel på produktsida: avbryt produktsidehämtningar för
  resten av rundan, låt status vara oförändrad, skicka inget larm som
  kräver kontroll.
- Produktsidans parser läser: Prime-indikator, pris, tillgänglighet.

## 2. Tillgänglighet och dolda mallar
- Produkter som är slut ska klassas som `ej_i_lager`, inte `saknar_pris`.
- Undersök i sparade page-N.html vilken markering listsidan har för
  slutsålda produkter (sv: "Inte tillgänglig för närvarande",
  en: "Currently unavailable", eller annan markup).
- Om listsidan saknar markering: produktsidan avgör vid nästa
  bakgrundsuppdatering.
- `saknar_pris` reserveras för fall där varken pris eller
  tillgänglighetsmarkering hittas.
- Listsidan innehåller dolda felmallar ("Ett fel inträffade…",
  "Amazon har stött på ett fel") vid varje produkt. Dessa ska ignoreras
  och får inte tolkas som produktfel eller blockering.

## 3. Prisformat
- Parsern ska klara alla varianter och returnera öre:
  - svenskt: "1 299,00 kr", "115,66 kr" (mellanslag/NBSP/smal NBSP som tusentalsavgränsare)
  - svenskt, valuta först: "kr217,90"
  - engelskt: "kr1,299.00", "kr115.66"
- Flera prisvarianter kan förekomma på samma rad ("217,90 kr kr217,90").
  Parsern ska ta ett pris per produkt, inte summera eller dubbelräkna.
- Tester för alla varianter samt kantfall (inga decimaler, enbart kronor).

## 4. Probe
- Rapporten ska visa Prime på listsidan som "finns ej på listsidan (förväntat)"
  