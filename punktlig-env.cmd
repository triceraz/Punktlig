@echo off
rem Felles oppsett for alle fire Scheduled Tasks. Kalles med:
rem     call "%~dp0punktlig-env.cmd"
rem
rem Lista over kodespacer stod tidligere i bade run-collector.cmd og
rem run-site.cmd. Da SJN ble fjernet 2026-08-06 ble den fjernet ett sted, og
rem eksporten fortsatte a sporre etter en stream som ikke lenger samles inn.
rem En innstilling som ma holdes i takt for hand pa to steder er en
rem innstilling som en dag ikke er det.

rem Data ligger paa D: fordi C: bare har ca 10 GB ledig og raa-arkivet alene
rem vokser til over 11 GB
set PUNKTLIG_DATA=D:\punktlig-data

rem SQLite og DuckDB legger midlertidige filer i %%TEMP%%, som bor paa C: med
rem ca 9 GB ledig. Slettingen av en eksportert dag dode tre netter paa rad med
rem "database or disk is full" pga dette, og dagen ble liggende i begge lag.
rem Alt midlertidig skal dit dataene bor: paa D.
if not exist "D:\punktlig-data\tmp" mkdir "D:\punktlig-data\tmp"
set TMP=D:\punktlig-data\tmp
set TEMP=D:\punktlig-data\tmp
set PUNKTLIG_CLIENT_NAME=triceraz-punktlig
set PUNKTLIG_MET_UA=punktlig-collector/0.1 (https://github.com/triceraz/Punktlig)

rem SJN fjernet 2026-08-06: 376 poll siden dag en, 0 anlop, 0 treningsrader.
rem SJ Nord kjorer Trondheim-Bodo og publiserer ingenting i denne regionen,
rem men brukte en fjerdedel av sekundaerbudsjettet mot Enturs grense - og
rem Flytoget, som stod sist i lista, ble kastet ut med 429 i sju timer for det.
set PUNKTLIG_DATASET=RUT,VYG,GOA,FLT
set PUNKTLIG_AUTHORITY=RUT:Authority:RUT,VYG:Authority:VY,VYG:Authority:FLB,VYG:Authority:VYT,GOA:Authority:GOA,FLT:Authority:FLT
set PUNKTLIG_MODES=tram,metro,bus,water,rail

rem RUT hvert minutt; togkodespacene sjeldnere sa klienten holder seg
rem innenfor Enturs grense
set PUNKTLIG_SECONDARY_EVERY=300
