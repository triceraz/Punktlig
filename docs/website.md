# Nettsiden

## Hva som var galt med forrige forsøk

En tekstside med et bakgrunnsbilde. Konkret:

- **Kartet var spaghetti.** 5 645 identiske grå streker uten hierarki. T-bane, trikk og
  buss tegnet likt, så formen på Oslo som folk har i hodet forsvant i støy.
- **Det så tomt ut.** 369 prikker fordelt på 5 000 stopp leser som en ødelagt side, ikke
  som en by i bevegelse.
- **Ingenting beveget seg.** «Live» var en påstand. Prikkene pulserte på stedet.
- **Ingen wow-øyeblikk.** Den eneste sterke ideen, nedtellingen som lyver, lå i et lite
  kort i hjørnet mellom annen tekst.
- **Generisk.** Paletten kunne vært hvilken som helst dashboard-mal.

## Den ene ideen

Vi har noe ingen andre har: **arkiverte prognoser ved siden av fasiten.** Alt på siden
skal tjene den ene tingen ingen andre kan vise: *se en prognose ta feil mens det skjer.*

Alle har stått og sett nedtellingen treffe null uten at det kom noen vogn. Det er
følelsen siden skal åpne med, ikke forklare.

## Skjerm for skjerm

### 1. «Nå» — hele skjermen, ett tall

Svart. Midt på skjermen står ett tall, i en størrelse som er ubehagelig stor: nedtellingen
til en avgang som skjer akkurat nå, hentet fra Enturs egen prognose, som teller ned i
sanntid, sekund for sekund.

Under, lite og grått: `Linje 17 mot Rikshospitalet · slik Ruter-appen viser det`.

Når den treffer null, stopper den ikke. Den fortsetter i rødt: `-0:14`, `-0:38`, `-1:22`.
Og teksten under skifter til: **«Vogna er fortsatt ikke der.»**

Ingenting annet på skjermen. Ingen meny, ingen kort, ingen forklaring. Den som lander her
skjønner det på to sekunder fordi de har stått i det selv.

Teknisk: velg en avgang der modellen og Entur er uenige, med ankomst 1–6 minutter frem, så
det faktisk rekker å skje mens noen ser på. Klokka teller i nettleseren, så det er ekte
bevegelse uten å spørre serveren.

### 2. To klokker

Samme avgang. Nedtellingen glir til venstre og en ny kommer opp ved siden av, i grønt:
vår. Begge teller. Differansen står mellom dem.

Når den ene treffer null og den andre fortsatt går, er poenget gjort uten et eneste ord.
Under: én setning om at fasiten havner i arkivet om noen minutter, og at det er sånn
tallene lenger nede er målt.

### 3. Kartet, slik Oslo ser ut i hodet ditt

Dette er der forrige forsøk falt.

**Hierarki, ikke likhet.** T-banen tegnes tykk, i linjenes egne farger. Trikken tynnere,
i sine. Bussnettet er som standard **av** — det er 377 linjer og de gjør bildet til grøt.
En liten bryter slår det på som et svakt nett under, for den som vil se hele omfanget.

Dette er det viktigste enkeltvalget på hele siden: T-banekartet er en form nordmenn
kjenner igjen umiddelbart. Med riktige farger sier bildet «Oslo» før noen har lest et ord.

**Kjøretøyene beveger seg faktisk.** Hver prikk vet hvilket stopp den er på vei mot og når
den ventes fram. Nettleseren interpolerer mellom oppdateringene, så prikkene glir jevnt
langs strekningen i stedet for å hoppe hvert minutt. Det er det som skiller «live» fra
«et skjermbilde av noe live».

**Fargen er forsinkelse**, ikke linje: blå før rutetid, hvit i rute, gul, oransje, rød.
Linjefargen ligger i strekene under, forsinkelsen i prikkene over.

**Tetthet.** Med bare skinnegående som standard blir det få nok prikker til at hver enkelt
betyr noe, og nok til at byen lever. Bussen slås på når man vil se trøkket.

**Klikk** åpner et panel: de neste stoppene nedover, med Enturs anslag, vårt anslag, og
usikkerhetsbåndet vårt som en skygge rundt. Båndet er det den offisielle feeden ikke har
i det hele tatt, så det skal være synlig, ikke gjemt i en tooltip.

### 4. Forsinkelsen som vær

Arkivet har hvert minutt. En skyvebryter spoler gjennom et døgn, og play kjører det av i
seksti ganger hastighet.

Da ser man rushet bygge seg opp, en forsinkelse oppstå på ett punkt og spre seg nedover
linja, og det hele klarne igjen. Ingen har sett Oslos kollektivtrafikk sånn før, fordi
ingen har arkivert det.

Dette er delen folk deler videre.

### 5. Oppgjøret

Først nå tall, og de skal telles opp, ikke stå stille i en tabell.

To store tall som teller seg oppover til de lander: Entur på 80, vi på 67. Under, én
setning: den dummeste tenkelige gjetningen, bare anta at forsinkelsen står stille, bommer
med 78. Altså mindre enn den offisielle prognosen.

Så, for den som vil grave: tabellen per horisont. Den får være kjedelig. Den skal bare
være der.

### 6. Hvordan vi vet det, og hva vi ikke vet

Metoden kort: arkivet, ingen innsyn i fremtiden, validering på en dag modellen aldri har
trent på. Så forbeholdene, usminket. De er grunnen til at noen skal tro på resten.

## Hvordan det ser ut

**Bakgrunn** ren svart, ikke mørkegrå. Kartet og tallene skal lyse.

**Skrift.** Tall i en monospace med tabulære siffer, for det er klokker og de skal ikke
hoppe når sifrene skifter. Brødtekst i systemfonten, tett satt, store bokstavstørrelser.
Overskrifter i clamp fra 2,4rem til 7rem — nedtellingen skal fylle skjermen på en 27-tommer.

**Farger.** Linjefargene er Ruters egne for T-bane og trikk, fordi gjenkjennelse er hele
poenget. Forsinkelsesskalaen går blå → hvit → gul → oransje → rød. Bare to aksentfarger
utover det: grønn for oss, rød for dem, brukt sparsomt.

**Bevegelse.** Nedtellingen tikker i sanntid. Kjøretøyene glir. Tallene i oppgjøret teller
opp når de kommer inn i bildet. Ingenting annet animerer, for da drukner det som gjør det.

**Rytme.** Hver skjerm er én idé, i full høyde, med snap-scroll. Man ruller og får én ting
av gangen i stedet for en vegg.

## Hva som må bygges i datalaget

1. **Linjefarger og modus per linje.** Vi har modus. Fargene må inn, enten fra Enturs
   presentasjonsdata eller som en liten tabell for T-bane og trikk, som er få nok til å
   skrives for hånd.
2. **Rekkefølge på stoppene per linje**, så T-banen tegnes som sammenhengende traseer og
   ikke som løsrevne strekninger.
3. **Avgangsvalg til nedtellingen**: én avgang, 1–6 minutter frem, der de to er uenige.
4. **Avspillingsdata** for værkartet. Størrelsen må måles først; et døgn kan bli for tungt
   og da begrenses det til rushtiden.
5. **Automatisk oppdatering** av datafilen, så siden alltid er fersk.

## Rekkefølge

1. Nedtellingen. Full skjerm, ekte klokke. Krever nesten ingen nye data og er det som
   avgjør om siden treffer.
2. To klokker.
3. Kartet med hierarki og riktige farger, skinnegående som standard.
4. Bevegelse på kjøretøyene.
5. Oppgjøret med tellende tall.
6. Klikk og usikkerhetsbånd.
7. Værkartet.

Punkt 1 og 2 er kvelden. Punkt 3 og 4 er det som gjør at folk blir. Punkt 7 er det som
gjør at de sender den videre.
