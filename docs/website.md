# Nettsiden: plan

## Hva som er galt med den som står der nå

Tre feil, i rekkefølge etter hvor mye de koster.

**Den svarer på feil spørsmål.** Siden åpner med «gjennomsnittlig bom 67,4 sekunder mot
80,4». Det er tallet en modell måles på, ikke tallet et menneske lurer på. Ingen har
noensinne stått på en holdeplass og tenkt «jeg lurer på hva den vektede absoluttfeilen
er». De har tenkt: appen sa Nå for tre minutter siden, hvor er den. Siden må svare på
det først, og komme til sekundene etterpå.

**Den viser ingenting.** Kollektivtrafikk er et av de mest visuelle datasettene som
finnes: tusen kjøretøy som beveger seg gjennom en by, med forsinkelse som sprer seg
gjennom nettet som et vær. Vi har alt dette i arkivet og tegner en tabell.

**Den er en spalte.** 62 rem midt på en 27-tommers skjerm ser ut som en artikkel, ikke
som et instrument.

## Prinsippet

Én person, ett spørsmål, besvart før noe tall dukker opp. Alt annet på siden tjener
den setningen. Rekkefølgen er: kjenn deg igjen, se det skje, så får du beviset.

## Struktur

### 1. Åpningen: to nedtellinger

Fullskjerm, mørkt. To telefonskjermer side om side, begge viser samme avgang som er ute
akkurat nå. Den ene teller ned slik Ruter-appen gjør. Den andre slik Punktlig ville
gjort. Når de er uenige, står differansen mellom dem.

Under: én setning. «Ruter sier ett minutt. Vi tror fire. Om ni minutter vet vi hvem som
hadde rett.» Og for en avgang som allerede er ferdig: fasiten, med hvem som traff.

Dette er hele prosjektet forklart uten et eneste fagord, og det bruker data vi allerede
har.

### 2. Kartet

Fullbredde, resten av første skjerm. Oslo tegnet av våre egne data, ikke et bakgrunnskart
fra noen andre: stoppene som punkter, linjene mellom dem som tynne streker, alt i grått
mot svart. Oppå det ligger kjøretøyene som lysende prikker, farget etter forsinkelse,
og de beveger seg.

Hvorfor tegne det selv i stedet for å legge prikker på Google Maps eller MapLibre: ingen
API-nøkkel, ingen attribusjonskrav, ingen tredjepart som kan endre vilkår, og det ser ut
som et instrument i stedet for enda en karttjeneste. Nettverket vårt er også det eneste
som er relevant, så et bakgrunnskart med bilveier og butikker er bare støy.

**Interaksjon:**

- Hold over et kjøretøy: linje, destinasjon, nåværende forsinkelse.
- Klikk: panelet åpner med de neste stoppene, og for hvert stopp Enturs anslag, vårt
  anslag, og usikkerhetsbåndet vårt som en skygge rundt. Båndet er poenget, for det er
  den ene tingen den offisielle feeden ikke har i det hele tatt.
- Filter på modus: trikk, T-bane, buss, båt, tog.

**Tidsmaskinen.** En skyvebryter som spoler tilbake gjennom arkivet. Trykk play og se
en ettermiddag spille av seg selv: rushet bygger seg opp, en forsinkelse oppstår på
Storo og forplanter seg nedover linja, og det hele klarner igjen. Vi har hvert minutt
lagret, så dette er bare avspilling. Dette er den delen folk kommer til å dele.

### 3. Avsløringen

Etter at man har sett det: hvorfor tar den offisielle prognosen feil.

Én avgang, én tidslinje. Vogna er fire minutter forsinket ved stopp 3. Enturs prognose
for stopp 8 antar at den fortsatt er fire minutter forsinket. Vår antar noe annet, fordi
den vet at akkurat den strekningen på akkurat den tiden av døgnet spiser to minutter av
forsinkelsen. Fasiten legges oppå.

Så det ubehagelige tallet: **den naive fremskrivningen slår Enturs egen prognose.** Bare
å anta at forsinkelsen står stille er bedre enn det de publiserer, i hver eneste
horisontbøtte på hele nettet. Det er ikke et angrep, det er et måleresultat, og det er
den beste enkeltsetningen prosjektet har.

### 4. Scoreboardet

Nå, og først nå, tallene. Men oversatt:

- «Når appen sier at den er der innen to minutter, stemmer det X av 100 ganger. Hos oss
  Y av 100.»
- Treffprosent innenfor et halvt minutt, ikke bare snittfeil.
- Hvor ofte hver av dem lyver i den retningen som gjør folk sinte, altså sier at den
  kommer før den faktisk gjør.

Sekundene og MAE-tabellen får bli, men lenger ned, for de som vil ha dem.

### 5. Metoden, kort

Hvordan man i det hele tatt måler dette ærlig: arkivet, at modellen aldri får se inn i
fremtiden, at valideringen er en dag den aldri har trent på. Med lenke til koden.

### 6. Forbeholdene

Blir stående der de er, godt synlige. De er ikke en svakhet ved siden, de er grunnen til
at noen skal tro på den.

## Hva som må bygges i datalaget

1. **Koordinater for stoppene.** Enturs geocoder gir dem. Lagres i en egen tabell og
   eksporteres én gang, ikke per oppdatering.
2. **Nettverksgeometrien.** Hvilke stopp som henger sammen per linje og retning, utledet
   av arkivet vi allerede har. Eksporteres som én fil.
3. **Posisjon for kjøretøy.** Vi lagrer ikke posisjoner, bare passeringer. Et kjøretøy
   plasseres derfor mellom forrige passerte stopp og neste ventede, interpolert på tid.
   Det er en tilnærming og skal stå i metodeteksten.
4. **Live-eksport.** Én JSON per oppdatering med kjøretøy, prediksjoner og
   usikkerhetsbånd. Kvantilmodellene finnes allerede.
5. **Avspillingsdata.** For tidsmaskinen: en komprimert serie for et valgt døgn.
   Størrelsen må måles før den bygges.

## Det som er vanskelig

**Kartet må tegnes på canvas, ikke som DOM-elementer.** Tusen prikker som oppdateres er
ingenting for canvas og altfor mye for tusen div-er.

**Interpolert posisjon kan se rart ut** når en vogn står stille eller hopper. Det må
testes mot virkeligheten før det vises, ellers ser siden ødelagt ut selv når dataene er
riktige.

**Avspillingsfilen kan bli stor.** Et døgn med tusen kjøretøy per minutt er 1,4 millioner
posisjoner. Det må ned til noe som lastes på et blunk, ellers dropper vi funksjonen eller
begrenser den til en time.

**Modellen bak live-prediksjonene er trent på en firedel av dataene.** Det står i
forbeholdene, men det betyr også at tallene på siden blir bedre av seg selv når
minneproblemet er løst.

## Rekkefølge

1. Bredden og layouten. Fullbredde, kartet som hovedelement. Billig, og fjerner
   «tynn»-følelsen umiddelbart.
2. Åpningen med de to nedtellingene. Krever ingen nye data.
3. Koordinater og nettverksgeometri.
4. Kartet med live kjøretøy, uten interaksjon.
5. Klikk og detaljpanel med usikkerhetsbånd.
6. Scoreboardet oversatt til menneskespråk.
7. Avsløringen med én avgangs tidslinje.
8. Tidsmaskinen.

Punkt 1 til 2 gjør siden brukbar. Punkt 3 til 5 gjør den verdt å vise frem. Punkt 8 er
det folk deler.
