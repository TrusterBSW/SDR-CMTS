# Tests à effectuer : émetteur downstream (état actuel)

Banc **fermé uniquement**. Aucune liaison avec la prise SFR pendant ces tests.

## Matériel
- B210 et son câble USB 3 ; PC Linux
- Modem Numericable/SFR et son alimentation
- Un atténuateur 8 dB, deux coupleurs (répartiteurs) 1 vers 2 et des adaptateurs SMA ↔ F
- HackRF (ou RTL-SDR) pour observer, avec gqrx ou SDR++

## Raccordement
```
B210 [TX/RX] ── coupleur A [entrée] ──┬── sortie 1 ── modem (entrée câble)
                                      └── sortie 2 ── 8 dB ── coupleur B ── HackRF / RTL-SDR
PC Ethernet ── modem (pour lire http://192.168.100.1)
```
Le port RX2 du B210 reste libre. Aucun récepteur ne doit être sur la même branche que le modem
sans passer par l'isolation du coupleur A et l'atténuateur 8 dB : les bursts du modem peuvent atteindre +8 dBm.

---

### T0 : préparation (modem branché sur la ligne SFR, AVANT le banc)
- **But :** relever la fréquence downstream habituelle du modem.
- **Action :** ouvre `http://192.168.100.1` et note la fréquence downstream, la puissance (dBmV), le SNR et la modulation.
- **Attendu :** une fréquence vers 300–800 MHz, sur la grille de 8 MHz. Elle servira dans `--freq`.
- Débranche ensuite le modem du réseau SFR.

### T1 : performance du PC
```bash
cd ~/claude/Docsis
python3 ds_tx.py --bench
python3 ds_tx.py --bench --qam 256
```
- **Attendu :** plus de `1.2x real time` dans les deux cas.
- **Sinon :** ferme les autres applications et règle le gouverneur CPU en performance (`mini_cmts.py --performance` le fait le temps du lancement).

### T2 : tests logiciels
```bash
python3 tests/test_phy_vs_grdtv.py
python3 tests/test_loopback.py 64
python3 tests/test_loopback.py 256
```
- **Attendu :** `equal: True` (deux fois), puis `packets equal to transmitted: 1.0000` et `HCS status 1` sur tous les messages.

### T3 : détection du B210
```bash
uhd_find_devices
uhd_usrp_probe | head -40
```
- **Attendu :** un `B210` trouvé, connecté en USB 3 (le probe indique `Operating over USB 3`).
- Au premier lancement, le firmware se charge (quelques secondes).

### T4 : signal à vide, sans modem
- **Raccordement :** B210 → 30 dB → HackRF uniquement.
- **Actions :**
  ```bash
  python3 ds_tx.py --freq 474e6 --gain 40
  ```
  En parallèle, dans gqrx sur le HackRF : fréquence 474 MHz, bande ≥ 10 MHz, gain LNA bas.
- **Attendu :**
  - un plateau plat d'environ 8 MHz de large, centré sur 474 MHz, avec des flancs raides ;
  - pas de raie au centre (fuite LO faible) ;
  - côté console : un message de statut toutes les 5 s, `queue=` stable autour de 10–16, **aucun `U`**.
- **Problèmes :** des `U` réguliers signalent un problème CPU ou USB (refais T1, change de port USB 3). Une raie forte au centre est normale à faible gain ; elle n'empêche pas le verrouillage.

### T5 : verrouillage du modem en 64-QAM
- **Raccordement :** le montage complet ci-dessus.
- **Action :**
  ```bash
  python3 ds_tx.py --freq <fréquence notée en T0> --gain 50
  ```
  Allume ensuite le modem et attends jusqu'à 5 minutes.
- **Attendu :**
  1. Le voyant downstream passe de clignotant à fixe.
  2. La page `192.168.100.1` indique downstream *Locked*, en 64-QAM.
  3. Puissance entre −10 et +10 dBmV, SNR au-dessus de 30 dB.
- **Réglage du niveau :** si la puissance dépasse +10 dBmV, baisse `--gain` ; si elle est sous −10 dBmV, monte-le, par pas de 5 dB. Repère : niveau au modem ≈ gain − 50 dBmV à travers un coupleur 1 vers 2 (±5 dB) ; démarre à `--gain 50`.
- **À noter :** le temps de verrouillage, la puissance, le SNR et le journal d'événements du modem.

> **✅ Résultat validé — 2026-10-07 (Sagem F@st 3686 V1B)**
> - Verrouillage downstream obtenu : voyant passé fixe (brièvement), en 64-QAM.
> - **Fréquence : 306 MHz. Gain : `--gain 49`. Inversion : défaut (spectre inversé), *sans* `--no-invert`.**
> - Raccordement : un seul splitter 1→2 partagé entre le HackRF et le B210.
> - Niveaux non lus dans l'interface web (verrouillage trop bref pour ouvrir la page).
> - Burst de ranging du modem observé au HackRF à **40 MHz** (voir T7) → le modem a lu l'UCD et appliqué la config amont.
> - Lock bref et répété : attendu, aucun récepteur amont ne répond au RNG-REQ, le modem retente puis rescanne.
> - **Conclusion : chaîne descendante J.83A + MAC DOCSIS (SYNC/UCD/MAP) + inversion validée de bout en bout contre un vrai modem.**

### T6 : synchronisation DOCSIS (SYNC, UCD, MAP)
- Se fait dans la suite de T5, sans rien changer.
- **Attendu :**
  - pas d'erreur *T1 timeout* (SYNC/UCD) ni *SYNC timing synchronization failure* dans le journal ;
  - le voyant upstream se met à clignoter ;
  - le statut passe en *ranging* ou équivalent.
- **À noter :** les messages exacts du journal (copie ou photo).

### T7 : bursts du modem visibles
- **Raccordement :** passe le HackRF ou le RTL-SDR à 40 MHz, avec 2 MHz de bande au moins (vue cascade/waterfall).
- **Attendu :**
  - des impulsions courtes (environ 1,6 MHz de large) autour de 40 MHz ;
  - leur niveau monte d'essai en essai : le modem augmente sa puissance faute de réponse ;
  - des *T3 timeout / No Ranging Response* dans le journal : **c'est normal à ce stade** ;
  - au bout d'un moment, le modem peut redémarrer son scan, puis revenir.
- **Optionnel :** enregistre une capture IQ de 10 à 20 s à 40 MHz pour l'analyser plus tard. Garde un gain bas pour ne pas saturer.
  ```bash
  hackrf_transfer -r us_40M.iq -f 40000000 -s 4000000 -n 80000000 -l 16 -g 10
  ```

> **✅ Observé — 2026-10-07 :** burst de ranging du modem visible à 40 MHz au HackRF, consécutif au verrouillage downstream (cf. T5).

### T8 : 256-QAM
```bash
python3 ds_tx.py --freq <fréquence> --gain 50 --qam 256
```
- **Attendu :** même chose qu'en T5–T7, avec la modulation 256-QAM affichée sur la page d'état.
- Le SNR nécessaire est plus élevé : au moins 33 dB.

### T9 : autre fréquence upstream
```bash
python3 ds_tx.py --freq <fréquence> --us-freq 60e6
```
- **Attendu :** les bursts se déplacent vers 60 MHz.
- Cela confirme que le modem suit bien l'UCD.

### T10 : stabilité (≥ 10 minutes)
- Laisse tourner T5 pendant plus de 10 minutes.
- **Attendu :** pas de `U`, le downstream reste verrouillé.
- **À surveiller vers 7 minutes (419 s) :** le compteur d'horodatage repart à zéro. Note si le modem perd la synchronisation à ce moment-là.

---

## Surveillance du banc avec le 2e B210 (`lab_monitor.py`)

Le 2e B210 écoute le coax du labo et décode en continu le downstream de **ton** CMTS. Il affiche
des diagnostics et écrit **tout** dans un pcap pour Wireshark : données, messages unicast et
messages diffusés. Ce n'est le cas que si la source des SYNC est ton CMTS (`--lab-mac`, 02:00:00:ca:fe:01 par défaut).

### Raccordement
```
B210 n°1 [TX/RX] ── coupleur A [entrée] ──┬── sortie 1 ── modem
                                          └── sortie 2 ── 8 dB ── coupleur B [entrée] ──┬── B210 n°2 [RX2 / « RXA »]
                                                                                         └── HackRF (upstream, 40 MHz)
```
Avec deux B210 sur le même PC, chaque commande doit viser son appareil :
```bash
uhd_find_devices                     # relever les deux numéros de série
python3 ds_tx.py --args type=b200,serial=<SERIE_TX> --freq 474e6 --gain 50
python3 lab_monitor.py --args serial=<SERIE_RX> --freq 474e6 --gain 50 --pcap captures/labo.pcap
wireshark captures/labo.pcap         # filtre utile : docsis_mgmt
```
Branche si possible les deux B210 sur deux contrôleurs USB 3 différents.

### T11 : le moniteur voit le signal (avant d'allumer le modem)
- **Attendu au lancement :** `niveau d'entrée` entre 0,05 et 0,5. Ajuste `--gain` si le script signale une saturation ou un niveau trop faible.
- **Attendu à chaque bloc :**
  - MER au-dessus de 35 dB, `spectre inversé` (ou `normal` si `ds_tx.py --no-invert`) ;
  - `RS en erreur 0.00 %`, `CC err 0`, `SYNC/10.0x ms` ;
  - gigue SYNC de l'ordre de 100 ns, et `horloge x1.000000` à ±0,000005 près (la norme impose ±5 ppm) ;
  - trames : `MAP` et `SYNC` à chaque bloc, `UCD` environ une fois par seconde.

### T12 : modem allumé
- Garde `lab_monitor.py` en marche pendant T5 à T7. Le pcap enregistre tout ce que ton CMTS émet.
- Si le modem ne verrouille pas alors que T11 est bon, compare dans Wireshark les SYNC, UCD et MAP de `captures/labo.pcap` avec ceux du réseau SFR (`captures/sfr282_mgmt.pcap`).
- `--save-iq captures/iq` garde aussi les blocs IQ bruts pour une analyse ultérieure. Prévois de l'espace : environ 80 Mo par bloc de 0,5 s.

---

## Étape 3 : upstream et ranging (`mini_cmts.py`, un seul B210)

Montage (voir README, section « Étape 3 ») :
```
B210 TX/RX ── coupleur A [entrée] ──┬── sortie 1 ── modem
                                     └── sortie 2 ── 8 dB ── coupleur B [entrée] ──┬── B210 RX2
                                                                                    └── HackRF (60 MHz, amp. OFF)
```
La rafale du modem atteint RX2 par l'isolation du coupleur A : environ −17 dBm au pire. Ne mets pas le modem sur l'entrée du coupleur A.
Ordre : lance le programme, puis allume le modem. Pour arrêter, éteins le modem d'abord, puis Ctrl-C.

### T13 : tests sans matériel
```bash
python3 tests/test_upstream_phy.py
python3 tests/test_ranging_sim.py
python3 tests/test_im_overlap.py
```
- **Attendu :** `PASS` trois fois. Le second affiche 6 scénarios `OK`, avec un *success* vers 0,1 s.

### T14 : démarrage sans modem
```bash
python3 mini_cmts.py --freq 306e6 --gain 49
```
- **Attendu :** une ligne `master clock 27.808000 MHz | TX … | RX 3.476000 MS/s @ 60.0000 MHz (LO 63.0 MHz)`,
  sans message `!!`. Ensuite, toutes les 5 s : `U 0 L 0 O 0`, et un bruit RX stable (vers −60 / −75 dBFS selon `--us-gain`).
- Avec le HackRF à 60 MHz, les rafales du modem doivent apparaître à 60 MHz, et plus à 40 MHz : le modem suit l'UCD.
- Si la ligne affiche `!!`, note le message exact.

### T15 : écoute seule, le modem émet ses RNG-REQ
```bash
python3 mini_cmts.py --freq 306e6 --gain 49 --us-gain 10 --no-rsp --save-bursts captures/bursts
```
Allume le modem.
- **Attendu :** après le verrouillage downstream, des séries de lignes `[US] RNG-REQ <MAC du modem> IM SID 0x0000 …`
  (écoute seule). La puissance (dBFS) monte d'une ligne à l'autre. Sans réponse, le modem abandonne, rescanne
  le downstream (quelques secondes à quelques dizaines de secondes), puis recommence une série.
- **Réglage de `--us-gain` :** les plus fortes rafales d'une série doivent arriver vers −20 dBFS. Si elles restent bien
  en dessous, relance avec +10. Si `SATURATION ADC` apparaît, baisse-le. Garde cette valeur pour T16 : le CMTS
  ordonne au modem d'arriver à −20 dBFS, donc un gain trop bas le ferait émettre plus fort que nécessaire.
- **À noter :** les mentions `octets LSB-first` ou `spectre inversé` si elles apparaissent, la MER et
  les éventuelles lignes `rafale … non décodée`. Garde le dossier `captures/bursts` si des rafales restent non décodées.

### T16 : ranging
```bash
python3 mini_cmts.py --freq 306e6 --gain 49 --us-gain <valeur réglée en T15>
```
- **Attendu :**
  1. `RNG-REQ … IM` puis `RNG-RSP continue` (correction de temps, puissance et fréquence) ;
  2. `RNG-REQ … SM SID 0x0001`, puis `RNG-RSP success` et `RANGING TERMINÉ` ;
  3. une `RNG-REQ SM` par seconde, toujours en *success* ;
  4. `à l'antenne … ms après la rafale` doit rester **sous 200 ms** ;
  5. ensuite, des lignes `REQ SID 0x0001 : N mini-slots demandés` : le modem tente DHCP. On ne lui accorde
     pas encore de bande, il finira donc par redémarrer : **c'est normal à cette étape**.
- Sur la page du modem (si elle est joignable) : upstream verrouillé et ranging terminé, puis DHCP en cours ou en échec.
- **À noter :** le premier `TA` (retard aller-retour du banc), la puissance d'émission annoncée par le modem et son journal.

### T17 : stabilité (5 minutes)
- Laisse tourner T16.
- **Attendu :** le modem reste en *success* et `U 0`. Pas de `Station Maintenance sans réponse, modem oublié`.

## À me rapporter
| Test | OK / KO | Notes : puissance, SNR, messages du journal, captures |
|---|---|---|
| T1 | | |
| T2 | | |
| T3 | | |
| T4 | | |
| T5 | | |
| T6 | | |
| T7 | | |
| T8 | | |
| T9 | | |
| T10 | | |
| T11 | | |
| T12 | | |
| T13 | OK (2026-10-09) | `test_upstream_phy` PASS (0 faux décodage sur bruit), `test_ranging_sim` PASS (7 scénarios, dont Sagem 183 µs d'avance : success à 0,098 s), `test_real_bursts` PASS (3/3 rafales réelles du Sagem) |
| T14 | OK (2026-10-08) | 27,808 MHz, RX 3,476 MS/s @ 60 MHz (LO 63), bruit −65 dBFS à `--us-gain 30`, TX horodaté bloc par bloc validé sur le FPGA LibreSDR : 6 lancements, `U 0 L 0` en régime établi ; 2 fois sur 6, un à-coup au démarrage seulement (U 1, L 1 à 9 = jusqu'à ~25 ms de downstream jeté, sans décalage), modem encore éteint à ce moment |
| T15 | OK (2026-10-08, 2e essai) | 51/51 RNG-REQ décodées (Sagem 24:7f:20:a8:38:b8, RNG-REQ v1, SID 0), LSB-first, spectre normal. Arrivée −174,1 à −174,5 µs (avant l'IM), +240 Hz, MER 27–32 dB, bruit −70 dBFS à `--us-gain 20`. Le modem balaie sa puissance par pas de ~5 dB sur ~42 dB (−42 à 0 dBFS), seule la plus forte sature. Filtre adapté OK (spectre ±800 kHz, −60 dB au-delà ; MER identique pour α 0,18/0,25). Encore U 12 / L 487 sans priorité temps réel. |
| T16 | OK (2026-10-09), avec un bug corrigé ensuite | Priorité `SCHED_RR 50` (via `prlimit`). IM : arrivée −174,2 µs, −21,7 dBFS, +355 Hz → `RNG-RSP continue TA -1784 P +1.75 dB F -355 Hz`. 1er SM : +0,08 µs, −20,1 dBFS → **success**, `RANGING TERMINÉ`. Puis 1 SM/s en success (±0,07 µs, −20,1 dBFS, MER 26–32 dB), RNG-RSP à l'antenne 54–61 ms après la rafale. REQ SID 1 de 77 mini-slots (DHCP). Rafale U 2 / L 11 isolée à t = 13,3 s, puis plus rien. **Bug à t = 25 s** : SM placé 2 ms après le début de l'IM, donc dans la marge `--im-slack-us` → mesuré contre l'IM, TA +2000 µs envoyé, modem oublié. Corrigé dans `cmts/upstream.py` (la marge IM ne prend plus une rafale située dans un grant unicast) et `cmts/ranging.py` (garde-fou), test `tests/test_im_overlap.py`. À relancer pour T17. |
| T17 | essai 1 (2026-10-09, 68 s, arrêté avant 5 min) | Correctif IM/SM validé : des SM tombés à +1988 µs du début de l'IM sont bien mesurés en SM, aucun `modem oublié`, modem `ranged` tout du long (±0,05 µs, −20 dBFS, MER 27–32 dB, RNG-RSP 54–65 ms). **Problème : U 9 / L 239**, par salves toutes les 5 à 15 s, dont la plus grosse (~220 L, soit ~32 ms de downstream : un `L` = un paquet USB de 2040 échantillons, 0,147 ms) vers t = 51 s. Après chaque salve, la puissance du modem saute de ±5 à 7 dB (−14,5 / −27 dBFS) et 2 rafales SM arrivent à −40 dBFS non décodées. Cause probable : l'IRQ USB du B210 (xHCI 00:14.0, IRQ 135) est traitée uniquement sur le CPU 0 ; nos threads SCHED_RR y affament ksoftirqd/0 (priorité normale). Correctif : `--cpus auto` (par défaut) écarte les CPU 0 et 4 ; lancer avec `--performance` (gouverneur `performance` le temps du lancement).<br>**Essai 2 (2026-10-09, 388 s, `--cpus auto`) :** U 4 / L 197 (0,5 L/s contre 3,4 L/s à l'essai 1), 556 rafales toutes décodées, modem `ranged` à l'arrêt (+0,04 µs, −20,4 dBFS, MER 29–30 dB, RNG-RSP 55–57 ms). Mais SID 0x0002 : le CMTS a oublié le modem une fois (16 SM sans réponse) puis l'a recalé ; cause (salve TX ou redémarrage du modem faute de DHCP) inconnue, log complet non conservé. Non validé : à refaire avec `--pcap` et `tee`.<br>**Essai 3 (2026-10-09) :** le modem répond parfaitement jusqu'à t = 213,5 s (−0,035 µs, −19,9 dBFS, MER 31,7 dB), puis se tait ~14 s ; 16 SM sans réponse → `modem oublié` ; il revient en IM avec SID 0 à t = 227,8 s (−174,9 µs, comme au démarrage), `success` dès le 1er SM, SID 0x0002, et ses REQ recommencent aussitôt (227,96 → 232,96 → 241,96 s : écarts de 5 puis 9 s, le recul exponentiel d'un DHCP DISCOVER). **U/L inchangés (U 2 L 172) de 00:30:23 à 00:31:03** : la perte n'est pas due au TX. Cause la plus probable : le modem réinitialise sa couche MAC après l'échec de son DHCP (attendu tant qu'il n'y a pas de grants de données ; à confirmer dans son journal d'événements). Ranging : stable et reprise automatique OK. Reste ouvert : U 2 / L 172, survenus avant 00:30:23.<br>**Essai 4 (2026-10-09) :** U 0 L 0 pendant 50 s, puis une seule perte de ~26 ms de downstream (U 1, L 176) juste après t = 50,8 s ; ensuite puissance du modem −14,3 / −31,4 dBFS et MER 15,9 dB pendant ~3 s, recorrigés. Même instant (~51 s) que le plus gros blocage de l'essai 1 → déclencheur probablement déterministe. GC Python écarté (aucune collecte complète en 60 s simulées, 81 k objets). Chien de garde ajouté pour l'identifier.<br>**Essai 5 (2026-10-09, `captures/t17.log` + `t17.pcap`, 60 s) :** une seule perte, U 1 / L 136 = **20 ms** de downstream vers t ≈ 51,5 s, **sans pause > 25 ms du thread principal** ; aucun bridage thermique, limites RAPL 64/64 W. pcap : 50 RNG-REQ→RSP en 51,6–63,9 ms (médiane 55,5), 46 success / 4 continue (dont P −5,25 puis +7 dB juste après la perte) ; REQ par séries de 17 (1 + 16 reprises) espacées de 5, 9, 17 s = DHCP DISCOVER sans grant. Cause retenue : la seule réserve contre un retard du thread d'envoi était ce qu'UHD avait déjà passé à l'USB (16 trames par défaut, quelques ms). Ajouté : `--tx-frames 256` (réserve 37,6 ms, latence RNG-RSP attendue ~90 ms), marge TX mesurée (ligne d'état `marge TX min`), L convertis en ms.<br>**Essai 6 / T17b (2026-10-09, 25 min, `--tx-frames 256`) : OK.** U 1 / L 35 = 5 ms en tout, une seule fois (t = 1022 s : le thread `worker` a tenu le GIL ~30 ms, chien de garde en retard de 31 ms). Plus de perte vers 51 s. Marge TX min ~36 ms (un creux à 17 ms sans perte). Le modem se réinitialise toutes les ~2,5 min (SID 1 → 8) faute de DHCP : attendu avant l'étape 4. |
| T18 | OK (2026-10-09) | `test_registration.py` (avec le vrai REG-REQ du Sagem) et `test_provision_tap.py` : PASS |
| T19 | en cours (2026-10-09) | Grants OK (le modem émet exactement dans son grant de 77 mini-slots, MER 30–32 dB). 1er essai : DHCP DISCOVER non décodé, car le modem complète son dernier bloc RS par des zéros jusqu'à la fin du grant (220+220+116 octets d'info pour une trame de 553). Décodeur corrigé, 10/10 rafales enregistrées décodées (FCS Ethernet bon).<br>**2e essai : DHCP OK** (DISCOVER→OFFER→REQUEST→ACK 10.10.0.10, mais le modem n'a répondu qu'au 4e OFFER, les 4 étant identiques ; cause inconnue), ARP OK, **ToD OK**. TFTP RRQ non décodé : la taille du grant arrivait à 447,9999 symboles (différence de grands flottants), tronquée au lieu d'arrondie → dernier bloc RS faux d'un octet. Corrigé, 6/6 RRQ enregistrées décodées. À relancer. |
| T20 | **OK (2026-10-09, 01:42)** | TFTP « cm.cfg » (100 octets) → REG-REQ 220 octets (TLV 3, 18, 29, 24, 25, 6, 7, 5, 8, 12, 44×3, 57) → REG-RSP ok (SFID 1 montant SID 0x0001, SFID 2 descendant) → **REG-ACK ok → MODEM EN LIGNE**. Capacités annoncées : DOCSIS 2.0 (plafonné 1.1 par le CMTS), concaténation, fragmentation, PHS, BPI+, 16 SID, DCC. Le CMTS MIC paraissait faux : TLV 57 (raison d'initialisation, ajouté par le modem) était à tort dans la liste du MIC ; corrigé, le MIC est correct. Un appareil derrière le modem a pris 10.10.0.11 et envoie des requêtes DNS vers 1.1.1.1 (pas de routage : labo fermé). |
| T22 | OK (2026-10-09) | C = Python sur 600 blocs RS, interpolation −58 dB, suivi QPSK/64-QAM sans erreur ; rafale 1500 o. décodée en 2–3 ms (69 avant), GIL ≤ 1,5 ms |
| T23 | OK (2026-10-09, 02:23) | `--tx-frames 128 --tx-queue 6` : ~4 min en ligne, U 0 L 0, marge TX min 16,3 ms sur 19, latence DS ~41 ms (75 avant). Devenus les défauts. CMTS MIC désormais vu correct. |
| T25 | OK (2026-10-09, après corrections) | 1er essai (fast.com) : 2/3 du montant jeté (concaténation en 0xDC au lieu de 0xF8), RNG-RSP jusqu'à 491 ms derrière les données. Corrigé : 55-59 pings/min arrivent, concaténations découpées. |
| T26 | OK (2026-10-09 03:05) | 256-QAM DS, gain 45 : en ligne ; 40,8 Mbit/s UDP sans perte, ping 0 % de perte ; à 56 Mbit/s la file plafonne à 100 ms et jette l'excédent, modem en ligne. |
| T28 | OK (2026-10-09 03:30) | `--docsis30` (MDD), un canal : REG-REQ-MP → REG-RSP-MP → REG-ACK ok, en ligne en mode 3.0. |
| T29 | OK (2026-10-09 03:37-03:58) | `--ds-channels 2` : RCC acceptée (REG-ACK ok, 4 essais sur 5 ; 1 « Partial Service » canal 2) ; DSID 0x1001 ; ping 20/20 avec tout le trafic forcé sur le canal 2 ; 91,8 Mbit/s DS sans perte, U 0 L 0. |
| T33 | OK (2026-10-10 00:05) | Configuration « débit maximal » (README) : speedtest local **93 Mbit/s DS / 22 Mbit/s US**, fast.com via wifi 78/17 Mbit/s, latence 64 ms à vide / 92 ms en charge. Étapes : 1,1 → 3,7 (grants) → 0,46 (burst 64 Ko, dérive d'horloge) → 9,6 (compensation −4,3 ppm) → 12 (profil 200/200) → 17-22 (C, grants un MAP sur 3). |
| T30 | OK (2026-10-09 03:56) | `--us-mode atdma --us-mod 16qam --us-rate 32` : UCD 29 accepté, en ligne, RNG MER 25,4 dB, rafales de données décodées. |
| T31 | OK (2026-10-09 04:27, après corrections) | `--us-mode atdma --us-mod 64qam --us-rate 32` : table 64-QAM confirmée. 1er essai : 5 % d'échecs (fausses REQ), rafales longues KO en fin de rafale (ISI, MER 24,5 dB), 32 rafales manquées (grappe fusionnée), blocages TX toutes les 31 s (pcap). Après égaliseur + corrections : 4 min à 30 Mbit/s DS, **15 670/15 674 rafales décodées, U 0 L 0, 0 blocage, pcap 960 Mo**. ATDMA 64-QAM à 2,56 Msym/s : MER 33,3 dB, 12 575/12 579. |
| T32 | OK (2026-10-09 04:37) | `--ds-channels 2` + ATDMA 64-QAM 5,12 : 3 min à 86,7 Mbit/s DS (jetées 0) + ~1300 trames/s montantes, U 0 L 0, 10 562/10 569 rafales décodées (bruit montant −48,5 dBFS, MER brute 23,8). 80 Mbit/s vers l'eRouter : ping 0 % de perte, 100 ms. 1 « Partial Service » sur 2 lancements. |
| T34 | OK (2026-10-10 02:18) | `--us-channels 2`, réception large bande (27,808 MS/s, DDC logiciel par canal), MTC pas encore activé : en ligne sur le canal 1 comme avant, ~40 % de CPU par processus montant. `tests/test_usproc.py` : même rafale réelle décodée sur les deux canaux (MER 36,7 dB, instant identique). |
| T35 | OK (2026-10-10 02:38) | Agrégation montante MTC : REG-ACK 209 (reject-bad-tcc) avec le canal 2 à 67 MHz, puis avec le TCC de fenêtre de dynamique ; sans ce TCC et à 53 MHz : ranging du canal 2 (MER 29 dB), REG-ACK ok, **en ligne sur 2 canaux montants**, segments réassemblés (seq 0 sur le canal 1, seq 1 sur le canal 2…), ping 20/20. Débit agrégé non mesuré (pas de client derrière le modem). |
| T36 | partiel (2026-10-10 02:43-03:00) | 3 DS + 2 US : en ligne à chaque essai ; les 3 canaux descendants acquis 1 fois sur 4 (sinon canal 2, parfois 2 et 3). `--tx-dc-cal` (fuite TX −2 à −24 dB) sans effet sur ce taux. |
| T37 | partiel (2026-10-10 02:43-03:41) | 3 DS + 2 US, 19 lancements : toujours en ligne, les 3 DS acquis 6 fois (gain 52 : 1/7 dont 4 calibrés ; 47 : 2/4 ; 44 : 3/4 puis 0/4). Le gain TX ne fait pas de différence nette. |
| T38 | OK (2026-10-10 03:19) | 4 US ATDMA 64-QAM 2,56 Msym/s + 2 DS : 4 canaux rangés, REG-ACK ok, ping 30/30 (91 ms), 2 640 segments réassemblés, 0 perdu, U 0 L 0 ; CPU : émission 150 %, 40-50 % par processus montant. |
| T39 | limite (2026-10-10 03:31-03:34) | 3 DS + 4 US : avec 2 cœurs pour le montant, l'émission décroche (L 3 millions) ; avec 1 cœur (`MINI_CMTS_US_CORES=1`), en ligne avec tous les canaux (1 essai), montant à ~150 ms, ping 40/40 à 258 ms. |
| T40 | OK (2026-10-10 13:33-14:13) | Hôte Proxmox (i5-9500 6 cœurs, sans AVX-512, B210 seul sur le bus USB 3, `--no-irq-herd`), client iperf3 derrière le modem : référence v6 2 DS + 1 US **87/19 Mbit/s** ; code actuel par défaut 86,8/18,6 ; 2 DS + 2 US 86,6/11-12 (processus montants à 100 %) ; 3 DS + 2 US 15-24/13 (émission limitée par le GIL : Python 1,4× plus lent que sur le portable, canal 2 perdu en boucle). Blocages TX+RX de 85 ms et 13 O/62 U en 90 s de `benchmark_rate` à 27,8 MS/s dus aux états C profonds (C10) : 0 O/1 U avec `/dev/cpu_dma_latency` tenu (désormais fait par `--performance`). |
| T41 | OK (2026-10-10 14:57) | `--radio-process` (B210, J.83, mise en forme, émission dans `cmts/radio.py`), Proxmox, 3 DS + 2 US : 15-24 → **129,7 Mbit/s** descendant, U 0 L 0, marge TX ≥ 17,5 ms pendant la charge (avant : ~1 ms, blocages de 66-109 ms, canal 2 perdu en boucle) ; les 3 canaux portent chacun un tiers du trafic. `tests/test_radio.py`. |
| T42 | OK (2026-10-10 15:10-15:35) | Décodage montant optimisé (métrique de préambule en C, DDC/filtre adapté/interpolation AVX2) et dérive d'horloge gardée seulement si elle décode : sans ce dernier correctif, sous envoi, MER de US1 22 → 12 dB et 18 % de rafales perdues. Coax nettoyé (2e modem non alimenté débranché). 3 DS + 2 US : 130,8 / 15,8 Mbit/s, 99,4 % décodées ; **3 DS + 1 US : 130,8 / 20,2 Mbit/s**, ping 93 ms. |
| T43 | analyse (2026-10-10 16:00-16:20) | Pourquoi 2 US < 1 US (3 DS, processus radio). 1 US : 20,7 Mbit/s, 95 retransmissions TCP, 0,2 % de rafales perdues. 2 US : 11,6-17,4 Mbit/s, 347 retransmissions ; US2 perdait 2,7 % de ses grants : le modem n'y émettait pas (captures : seulement le débordement à −38 dBFS d'une rafale US1 simultanée), puissance déclarée 46-49 dBmV par canal. Avec `--us-target -20` (40,75 dBmV) : 0,2-0,8 %, 108-164 retransmissions. Restent : réassemblage en ordre qui attend l'autre canal (moy. 4-70 ms, max 200-760 ms), grants utilisés à 85 % mais ~35 % de la capacité d'un canal : TCP, freiné par ces pertes et à-coups, ne remplit pas la file du modem (requêtes moyennes 7-10 Ko). Les grants en avance sur tous les canaux à la fois (`MINI_CMTS_AHEAD_ALL`) n'y changent rien. Diagnostic `[MTC]` (MINI_CMTS_TIMING). |
| T44 | OK (2026-10-10 16:20-16:40) | Horloge maître et calage du modem, 1 canal : calé en 12-16 s à 20,856 (3 éch./symbole), 27,808 (2 et 4) et 34,76 MHz (5) ; jamais en 150-180 s à 41,712, 48,664, 55,616 MHz, ni en 64-QAM, ni avec `--no-invert` (aucune énergie montante : le modem n'émet pas). Rebouclage B210 (TX 306 MHz → RX2) : porteuse 0 Hz, horloge 0 ppm, MER 21,2 dB identiques de 27,808 à 55,616 MHz. |
| T45 | OK (2026-10-10 16:44-16:54) | **4 DS** à 34,76 MHz, `--radio-process` (une synthèse par canal : avec 2 fils l'émission tombait à 80 % du temps réel) : en ligne, 4 canaux agrégés (le 4e par CM-STATUS), **156,0 / 19,3 Mbit/s**, trafic réparti au quart ; second essai 131,5 / 19,2, les canaux centraux 2-3 (de part et d'autre de l'oscillateur local à 318 MHz) ont décroché 1 à 3 fois par essai. |
| T46 | OK (2026-10-10 21:00-21:30) | **Calage au-delà de 34,76 MHz** : à 41,712 MHz avec un US qui marche (`MINI_CMTS_DECIM=2`), la page du modem dit DS non acquis → c'est l'émission. Lien FPGA ↔ AD9361 à 41,7 MHz écarté (calé à 20,856 MHz en 2R2T, horloge du lien doublée). Rebouclage B210 : image TX (miroir autour de l'OL) à −51 dBc jusqu'à 27,8 MHz, −21 à −24 dBc au-delà avec le filtre RX à 10-12,4 MHz, −51 dBc avec ≥ 20 MHz : la calibration de quadrature TX d'UHD (ton rebouclé par le filtre RX, à l'ouverture des flux) ratait. Flux ouverts avec un filtre RX de 56 MHz puis resserré (`radio.TX_CAL_RX_BW`) : **calé à 41,712 MHz (20 s) et 55,616 MHz (12 s)** ; 4 DS en ligne d'emblée sur les 4 canaux. 5 DS à 55,616 MS/s : le processus radio (218 % CPU) ne suit pas, l'émission prend un retard définitif. |
| T47 | OK (2026-10-10 21:45-22:20) | **Synthèse AVX2** (filtre polyphasé vectorisé sur les phases, 4 symboles à la fois) : 0,51 → 0,09 cœur par canal à 55,6 MS/s sur le Proxmox, identique à numpy à −123 dB ; codage J.83 dans les fils de synthèse ; un fil par 2 canaux (un par canal = 9 fils temps réel sur 4 CPU : décodage montant jusqu'à 220 ms en retard, ping 86-388 ms). **5 DS : en ligne sur 5 canaux, 215,6 / 20,2 Mbit/s, ping 91 ms, U 0 L 0.** 6 DS : SNR 38-39,8 dB sur les 6 canaux (page du modem), 208-209 Mbit/s descendant, mais montant 2,9 Mbit/s, ping 49-1060 ms, canal 6 perdu une fois. Ping de la config de référence (2 DS, un processus) sur le Proxmox : 89-93 ms, minimum 46-49 ms (grants en avance encore actifs). |
| T48 | analyse (2026-10-10 22:15-22:25) | Ping ~92 ms contre 56-72 au README : la sauvegarde v4 (9/10 22 h 29) donne 96 ms sur le Proxmox, la config de référence 89-93 ms. Ping toutes les 50 ms : min 44, moyenne 71 ms ; toutes les 0,5 s : 96 ms. Pas de régression : sans trafic, chaque ping attend un cycle requête → grant, et le MAP qui porte le grant est construit en tête de la file descendante (~36 ms) ; les 56-72 ms avaient été mesurés avec du trafic qui gardait les grants en avance actifs. |
| T49 | partiel (2026-10-10 22:45-23:05) | **5 DS + 2 US** (60 / 53 MHz, 5,12 Msym/s 64-QAM, `--us-target -20`). Réception large bande à l'horloge maître = 445 Mo/s d'USB à 55,616 MHz : réception réduite au plus petit débit qui contient les canaux. 55,616 / 3 : MER US1 14 dB (décimation impaire du FPGA = CIC seul) ; / 2 : MER 26,4 mais 333 Mo/s d'USB, émission en retard, débordement RX, modem oublié. Décimations paires seulement depuis. **5 DS à 48,664 MHz (7 éch./symbole) + 2 US à 24,332 MS/s : en ligne d'emblée, MER US1 27,4 dB, 145,8 / 12,4 Mbit/s** ; les 2 canaux montants portent ~1 Mo/s chacun, mais les processus montants prennent 70-570 ms de retard sous charge (démodulation à 24,3 MS/s, 4,75 éch./symbole). Profilage tenté : le surcoût fait oublier le modem. |
| T50 | partiel (2026-10-10 23:10-23:45) | **6 DS, nouvel essai** : ping 92 ms, 157,7 / 6,7 Mbit/s ; l'émission prend 1 à 6 ms de retard (« retard côté USB / pilote », L jusqu'à 629), canaux 2/3/4/6 perdus et retrouvés, montant bloqué. Écarté : réordonnancement du modem (trafic forcé sur le canal 1 : pareil), CPU moyen (radio 172 % sur 4 cœurs). Essayé sans effet : fils de calcul en priorité temps réel 40 (pire : 34 Mbit/s, modem oublié), blocs de 128 paquets + file 3 (145 / 5,8). Cause non trouvée. **5 DS @48,664 + 2 US, démodulation après DDC /2** (2,38 éch./symbole, MER 35,8 au lieu de 36,7 au test) : processus montants 50 → 29 % CPU, montant 12,4 → 15,8 Mbit/s ; mais radio sur 3 cœurs : émission en retard, modem oublié ; montants sur 1 cœur (radio sur 4) : modem stable, 122 / 8 Mbit/s, retard montant jusqu'à 711 ms. py-spy du processus montant : DDC 43 % du temps actif. |
| T51 | OK (2026-10-11 00:15-01:05) | **Voie montante en C** : égaliseur en AVX2, étiquettes → octets par mots entiers : 3,08 → 2,55 ms par rafale réelle de 16 640 symboles sur le Proxmox. En marche, une rafale de données coûte ~3,9 ms (≈ 6 Ko : le Python autour de la démodulation pèse autant qu'elle) ; sous envoi, chaque processus montant est à ~76 % de son cœur. 5 DS @48,664 + 2 US : 14,4 Mbit/s, radio en retard sur 3 cœurs, modem oublié 7 fois. **4 DS + 2 US : 173 / 21,5-22,3 Mbit/s, modem jamais oublié** (16 flux, 30 s : 21,1 ; ~1,5 Mo/s de données sur chaque canal, 99,8 % des rafales décodées, processus montants 55 %). Grant en avance à chaque MAP : 12 Mbit/s (retard montant 511 ms) ; `--tx-queue 3` : ping 82 ms, montant 21 Mbit/s. Chaque canal montant n'est utilisé qu'à ~43 % : le modem demande ~16 Ko à la fois, ~190 fois/s. |
| T52 | analyse (2026-10-11 01:10-02:00) | **4 DS + 2 US, pourquoi ~22 Mbit/s montants.** Profil du processus montant sous envoi (py-spy, fil de travail occupé 81 %) : DDC 15 %, filtre adapté 15 %, égaliseur + suivi 14 %, regard « bourrage » de chaque segment 11 %, ajustement d'égaliseur 5 %, RS 7 %. Regard de bourrage réservé aux grants en avance (drapeau `IUC_AHEAD`) : processus 55 → 47 %, attentes de réassemblage moy 1-2 ms. Demandes MTC au-delà de 255 mini-slots : le reste n'était pas accordé (corrigé : accordé aux MAP suivants), mais le modem ne demande en moyenne que 12-18 Ko. Sous envoi : ping 220-840 ms, 242 retransmissions / 18 s ; en traitant les demandes comme additives (essai) : 23,5 Mbit/s, ping 293 ms, mais 176-247 Ko/s de bourrage : le modem laisse des grants vides alors que des données attendent. Conclusion : le Sagem (mode routeur, NAT) ne fait pas passer plus de ~22-24 Mbit/s en montant, avec 1 comme avec 2 US ; la file est avant sa partie DOCSIS. UDP 25 Mbit/s : 12,9 reçus (46 % perdus), à 35 Mbit/s plus rien pendant 2-3 min. |
| T53 | OK (2026-10-11 01:00-01:30) | **Modem en bridge** (case « mode Routeur » décochée, il redémarre seul ; le PC reçoit 10.10.0.12 du CMTS) : 1 US : 17,7-21,1 Mbit/s montants, ping en charge 145 ms (au lieu de ~460 en routeur) ; 4 DS + 2 US : 21,5-22 Mbit/s, ping en charge 300-600 ms, 729 retransmissions : le plafond à 2 US n'est pas le NAT (conclusion de T52 fausse). **Échantillons en 16 bits** : gain négligeable (conversion UHD ~1 % d'un cœur ; le fil d'envoi est surtout noyau). **Trames USB de 16 Ko** (`--args type=b200,send_frame_size=16360 --tx-frames 64`) : fil d'envoi 23 → 16 % d'un cœur à 55,6 MS/s ; **6 DS : plus aucun retard d'émission ni décrochage (U 0 L 0), 204 Mbit/s descendants**, mais montant 6,7-9,9 Mbit/s : le modem demande peu de grants (~50/s), décodage montant à 100 %. 5 DS @48,664 + 2 US : toujours instable. 4 DS + 2 US + trames de 16 Ko : 172 / 20,5 Mbit/s, U 0 L 0. REG-REQ-MP du Sagem : 5.29 (canaux de réception) = 24, 5.24 (canaux d'émission) = 8, 7 profils de réception (TLV 48). |
| T54 | OK (2026-10-11 01:35-01:50) | **Trames USB de 16 Ko par défaut** (`--send-frame-size 16360`, `--tx-frames 64`) : même latence (36 ms) et réserve USB (19 ms), 4 DS + 2 US sans argument : 173 / 21,5-21,9 Mbit/s, U 0 L 0. **Profils de réception du Sagem** (TLV 48 de son REG-REQ-MP) : CLAB-8M-002, -003, -004, -005, -008, -016, -024, modules de 8 canaux adjacents, fréquences centrales 112-1002 MHz ; le CMTS prend le plus petit qui suffit : CLAB-8M-004 en 4 DS, CLAB-8M-008 en 5 et 6 DS. 5 DS marche avec le même profil : ce n'est pas lui qui bloque le montant en 6 DS. |
| T27 | OK (2026-10-09 03:24) | US 2,56 Msym/s 16-QAM + DS 256-QAM : en ligne ; sous 45,9 Mbit/s DS, ~1000 trames/s montantes décodées (0 échec), ping 0 % de perte (134 ms moyen), U 0 L 0. Devenus les défauts. |
| T24 | OK (2026-10-09) | Émetteur descendant en C : J.83A identique au bit près au numpy (64/256-QAM, appels enchaînés, flux DOCSIS réel), GNU Radio et boucles 64/256 OK ; chaîne complète 4,5× le temps réel (2,5× avant) |
| T21 | en cours | 2,5 min en ligne, U 0 L 0, marge TX ~34 ms. À prolonger à 10 min ; tester `ping 10.10.0.10`. |

Joins aussi le **modèle exact du modem**, sa version de firmware et sa version DOCSIS.

---

## Étape 4 : provisionnement et enregistrement (`mini_cmts.py --all-in-one`)

Même montage que l'étape 3. Ordre : lance le programme, puis allume le modem.

### T18 : tests sans radio
```bash
python3 tests/test_registration.py
python3 tests/test_provision_tap.py     # demande sudo (crée cmts0 si besoin)
```
- **Attendu :** `PASS` deux fois.

### T19 : bande accordée et DHCP
```bash
python3 mini_cmts.py --all-in-one --pcap
```
- **Attendu au démarrage :** `[PONT] modems <-> cmts0`, puis `[PROV] cmts0 10.10.0.1/24 : DHCP …` et le contenu du fichier de config.
- **Après le ranging :** `[US] REQ SID 0x0001: … -> grant au MAP suivant`, puis `[PONT] ↑ DHCP DISCOVER de 24:7f:20:a8:38:b8 … [docsis3.0]`,
  `[DHCP] DISCOVER … → OFFER 10.10.0.10`, `[PONT] ↓ DHCP OFFER …`, puis REQUEST / ACK.
- **Plus de séries de 17 REQ** : une REQ, un grant, une trame.
- **Si le DISCOVER n'arrive pas** : regarder les lignes `[US] rafale … non décodée` dans les grants IUC 5/6 et garder le pcap.

### T20 : ToD, TFTP et enregistrement
- **Attendu :** `[ToD] heure envoyée`, `[TFTP] RRQ « cm.cfg »` puis `transféré`, puis `[REG] … REG-REQ` avec le détail des TLV,
  `[REG] REG-RSP ok … flux montant réf 1 SFID 1 SID 0x0001`, puis `[REG] … REG-ACK ok -> MODEM EN LIGNE`, et l'état `online`
  sur la ligne d'état.
- **À noter :** les capacités annoncées par le modem (TLV 5 du REG-REQ), toute ligne `!!`, et le journal d'événements du modem.

### T21 : stabilité en ligne (10 minutes)
- **Attendu :** le modem reste `online`, plus de réinitialisation toutes les 2 à 3 minutes (elles venaient de l'échec du DHCP),
  `U 0`. Le modem répond au ping : `ping 10.10.0.10`.

---

## Accélération C du récepteur montant

### T22 : tests sans radio
```bash
python3 tests/test_cdsp.py         # C = références Python, vitesses
python3 tests/test_real_bursts.py  # vraies rafales du Sagem : MER ~36 dB attendue (29 avant)
```
### T23 : banc, modem en ligne
```bash
python3 mini_cmts.py --all-in-one
```
- **Attendu :** mêmes étapes qu'en T20, MER des rafales ~36 dB dans les lignes `[US]`, aucune ligne `[TX] !!` liée au décodage.
- **Puis, pour la latence :** `python3 mini_cmts.py --all-in-one --tx-frames 128 --tx-queue 6` (latence descendante ~41 ms au lieu de ~75) ;
  garder ces valeurs par défaut seulement si `marge TX min` reste positive et `L 0` pendant 10 minutes.

### T24 : émetteur descendant en C (sans radio)
```bash
python3 tests/test_dsdsp.py; python3 tests/test_phy_vs_grdtv.py; python3 tests/test_loopback.py 64; python3 tests/test_loopback.py 256
python3 ds_tx.py --bench; python3 ds_tx.py --bench --qam 256      # attendu : > 4x le temps réel
```

### T25 : débit montant (concaténation, grants en avance)
```bash
python3 mini_cmts.py --all-in-one          # réserves serrées par défaut
```
- Charger une page web et lancer un ping en même temps depuis le PC derrière la box.
- **Attendu :** les pings passent pendant le chargement ; sur la ligne d'état, `pont ↑` progresse bien au-delà de 18 trames/s
  sous charge, `concat` augmente, et `vides` reste une petite fraction des grants en avance.
- Comparer avec `--grant-ahead-ms 0` pour voir l'effet seul de la concaténation.

### T29 : agrégation de deux canaux descendants
```bash
python3 tests/test_bonding_tx.py                       # sans radio : chaque canal du signal composite démodulé seul
python3 mini_cmts.py --all-in-one --ds-channels 2      # banc
```
- **Attendu :** `RCC : profil 0010001002, canaux 1@306 MHz, 2@314 MHz`, `REG-ACK ok`, `trafic unicast agrégé sur 2 canaux` ;
  sous charge, la ligne d'état montre la répartition `N/M par canal`. Un fast.com depuis le PC derrière la box doit dépasser
  ce que donnait un seul canal (~45 Mbit/s).
- Si `service partiel` : le modem n'a pas acquis le canal 2 ; relancer.

### T30–T32 : montant ATDMA (DOCSIS 2.0)
```bash
python3 tests/test_real_bursts.py        # inclut deux concaténations 64-QAM 5,12 Msym/s réelles (égaliseur)
python3 tests/test_merged_cluster.py     # rafale collée à la précédente : trouvée au début du grant
python3 mini_cmts.py --all-in-one --us-mode atdma --us-mod 64qam --us-rate 32 --prov-args "--us-rate 20000000"
```
- **Attendu :** en ligne comme en TDMA ; ligne d'état `rafales N (décodées ≈N)`, MER modem ~25 dB (brute, avant égaliseur) ;
  aucune ligne `[TX] !!` ; `captures/bursts_ko/` vide ou presque.
- **Test de débit montant** (pas encore fait) : test d'envoi fast.com / speedtest depuis le PC derrière la box ; attendu
  bien au-delà des ~8 Mbit/s utiles du TDMA 16-QAM 2,56 (cm.cfg à 20 Mbit/s montant).
- Pour charger le descendant sans fausser le ping, envoyer la charge à l'eRouter (10.10.0.11), pas au modem.
