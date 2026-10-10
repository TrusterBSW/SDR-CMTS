# Mini-CMTS SDR : downstream EuroDOCSIS (étape 2) et ranging upstream (étape 3)

> **Étape 3 (upstream) :** voir la section [Étape 3](#étape-3--upstream-et-ranging-mini_cmtspy) plus bas.
> `ds_tx.py` reste l'émetteur downstream seul ; `mini_cmts.py` fait downstream + upstream sur le même B210.

Ce programme émet un downstream EuroDOCSIS (ITU-T J.83 Annex A, 64 ou 256-QAM, 6,952 Msym/s)
avec un USRP B210. Le flux transporte les messages MAC nécessaires pour qu'un vrai modem :

1. verrouille le downstream (QAM, FEC, MPEG) ;
2. se synchronise sur les messages **SYNC** (toutes les 10 ms) ;
3. lise l'**UCD**, qui annonce un canal upstream à 40 MHz, 1,28 Msym/s, QPSK ;
4. reçoive des **MAP** (toutes les 2 ms) qui contiennent une fenêtre *Initial Maintenance*
   toutes les 10 ms, puis **émette ses RNG-REQ** sur l'upstream.

Il n'y a pas encore de récepteur upstream. Le modem va donc tenter le ranging en boucle, en
augmentant sa puissance à chaque essai, puis recommencer son scan. C'est le comportement
attendu à cette étape.

## Fichiers

| Fichier | Rôle |
|---|---|
| `ds_tx.py` | Programme principal : émission B210, `--bench` ou `--out fichier.cfile` |
| `cmts/j83a.py` | Couche physique J.83A : randomisation, RS(204,188), entrelaceur I=12, mapping QAM différentiel, filtre RRC α=0,15 |
| `cmts/fastdsp.c` | Filtre polyphase en C, compilé automatiquement au premier lancement |
| `cmts/docsis_mac.py` | Messages SYNC, UCD et MAP, HCS/CRC, mise en paquets MPEG-TS (PID 0x1FFE) |
| `cmts/downstream.py` | Ordonnanceur et base de temps : l'horodatage 10,24 MHz est dérivé du compteur de symboles |
| `cmts/ts_demux.py` | TS → trames MAC DOCSIS → pcap Wireshark (servira aussi à la phase 1, l'écoute du réseau SFR) |
| `tests/` | Comparaison avec GNU Radio gr-dtv et test de bouclage complet |
| `mini_cmts.py` | **Étape 3** : downstream + réception upstream + ranging sur un seul B210 ; `--sim` sans matériel |
| `cmts/us_phy.py` | Couche physique montante TDMA : RS, embrouilleur x^15+x^14+1, QPSK, RRC α=0,25 ; démodulateur de rafales |
| `cmts/upstream.py` | Base de temps commune TX/RX et découpage des intervalles des MAP dans le flux RX |
| `cmts/ranging.py` | RNG-REQ → RNG-RSP, SID temporaires, Station Maintenance |
| `cmts/sim_modem.py` | Modem simulé (machine d'état CM) pour tester toute la boucle sans matériel |

## Validations déjà faites (sans matériel)

- `tests/test_phy_vs_grdtv.py` : randomiseur, RS et entrelaceur sont **identiques bit à bit**
  à ceux de GNU Radio gr-dtv.
- `tests/test_loopback.py 64|256` : modulation, puis démodulation indépendante. Le décodage
  différentiel reprend littéralement l'équation booléenne de l'EN 300 429 ; le désentrelacement,
  le décodage RS et le désembrouillage sont ceux de gr-dtv. Résultat : 100 % des paquets TS
  sont identiques à l'émission, et toutes les trames sont lues par tshark avec un HCS valide.
- Les horodatages SYNC suivent l'horloge symbole à ±1 coup de 10,24 MHz près.
- Les MAP sont contigus (pas de 80 minislots) et arrivent au moins 2 ms avant leur début.
- Le mapping des constellations a été relevé sur les figures 7 et 8 de l'EN 300 429 V1.2.1.

**Pas encore testé : l'émission réelle avec le B210**, qui n'était pas branché pendant le
développement.

## Montage

```
B210 [TX/RX] ── coupleur A ──┬── modem
                              └── 8 dB ── coupleur B ──┬── B210 n°2 (lab_monitor.py)
                                                       └── HackRF / RTL-SDR (observer l'upstream à 40 MHz)
```

- **Banc fermé uniquement.** Jamais de liaison avec la prise SFR.
- Niveau visé à l'entrée du modem : environ 0 dBmV (plage acceptée : −15 à +15 dBmV).
- Pour régler : commence avec `--gain 50` à travers un coupleur 1 vers 2 (niveau au modem ≈ gain − 50 dBmV), puis lis la puissance reçue
  sur `http://192.168.100.1`.

## Utilisation

```bash
python3 ds_tx.py --bench                      # vérifier que le PC tient le temps réel (> 1.2x conseillé)
python3 ds_tx.py --freq 474e6 --gain 50       # émettre
python3 ds_tx.py --freq 474e6 --qam 256       # en 256-QAM
python3 ds_tx.py --freq 474e6 --us-freq 60e6  # upstream annoncé à 60 MHz (pratique pour le B210 plus tard)
```

**Astuce :** le modem mémorise sa dernière fréquence downstream. Émettre sur l'ancienne
fréquence SFR, relevée sur la page d'état du modem avant de le débrancher, évite un scan
complet qui peut durer plusieurs minutes. Sinon, utilise une fréquence de la grille européenne
de 8 MHz, par exemple 474 MHz, 306 MHz ou 330 MHz.

## Ce que tu dois observer

1. Voyant downstream du modem : il passe de clignotant à fixe.
2. Page `192.168.100.1` : downstream *Locked*, puissance et SNR (au-delà de 30 dB en 64-QAM,
   c'est bon).
3. Voyant upstream qui clignote, et **bursts visibles vers 40 MHz** sur le HackRF ou un RTL-SDR
   (gqrx, SDR++), environ toutes les 10 ms, de plus en plus forts.
4. Dans les logs du modem : des timeouts *T3* / *No Ranging Response*. C'est normal, il n'y a pas
   encore de RNG-RSP.

## Si ça ne verrouille pas

| Symptôme | Piste |
|---|---|
| Aucun verrouillage | Niveau trop fort ou trop faible : varie `--gain` de 10 dB en 10 dB. Vérifie la fréquence. Essaie `--qam 256`. |
| Verrouillage QAM sans SYNC (*T1 timeout*) | Note le message exact du journal du modem et rapporte-le. |
| Un `U` s'affiche dans la console | Underrun USB ou CPU : vérifie `--bench`, ferme les autres programmes, utilise un port USB 3. |

## Limites connues

- **Pas de MDD :** un modem DOCSIS 3.0 devrait se rabattre en mode 2.0.
- **Bouclage du compteur :** le timestamp 32 bits boucle toutes les 419 s. Le comportement des
  minislots à ce moment-là n'a pas encore été vérifié avec un vrai modem.
- **Upstream** : le ranging est fait par `mini_cmts.py` (étape 3). Les grants de données, DHCP,
  TFTP et l'enregistrement restent à faire.

## Phase 1 : écoute du downstream SFR (réception seule)

`sfr_capture.py` démodule une capture IQ d'un canal EuroDOCSIS et ne garde **que les messages de
gestion diffusés à tous les modems** (SYNC, UCD, MAP, MDD). Les trames de données et les messages
unicast adressés à un modem sont jetés. Les captures IQ brutes contiennent tout le canal : supprime-les
une fois analysées.

```bash
# B210 sur RX2 : forcer master_clock_rate pour avoir EXACTEMENT 20 MS/s (sinon UHD prend 20,48 MS/s)
uhd_rx_cfile -a "type=b200,master_clock_rate=40e6" -A RX2 --spec A:A -f 282e6 -r 20e6 -g 38 -N 20000000 -s ch.sc16
./sfr_capture.py ch.sc16 --fmt sc16 --pcap mgmt.pcap
python3 tests/analyze_mgmt.py mgmt.pcap 2      # statistiques SYNC/MAP (2 = taille de minislot en ticks)
python3 tests/sync_jitter.py ch.sc16 sc16 20e6  # précision des horodatages SYNC
```

### Ce qui a été observé sur le réseau SFR (06/10/2026)

| Élément | SFR | Mon émetteur |
|---|---|---|
| Plan de fréquences | DOCSIS de 282 à 530 MHz (6,952 Msym/s, 256-QAM, Annex A) ; TV DVB-C à 6,875 Msym/s sur 130–274 et 538–698 MHz | 6,952 Msym/s, 64 ou 256-QAM |
| Canaux primaires (d'après le MDD) | 282 et 314 MHz ; 290/298/306 ne sont pas primaires (pas de SYNC/MAP) | un seul canal primaire |
| **Orientation du spectre** | **inversée** par rapport à ma lecture de l'EN 300 429, confirmé par le B210 et le HackRF | **inversée par défaut** (`--no-invert` pour revenir) |
| PID utilisés | 0x1FFE (DOCSIS) et 0x1FFF (paquets nuls) uniquement | identique |
| SYNC | toutes les 10,0 ms, horodatage à l'octet 31 du paquet TS, gigue d'environ 100 ns crête à crête | identique |
| MAP | contigus, 256 minislots de 2 ticks (3,2 ms), envoyés au moins 2,3 ms à l'avance | contigus, 80 minislots de 4 ticks (2 ms), au moins 2,25 ms à l'avance |
| Initial Maintenance | 65 minislots environ toutes les 475 ms | 48 minislots toutes les 10 ms (plus fréquent, pratique en labo) |
| Backoffs ranging / data | 2–7 / 2–8 | 3–6 / 2–8 |
| UCD | type 29 (ATDMA) ; canaux montants à 34,8 / 41,2 / 47,6 / 54,0 / 60,4 MHz, 5,12 Msym/s, donc **sub-split 5–65 MHz** | type 2 (TDMA), 40 MHz, 1,28 Msym/s |
| MDD | présent (DOCSIS 3.0) | absent pour l'instant |

Le HackRF suffit à décoder les canaux SFR (MER d'environ 25 dB), mais le B210 est bien meilleur (MER d'environ 34 dB).

### Mesures du 06/10/2026, entre 18 h et 18 h 30 (réception seule, comptages agrégés)

```bash
python3 count_modems.py --count 40         # modems en ligne (SID unicast vus dans les MAP)
python3 network_load.py --passes 3          # charge descendante des 32 canaux (~2,2 s par canal)
python3 upstream_analysis.py --count 10     # canaux montants : UCD + utilisation d'après les MAP
```

| Mesure | Résultat |
|---|---|
| SID unicast actifs dans le domaine MAC | 59 (stable dès la 1re seconde) ; 8 vus en maintenance périodique en 15 min |
| Descendant | 32 × 256-QAM = 1 605 Mbit/s ; données 3,7 Mbit/s en moyenne (0,2 %) ; signalisation 26 Mbit/s |
| Montant | 4 segments × 5 canaux (34,8 / 41,2 / 47,6 / 54,0 / 60,4 MHz), 5,12 Msym/s, 16-QAM : 102 Mbit/s bruts par segment ; utilisation d'environ 0,1 % |

## Étape 3 : upstream et ranging (`mini_cmts.py`)

### Choix du matériel : le même B210 pour les deux sens

Le B210 fait le downstream sur **TX/RX** et reçoit l'upstream sur **RX2** (voie A). Les synthétiseurs
TX et RX de l'AD9361 sont indépendants : émettre à 306 MHz et recevoir à 60 MHz en même temps ne pose pas de problème.

La raison décisive est l'**horloge commune**. Un CMTS doit savoir à quel instant exact chaque
mini-slot arrive chez lui, et mesurer l'avance ou le retard du modem à mieux qu'une fraction de
symbole près (l'unité du *Timing Adjust* vaut 97,7 ns). Avec un seul B210, TX et RX partagent le même
compteur : chaque horodatage DOCSIS correspond à un échantillon RX exact, sans dérive. Avec un
second SDR, il faudrait une référence 10 MHz + PPS commune, ou bien suivre la dérive en démodulant le
downstream (± 20 ppm sur un HackRF, soit 20 µs de dérive par seconde).

| Option | Verdict |
|---|---|
| **Même B210** (TX/RX + RX2) | ✅ retenu : même horloge, horodatage matériel, 12 bits, USB 3 suffisant (13,9 + 3,5 MS/s) |
| 2e B210 | possible avec 10 MHz + PPS communs ; il reste plus utile en moniteur (`lab_monitor.py`) |
| HackRF | 8 bits, ±20 ppm, pas d'horodatage aligné avec le B210 : bien comme **observateur**, pas comme récepteur CMTS |
| RTL-SDR | 8 bits, 2,4 MS/s, horodatage USB imprécis : non |

Seule contrainte : UHD n'accorde pas l'AD9361 sous **50 MHz**. L'upstream est donc annoncé à
**60 MHz** par défaut, une fréquence que SFR utilise déjà (60,4 MHz) et que ton modem sait donc émettre.
Le LO RX est réglé 3 MHz au-dessus, à 63 MHz, pour que la rafale ne tombe pas sur la fuite du LO au centre.
Le FPGA ramène ensuite le signal à 0 Hz. On garde ainsi l'horloge maître validée de 27,8 MHz.
Toute fréquence entre 53 et 64 MHz convient. Le canal montant EuroDOCSIS s'arrête à 65 MHz.
`--us-freq 40e6` reste possible : le LO passe alors à 52 MHz avec un décalage de 12 MHz, et l'horloge
monte automatiquement à 55,6 MHz.

### Montage

Matériel : un atténuateur 8 dB et deux coupleurs 1 vers 2 (un troisième en option).

```
B210 TX/RX ── coupleur A [entrée] ──┬── sortie 1 ── modem
                                     └── sortie 2 ── 8 dB ── coupleur B [entrée] ──┬── B210 RX2
                                                                                    └── HackRF (observation à 60 MHz, amp. OFF)
```

- **Downstream :** un seul passage dans un coupleur entre le B210 et le modem, comme le montage validé (`--gain 49`).
- **Upstream :** la rafale du modem passe d'une sortie du coupleur A à l'autre, donc par son **isolation**
  (15 à 25 dB, non mesurée), puis par le 8 dB et le coupleur B (3,5 dB). Au total, environ 27 à 37 dB.
- **Pire cas** (modem à sa puissance max, environ +9 dBm) : environ −17 dBm au plus sur RX2, plutôt −22 à −27 dBm.
  Les limites Ettus sont −15 dBm conseillé et 0 dBm au maximum. Un 3e coupleur en cascade devant RX2 ajoute 3,5 dB de marge.
- **Ne mets pas le modem sur l'entrée du coupleur A :** la rafale n'y perdrait que 15 dB, soit −6 dBm sur RX2.
- **Laisse tous les ports occupés** (ou bouchon 75 Ω) : un port en l'air dégrade l'isolation.
- **Ordre :** lance `mini_cmts.py`, puis allume le modem. Pour arrêter, éteins le modem d'abord, puis Ctrl-C.
- Le 2e B210 (`lab_monitor.py`) peut remplacer le HackRF sur le coupleur B.

### Ce que fait le programme

1. Il émet le même downstream que `ds_tx.py` (SYNC, UCD, MAP). L'UCD annonce un nouveau préambule à
   faible autocorrélation, dont chaque IUC utilise un segment distinct. L'ancien motif `cc…0d…` était périodique
   et rendait l'instant d'arrivée ambigu de quelques symboles.
2. Il découpe le flux RX selon les MAP qu'il a lui-même émises. Un seuil d'énergie évite de démoduler
   le bruit ; les fenêtres retenues sont démodulées : corrélation du préambule (temps, phase, fréquence, puissance),
   QPSK, désembrouillage, RS, puis HCS/CRC.
3. **RNG-REQ en Initial Maintenance** → RNG-RSP (SID temporaire, *continue*, corrections de temps,
   de puissance et de fréquence).
4. **Station Maintenance** pour ce SID → RNG-REQ → RNG-RSP *success* dès que l'erreur reste sous
   0,15 µs, 1,5 dB et 300 Hz, puis une interrogation par seconde (le T4 du modem vaut 30 s). Une seule interrogation
   est en vol à la fois, pour ne jamais corriger deux fois avant que le modem ait appliqué la correction.
5. Les **REQ** (demandes de bande) sont décodées et affichées. On n'accorde pas encore de grant de données :
   c'est l'étape 4 (DHCP, TFTP, ToD, REG-REQ/RSP).

La latence entre une rafale et sa RNG-RSP à l'antenne est affichée ; elle doit rester sous 200 ms
(T3 du modem). Avec les réglages par défaut (`--tx-queue 6 --tx-frames 128`, environ 41 ms de chaîne descendante),
elle vaut environ 55 ms. Chaque bloc TX porte son heure d'émission : après un underflow, le B210 reprend à l'heure
prévue (un trou) au lieu de décaler tout le downstream, ce qui fausserait la référence de temps du récepteur.
Les blocs arrivés trop tard sont jetés et comptés en `L`. `--tx-untimed` rétablit l'ancien comportement.

### Utilisation

```bash
python3 tests/test_upstream_phy.py          # couche physique seule (bruit, fréquence, timing)
python3 tests/test_ranging_sim.py           # boucle complète avec modem simulé, 6 scénarios
python3 mini_cmts.py --sim 3                # idem, avec le journal détaillé

python3 mini_cmts.py --freq 306e6 --gain 49 --us-gain 10 --no-rsp --save-bursts captures/bursts   # écoute seule
python3 mini_cmts.py --freq 306e6 --gain 49 --us-gain <réglé en T15>                            # ranging
```

Réglages utiles : `--us-gain` (gain RX, 30 par défaut), `--us-target` (puissance visée en dBFS, −20),
`--us-freq`, `--sm-period`, `--tx-queue` (tampon downstream, 6 blocs par défaut depuis le 2026-10-09).

**Priorité temps réel.** `mini_cmts.py` passe tout le processus en `SCHED_RR 50` (`--rt-prio`, 0 pour désactiver),
pour que l'activité du PC ne retarde plus l'alimentation du B210. Il faut l'autoriser une fois, puis se reconnecter :
```bash
sudo mkdir -p /etc/security/limits.d        # absent par défaut sur Arch
printf '@usrp - rtprio 99\n@usrp - nice -20\n' | sudo tee /etc/security/limits.d/99-usrp.conf
ulimit -r     # après reconnexion : 99
```
Sans cette autorisation, le programme le signale au démarrage et tourne en priorité normale.
Sous KDE, le terminal est lancé par `systemd --user`, qui survit à une reconnexion : si `ulimit -r` reste à 0,
redémarre le PC, ou tape `sudo prlimit --pid $$ --rtprio=99:99 --nice=40:40` dans le shell avant de lancer le programme.
Après `prlimit`, `ulimit -r` affiche encore 0 sous zsh (il garde sa propre copie des limites) : c'est sans effet,
les programmes lancés ont bien 99. La seule vérification qui compte est la ligne verte `priorité temps réel SCHED_RR 50`.

**CPU.** Le B210 est sur le contrôleur USB 00:14.0, dont l'interruption est traitée par le CPU 0. Avec la priorité temps réel, un thread
du programme posé sur ce cœur affame le traitement USB (ksoftirqd tourne en priorité normale) et l'émission se bloque
jusqu'à plusieurs centaines de ms. `--cpus auto` (défaut) garde donc tout le processus hors du cœur qui reçoit les interruptions
USB et hors de son jumeau hyperthread (CPU 0 et 4 ici) ; `--cpus all` désactive cet isolement, `--cpus 1-3,5-7` le force.
`--performance` passe tous les CPU dans le gouverneur `performance` le temps du lancement (via sudo), puis restaure
le gouverneur et la préférence d'énergie d'origine à l'arrêt. Sans cette option, le programme signale un gouverneur autre que `performance`.

**Défauts du banc.** `python3 mini_cmts.py` sans option équivaut à `--freq 306e6 --gain 49 --us-gain 20 --performance
--cpus auto --tx-frames 256`. Chaque lancement sur le matériel écrit `captures/cmts_<date>_<heure>.log` (la console sans
couleurs) : plus besoin de `tee` (`--log none` pour s'en passer). Le pcap reste à la demande : `--pcap` seul écrit
`captures/cmts_<date>_<heure>.pcap`, `--pcap FICHIER` choisit le nom. `--no-performance` garde le gouverneur actuel.

**Réserve d'émission.** `--tx-queue` (6 blocs, 15 ms ; 12 avant le passage en C) couvre seulement l'encodeur et la mise en forme. Contre un retard du thread
qui appelle `send()`, la seule réserve est ce qu'UHD a déjà passé à l'USB : `--tx-frames` (128 par défaut, 2040 échantillons
chacune, soit 18,8 ms ; 256 avant le passage en C ; cela ajoute autant de latence). Latence descendante ~41 ms (75 avant). La ligne d'état affiche `marge TX min` : l'avance minimale, sur les 5 s
écoulées, avec laquelle un bloc a été remis à UHD avant son heure d'antenne. Sous 0, des paquets sont jetés. UHD compte un `L`
par paquet USB en retard (0,147 ms), pas par bloc.

**Blocages de l'émission.** Un chien de garde (`cmts/stallwatch.py`) remplace les `U`/`L` en vrac d'UHD par une ligne datée par blocage :
durée, `t=` (même échelle que les lignes `[US]`), U/L perdus, puis le temps CPU de chaque thread pendant le blocage
et où chacun se trouvait (pile Python, état noyau R/S/D). « Chien de garde à l'heure » avec le thread principal dans
`send()` : attente du pilote USB. « Chien de garde bloqué / en retard » : un thread tenait le GIL, celui qui a consommé
du CPU pendant le blocage. Des U/L sans blocage du programme sont signalés à part (retard côté USB).

**Pcap.** `--pcap FICHIER` enregistre chaque trame montante décodée (RNG-REQ, REQ…) et chaque RNG-RSP envoyée,
horodatées sur la base de temps DOCSIS : l'écart entre une rafale et sa réponse dans Wireshark est celui du câble.
Le fichier est écrit au fil de l'eau, Wireshark peut l'ouvrir pendant l'essai (filtre `docsis_mgmt`, ou `docsis` pour voir aussi les REQ).
Les rafales déjà enregistrées avec `--save-bursts` se convertissent avec
`python3 tools/bursts2pcap.py captures/bursts -o captures/bursts.pcap`.

**Verbosité.** La console n'affiche que l'essentiel par défaut ; le journal `captures/cmts_*.log` reçoit toujours **tout** :

| Option | En plus à l'écran |
|---|---|
| (aucune) | démarrage, ligne d'état toutes les 5 s, toute ligne `!!`, vie du modem (ranging terminé, oublié, enregistrement, en ligne), DHCP/TFTP/ToD, blocages TX/RX |
| `-v` | échanges RNG-REQ/RNG-RSP, demandes de bande, rafales non décodées, détails sous une ligne (TLV du REG-REQ, instantané du chien de garde, fichier de config), messages `[INFO]` d'UHD |
| `-vv` | chaque trame qui traverse le pont |
| `-vvv` | scan d'énergie brute `[US-BRUT]`, bruit réseau du pont (IPv6, mDNS, IGMP) |

**Couleurs.** La console est colorée d'après le contenu des lignes : rouge pour les problèmes (`!!`, compteurs
`U`/`L`/`O` non nuls, rafales non décodées), cyan pour les `RNG-REQ`, jaune pour `RNG-RSP continue`, vert pour
`success` et `RANGING TERMINÉ`, magenta pour les `REQ`, gris pour `[US-BRUT]`, et l'heure en bleu sur la ligne d'état.
`--color auto` (défaut) ne colore que dans un terminal. `--color always` garde les couleurs à travers `tee`
(relire avec `less -R`), et `--color never` les supprime.

### Points à confirmer sur le vrai modem

- **Ordre des bits** : la spec envoie le MSB de chaque symbole RS en premier, mais elle note aussi que la
  convention MAC est LSB en premier. Le récepteur essaie les deux ; il affiche `octets LSB-first` si c'est le second cas.
- **Sens du spectre montant** : le récepteur essaie aussi le spectre conjugué et affiche `spectre inversé` si c'est le cas.
- Les rafales non décodées sont affichées avec leur puissance, MER et corrélation. Avec `--save-bursts`,
  l'IQ est enregistré pour une analyse hors ligne.

## Étape 4 : bande montante, provisionnement et enregistrement

`mini_cmts.py` se comporte maintenant en CMTS « ponté » : il accorde la bande demandée par le modem, fait passer ses trames
Ethernet vers une interface Linux `cmts0`, et gère l'enregistrement DOCSIS. Les serveurs de provisionnement (DHCP, TFTP,
heure) sont à part, comme dans une tête de réseau classique.

```
modem ──DOCSIS──> mini_cmts.py ──pont L2──> cmts0 (10.10.0.1) <── provision.py (DHCP, TFTP, ToD)
                  │ grants, REG-REQ/RSP/ACK                     <── ou tes propres serveurs (dnsmasq, ISC…)
```

### Deux façons de lancer
```bash
python3 mini_cmts.py --all-in-one     # tout en un : lance aussi provision.py (sudo) sur cmts0
python3 mini_cmts.py                  # CMTS seul : à toi de servir DHCP/TFTP/ToD sur cmts0
```
`cmts0` est créée au premier lancement (sudo : `ip tuntap add … user <toi>`, lien activé, ignorée par NetworkManager) et dure
jusqu'au redémarrage. `--tap none` désactive le pont. Avec tes propres serveurs, donne une adresse à `cmts0`
(`sudo ip addr add 10.10.0.1/24 dev cmts0`) ou relie-la à un pont Linux vers un vrai réseau isolé. Il faut au minimum :
DHCP avec options 1, 2, 3, 4 (serveur d'heure), `siaddr` + nom du fichier (66/67) ; TFTP du fichier de config ; heure RFC 868
(UDP 37, facultatif mais attendu).

### `provision.py`
```bash
sudo python3 provision.py                          # sur cmts0 : DHCP 10.10.0.10-99, TFTP « cm.cfg » généré, ToD
sudo python3 provision.py --config mon.cfg         # sert ton propre fichier de configuration
python3 provision.py --make-config cm.cfg          # écrit seulement le fichier généré (sans root)
python3 provision.py --show-config mon.cfg         # affiche un fichier de config et vérifie ses MIC
```
Le fichier généré (100 octets) : accès réseau, 4 CPE max, pas de chiffrement (BPI), un flux montant « best effort » à 1 Mbit/s
et un flux descendant à 20 Mbit/s, CM MIC et CMTS MIC. `--ds-rate`, `--us-rate`, `--max-cpe` le règlent ; `--prov-args`
de `mini_cmts.py` les transmet (`--prov-args "--config mon.cfg"`).

### Secret partagé (CMTS MIC)
Le CMTS vérifie le CMTS MIC de chaque REG-REQ avec `--shared-secret` (par défaut `docsis-lab`, le même que `provision.py`).
Un MIC faux est signalé en rouge mais accepté, sauf avec `--strict-mic`. Avec un fichier fait par un autre outil, mets le même
secret des deux côtés.

### Ce que fait le CMTS
- **Bande montante** : chaque REQ (ou requête « piggyback » dans un en-tête étendu) reçoit un grant au MAP suivant :
  IUC 5 (données courtes) jusqu'à 12 mini-slots, IUC 6 au-delà. Un MAP s'allonge (jusqu'à 320 mini-slots) pour qu'un grant
  de 255 mini-slots tienne avec l'IM.
- **Ack Time des MAP** : il vaut maintenant le dernier mini-slot montant réellement traité. Avant, il annonçait un traitement
  qui n'avait pas encore eu lieu : le modem prenait sa REQ sans réponse pour une collision et la renvoyait aussitôt (les séries
  de 17 REQ en ~115 ms des essais T17).
- **Pont** : trames du modem → `cmts0` (FCS Ethernet vérifié), trames de `cmts0` → Packet PDU descendants. Chaque trame est
  résumée dans le journal (`[PONT] ↑ DHCP DISCOVER …`, `↓ TFTP DATA …`), 20 lignes/s au plus.
- **Enregistrement** : REG-REQ → vérification du CMTS MIC, un SFID par flux de service, le SID du ranging pour le premier flux
  montant, réponse aux capacités du modem (version plafonnée à DOCSIS 1.1 ; concaténation, fragmentation, PHS, DCC et multicanal
  refusés) → REG-RSP ; REG-ACK → `MODEM EN LIGNE`. Un REG-REQ répété reçoit la même REG-RSP.
- Non géré : REG-REQ-MP (DOCSIS 3.0 multi-fragments ; sans MDD sur le downstream, un modem 3.0 utilise le REG-REQ classique),
  BPI+, fragmentation et concaténation montantes.

### Tests sans radio
```bash
python3 tests/test_registration.py     # rafale de données IUC 6 décodée, REG-REQ/RSP/ACK, MAP allongé, Ack Time
python3 tests/test_provision_tap.py    # provision.py via cmts0 (sudo) : DHCP, ARP, ToD, TFTP + MIC
```

## Accélération C du récepteur montant (2026-10-09)

`cmts/usdsp.c` (compilé automatiquement au premier lancement en `cmts/_usdsp.so`, chargé par `cmts/cdsp.py`)
contient les calculs par symbole et par octet du récepteur de rafales :

| Fonction | Rôle | Prévu pour |
|---|---|---|
| `fir_cr` | filtre adapté | toute largeur de canal |
| `interp_symbols` | prélèvement des symboles à l'instant exact (sinus cardinal fenêtré, 16 coefficients, 1024 phases) | tout rapport échantillons/symbole (0,16 à 5,12 Msym/s) |
| `dd_track` | suivi de phase décisionnel du 2ᵉ ordre | toute constellation : QPSK aujourd'hui, 8 à 64-QAM (ATDMA) demain |
| `rs_decode`, `rs_encode` | Reed-Solomon GF(256) | T = 0 à 16 (ATDMA), toute longueur |

Les fonctions sont sans état : plusieurs canaux montants pourront être décodés en parallèle. `ctypes` relâche le GIL
pendant chaque appel : décoder une rafale ne retarde plus l'émission descendante. Si aucun compilateur C n'est
disponible, des versions Python (plus lentes) prennent le relais.

Rafale de 1500 octets : **2 à 3 ms au lieu de ~69 ms**, et le GIL n'est plus tenu plus de 1,5 ms (contre 46 ms avant,
plus que la réserve d'émission). La MER mesurée sur les vraies rafales du Sagem gagne ~7 dB (36 dB au lieu de 29) :
l'ancienne méthode prenait l'échantillon le plus proche sur une grille ×8 sans le décalage fractionnaire.
`python3 tests/test_cdsp.py` compare le C aux références Python (600 blocs RS avec 0 à T+2 erreurs, interpolation,
suivi de phase QPSK et 64-QAM) et mesure les vitesses.

## Accélération C de l'émetteur descendant (2026-10-09)

`cmts/dsdsp.c` (compilé en `cmts/_dsdsp.so`, chargé par `cmts/cdsp.py`) :

| Fonction | Rôle | Prévu pour |
|---|---|---|
| `j83a_encode` | codeur J.83A complet : brassage, RS(204,188), entrelaceur I=12, symboles, codage différentiel, QAM | 64 et 256-QAM ; l'état (entrelaceur, rotation) appartient à l'appelant : un codeur par canal |
| `mix_add` | place un canal à son décalage de fréquence et l'ajoute aux autres, phase continue d'un bloc à l'autre | agrégation de plusieurs canaux descendants |

Le codeur C est **identique au bit près** au codeur numpy (gardé en référence : `J83AEncoder.encode_py`, et en
secours sans compilateur), vérifié sur 12 appels enchaînés en 64 et 256-QAM et sur un flux DOCSIS réel
(`tests/test_dsdsp.py`) ; la comparaison avec GNU Radio (`tests/test_phy_vs_grdtv.py`) et les boucles émission →
réception (`tests/test_loopback.py 64|256`) passent toujours.

Chaîne descendante complète (MAC + codage + mise en forme), `ds_tx.py --bench` : **4,5× le temps réel au lieu de 2,5×**
(0,22 cœur par canal au lieu de 0,41). Codage seul : 0,07 cœur au lieu de 0,44 ; mélange d'un canal à 27,8 MS/s :
0,04 cœur au lieu de 0,12. Le poste principal restant est la mise en forme (`fastdsp.c`, déjà en C).

## Débit montant : concaténation et grants en avance (2026-10-09)

**Constat (essai T23, page web derrière le modem) :** le débit montant plafonnait à **18 trames/s** avec
`--tx-frames 128 --tx-queue 6` et à **10 trames/s** avec 256/12, quelle que soit la taille des trames. Chaque trame attendait
son propre aller-retour « demande → MAP » (~55 ms, ou ~85 ms avec les grandes réserves), et les pings faisaient la queue
derrière le trafic TCP. Un vrai CMTS a un aller-retour de 2 à 4 ms ; ici le SDR et l'USB en imposent ~55.

**Correctifs :**
- **Concaténation** acceptée dans la REG-RSP (le Sagem l'annonce) : le modem met plusieurs trames dans une rafale
  (en-tête FC 0xF8 : FC_TYPE 11, FC_PARM 11100 ; une première version utilisait 0xDC à tort et jetait ces rafales, soit 2/3 du trafic montant de l'essai T25). Le CMTS découpe la rafale et traite chaque trame (données, gestion, requête). Le fichier de config
  généré lève la limite par défaut de 1522 octets (TLV 24.14 = 0).
- **Grants en avance** (`--grant-ahead-ms`, 100 par défaut, 0 = désactivé) : après chaque demande d'un SID, le CMTS lui
  accorde la même taille à chaque MAP, sans attendre la demande suivante. Arrêt dès une demande de 0 mini-slot, dès
  qu'un de ces grants revient vide (file du modem vide), ou à la fin de la fenêtre (horloge DOCSIS). Coût : environ un
  aller-retour de grants inutilisés à la fin de chaque salve.
- Plafonds de débit du fichier de config généré au-dessus de la capacité physique : 5 Mbit/s montant (1 avant, qui
  bridait le modem), 50 Mbit/s descendant.
- La ligne d'état affiche `grants N demandés + M en avance (K vides), concat C`.
- Le journal reçoit toutes les trames du pont ; seule la console est limitée à 20 lignes/s.

Limite physique attendue : 1,28 Msym/s QPSK = 2,56 Mbit/s bruts, ~2,2 Mbit/s utiles après FEC, soit ~180 trames de 1500 octets
par seconde (contre 18 trames/s avant).

## Modulations supérieures (nuit du 2026-10-09)

Nouveaux réglages par défaut, validés sur le Sagem 3686 :

| | Avant | Maintenant | Débit brut |
|---|---|---|---|
| Downstream | 64-QAM, gain 49 | **256-QAM, gain 45** | 38 → **51 Mbit/s** (40,8 Mbit/s acheminés sans perte, 45,9 avec la file à 2 ms) |
| Upstream | QPSK, 1,28 Msym/s (1,6 MHz) | **16-QAM, 2,56 Msym/s (3,2 MHz)** | 2,56 → **10,24 Mbit/s** |

Retour aux anciens réglages : `--qam 64 --gain 49 --us-rate 8 --us-mod qpsk`.

**Mesures du modem (page web, 03:03, en 64-QAM gain 49) :** SNR descendant 48,6 dB pour 16,4 dBmV reçus (un peu au-dessus
de la plage −15/+15 dBmV : d'où le gain 45, ~12 dBmV), émission montante 42,8 dBmV.

**16-QAM montante (DOCSIS 1.x TDMA) :** seules les rafales de données (IUC 5/6) passent en 16-QAM ; REQ, IM et SM restent en
QPSK. Le préambule d'une rafale 16-QAM est lui-même en 16-QAM (la longueur de préambule de l'UCD est alors un multiple de
4 bits) : sans la bonne correspondance bits ↔ points, la rafale est introuvable. La correspondance a été retrouvée sur les
rafales du modem par `tools/find_qam16_map.py` (essai de ~10 000 correspondances plausibles, celle qui fait passer le RS) :
étiquette I1 Q1 I0 Q0, I1/Q1 = signe (1 = positif), I0/Q0 = amplitude (1 = point extérieur). Les rotations et symétries
de la constellation donnent des correspondances équivalentes ; l'outil garde celle qui décode sans inversion du spectre.

**Corrigé au passage :**
- la concaténation utilisait l'en-tête FC 0xDC au lieu de 0xF8 : les rafales concaténées étaient jetées (2/3 du trafic
  montant de l'essai fast.com, d'où les pings perdus) ;
- la file descendante : la gestion (RNG-RSP, REG-RSP) passe avant les données (une RNG-RSP était partie 491 ms après la
  rafale, au-delà du T3 de 200 ms) ; les données attendant plus de `--ds-queue-ms` (100) sont jetées ; plusieurs petites
  trames partagent les paquets MPEG ;
- une rafale de données n'est plus plafonnée à 2048 octets (le modem concatène jusqu'à 36 trames, 3030 octets) ;
- le CMTS MIC excluait mal les TLV que le modem ne recopie pas (57, et les TLV SNMP 53 à 55).

`provision.py --snmp-community NAME` ajoute un accès SNMP en lecture (TLV 53), mais le firmware SFR ne répond pas à SNMP
côté câble.

## Multicanal : agrégation de deux canaux descendants (DOCSIS 3.0, nuit du 2026-10-09)

```bash
python3 mini_cmts.py --all-in-one --ds-channels 2      # 306 + 314 MHz, gain 48 automatiquement
```

**Mesuré sur le Sagem 3686 :** enregistrement DOCSIS 3.0 avec deux canaux, données réparties sur les deux, **91,8 Mbit/s
descendants acheminés sans perte** (U 0 L 0, file à 9 ms au plus, ping 16/16 pendant la charge), soit près du double d'un
canal 256-QAM. Avec toutes les trames forcées sur le canal 2 (`MINI_CMTS_BOND_CH=2`, diagnostic), le modem répond à 20 pings
sur 20 : il reçoit et resséquence bien le second canal.

**Ce que fait le CMTS :**
- `--docsis30` (implicite avec `--ds-channels 2`) : un **MDD** (MAC Domain Descriptor, type 33, version 4) par seconde sur
  chaque canal : liste des canaux descendants (format repris du MDD du réseau SFR capturé le 06/10), canal montant, IPv4
  seul, pas de chiffrement précoce, rapport détaillé des profils de réception demandé. Le modem passe alors en mode 3.0 :
  il envoie un **REG-REQ-MP** (type 44) au lieu du REG-REQ ; le CMTS répond par une **REG-RSP-MP** (type 45).
- **RCC** (TLV 49) construite à partir du profil de réception que le modem détaille (CLAB-8M-002 : un module, deux canaux,
  le premier primaire) ; **DSID** (TLV 50) de resséquencement sur les canaux 1 et 2.
- **Émission :** deux canaux mis en forme à 4 échantillons/symbole, décalés de ±4 MHz (`mix_add` en C), additionnés ;
  B210 à 27,808 MS/s centré sur 310 MHz (même horloge maître). Le canal secondaire porte MDD, données et paquets nuls ;
  SYNC, UCD et MAP restent sur le primaire.
- **Données :** après le REG-ACK, chaque trame unicast reçoit un en-tête étendu DS (type 8 : DSID + numéro de séquence) et
  part sur le canal le moins chargé ; le broadcast reste sur le primaire. Ligne d'état : `pont ↑… ↓… (N/M par canal, …)`.
- **REG-ACK « Partial Service » (30)** : le modem n'a pas acquis un canal (il le liste) ; il est compté en ligne, sans
  agrégation s'il ne reste que le primaire. Arrivé 1 fois sur 5 essais : relancer suffit.

**Limites connues :** deux canaux seulement (trois et plus demandent une horloge de 41,7 MHz et de revoir la réception
montante) ; l'agrégation montante n'est pas faite ; le bruit montant mesuré monte de ~10 dB en mode deux canaux (MER des
rafales ~29 dB au lieu de 33, sans gêne pour la 16-QAM), probablement un couplage émission → réception dans le B210 ;
chaque canal est l'image I/Q de l'autre autour du centre d'émission (réjection du B210 ~35–40 dB).

**Piège rencontré :** un générateur de charge qui attend en boucle active, non épinglé, peut tomber sur le CPU 0 (interruptions
USB du B210) et provoquer des pertes « côté USB ». Avec des pauses, plus aucune perte jusqu'à 92 Mbit/s.

## Upstream ATDMA (DOCSIS 2.0) : 64-QAM à 5,12 Msym/s (nuit du 2026-10-09)

```bash
python3 mini_cmts.py --all-in-one --us-mode atdma --us-mod 64qam --us-rate 32
# configuration maximale testée (2 canaux descendants + ATDMA), débits du cm.cfg relevés :
python3 mini_cmts.py --all-in-one --ds-channels 2 --us-mode atdma --us-mod 64qam --us-rate 32 \
    --prov-args "--ds-rate 100000000 --us-rate 20000000"
```

| Montant | Débit brut | MER brute / égalisée (Sagem 3686) | Résultat sous charge |
|---|---|---|---|
| TDMA 16-QAM 2,56 Msym/s (défaut) | 10,24 Mbit/s | ~29 / 32 dB | validé (T27) |
| ATDMA 16-QAM 5,12 Msym/s | 20,48 Mbit/s | 25,4 / — dB | en ligne, toutes rafales décodées (T30) |
| ATDMA 64-QAM 2,56 Msym/s | 15,36 Mbit/s | 33,3 / — dB | 12 575/12 579 rafales décodées |
| **ATDMA 64-QAM 5,12 Msym/s** | **30,72 Mbit/s** | 25 / 33–36 dB | **15 670/15 674 décodées, U 0 L 0 (T31)** |

**Ce que fait le CMTS en `--us-mode atdma` :** UCD de type 29 (DOCSIS 2.0, TLV 7 S-CDMA désactivé) ; les données passent
dans les IUC 9 (*advanced short*) et 10 (*advanced long*) avec préambule QPSK de 104 symboles, RS T=6/k=78 et T=8/k=224,
dernier mot de code raccourci. REQ, IM et SM restent en QPSK. Le préambule étant QPSK, la détection ne dépend pas de la
correspondance bits ↔ points des données. **La table 64-QAM supposée (Gray par axe, comme la 16-QAM) est confirmée par le
modem réel** : toutes ses rafales décodent (RS + HCS).

**Égaliseur de réception (`us_phy.equalize`)** : à 5,12 Msym/s la MER brute (~24,5 dB) n'est pas limitée par le bruit
mais par l'interférence entre symboles (filtres du modem et du B210, coax, coupleurs) : un égaliseur linéaire de 11
coefficients, ajusté par moindres carrés sur les premières décisions de chaque rafale, la remonte à 32–36 dB. Les
longues concaténations 64-QAM, qui échouaient en fin de rafale, décodent. Appliqué à toutes les rafales QAM (aussi en
16-QAM TDMA : 29 → 32,5 dB), conservé seulement s'il améliore l'erreur ; ~1 ms par rafale longue.
*Évolution possible : la pré-égalisation DOCSIS 2.0 (coefficients renvoyés au modem dans la RNG-RSP), qui corrigerait
le canal à la source pour tous les modems.*

**Corrigé au passage :**
- **Recherche au début du grant** : à 5,12 Msym/s un mini-slot fait 128 symboles ; l'intervalle de garde entre deux
  rafales (147 échantillons) est plus court qu'un bloc de mesure d'énergie (174) : la queue de la rafale précédente et la
  nouvelle forment une seule grappe, et le préambule était cherché là où commençait la queue (32 rafales perdues en
  3 minutes). Les intervalles unicast cherchent désormais aussi au début du grant.
- **Faux préambules en zone de requêtes** : la fin d'une rafale 64-QAM juste avant les créneaux REQ donnait une
  corrélation 0,35–0,48 (vraies rafales : 0,96 et plus), comptée en « rafale REQ non décodée » (~3/s). Ignorée sous 0,6.
- **Blocages TX périodiques avec `--pcap`** : toutes les ~31 s, 50 à 190 ms de downstream perdus (U/L) sous charge. Le
  pcap grossit de ~4 Mo/s à 30 Mbit/s ; le noyau en réécrit ~120 Mo d'un coup toutes les 30 s (`vm.dirty_expire_centisecs`)
  et les `write()` faits depuis le thread d'encodage descendant bloquaient derrière. `cmts/pcap.py` écrit désormais depuis
  son propre thread (file en mémoire, `fdatasync` chaque seconde) : 0 blocage avec un pcap de 960 Mo.
- Les fenêtres « nopre » (énergie sans préambule) ne sont plus sauvées quand l'énergie est hors du grant (rafale voisine,
  grant en avance inutilisé) ; les rafales KO gardent la taille de leur grant (`grant_syms`) pour l'analyse hors ligne.

**Mesurer sans se tromper :** une charge UDP envoyée *au modem* (10.10.0.10, port fermé) lui fait renvoyer un ICMP
« port unreachable » par datagramme ; au-delà de ~1000/s sa limite ICMP globale (noyau Linux) mange ses réponses ping
(jusqu'à 25 % de pertes, RTT ~230 ms). Ce n'est pas le CMTS : la même charge envoyée à l'eRouter (10.10.0.11, qui ne
répond pas) laisse le ping à 0 % de perte et 100 ms, même à 80 Mbit/s agrégés. Utile en revanche pour charger le montant
(~600–1300 petites trames/s).

**Débits du cm.cfg :** `provision.py` génère par défaut 50 Mbit/s descendant / 5 Mbit/s montant. Le montant est limité
**par le modem lui-même** : sans `--prov-args "--us-rate …"`, un test d'envoi plafonne à 5 Mbit/s quel que soit le mode.
Le descendant n'est pas limité par ce CMTS.

**Limites connues :** en mode deux canaux descendants, le bruit montant monte de −64 à −48,5 dBFS (MER brute 23,8 dB,
l'égaliseur compense) ; le débit montant réel n'a pas été mesuré faute de source de trafic montant sur le banc (à faire
avec un test d'envoi depuis le PC derrière la box). Diagnostic CPU : `MINI_CMTS_PROFILE=fichier` profile le thread de
réception montante (pstats réécrit toutes les 20 s).

## Débit maximal : 93 Mbit/s descendant, 22 Mbit/s montant (nuit du 9 au 10/10/2026)

```bash
python3 mini_cmts.py --all-in-one --us-process --grant-ahead-hold --grant-ahead-every 3 --grant-ahead-scale 4 \
    --ds-channels 2 --gain 50 --us-mode atdma --us-mod 64qam --us-rate 32 --us-target -14 \
    --prov-args "--ds-rate 200000000 --us-rate 200000000 --us-burst 64000"
```

Mesuré avec un serveur speedtest sur le PC du CMTS (sans le wifi du trajet) : **93 Mbit/s / 22 Mbit/s**, soit les
plafonds physiques (2 canaux 256-QAM ; 1 canal ATDMA 64-QAM 5,12 Msym/s ≈ 25 Mbit/s utiles). Envoi parti de 1,1 Mbit/s.

| Cause | Correction | Option / fichier |
|---|---|---|
| Max Traffic Burst du cm.cfg à 3044 o : le modem ne demande jamais plus (~33 mini-slots par demande) | rafale montante configurable | `provision.py --us-burst` |
| Grants en avance coupés par les piggyback de 0 (le Sagem en met ~100/s) | maintenus 100 ms après la dernière activité, un MAP sur N, N fois plus gros | `--grant-ahead-hold/-every/-scale` |
| Traitement montant saturé (retard jusqu'à 1 s) | étape symboles → octets et RS en C, égaliseur mémorisé, fenêtres vides sans recherche ; processus séparé, fenêtres par anneau en mémoire partagée | `cmts/usdsp.c`, `--us-process`, `cmts/usproc.py` |
| Horloge symbole du Sagem à −4,3 ppm : longues rafales illisibles en fin | écart mesuré sur la première longue rafale ratée, mémorisé et compensé par modem | `us_phy.estimate_drift` |
| Interruptions du pavé tactile sur nos cœurs (pertes TX quand le PC est utilisé) | toutes les interruptions hors USB déplacées sur le CPU 4 | `--irq-herd` (défaut) |
| MER brute 21 dB avec un bruit montant à −49 dBFS | cible de puissance relevée | `--us-target -14` |
| Diagnostic « énergie brute » : FFT de 32 768 points par rafale | calculé seulement pour les lignes journalisées | `upstream._raw_event` |

**Écartés :** raccourcir la chaîne d'émission (`--tx-queue 4 --tx-frames 64`, `--block 32`) : pertes TX et modem perdu
sous charge. Le cycle demande → grant (~60 ms, contre 5-8 ms chez SFR d'après les captures) est la limite structurelle
d'un SDR qui prépare le signal 36 ms à l'avance.

**Diagnostics ajoutés :** ligne d'état `retard max` (traitement montant), `RX écart max`, `MINI_CMTS_TIMING=1` (temps
par type de fenêtre), `kill -USR1` sur le processus montant (piles dans `/tmp/usproc_<pid>.stack`).

## Agrégation montante DOCSIS 3.0 : 2 canaux montants (nuit du 9 au 10/10/2026)

```bash
python3 mini_cmts.py --all-in-one --us-channels 2          # + --ds-channels 3 possible
```

**Résultat :** le Sagem F@st 3686 passe en mode multi-canal montant (MTC). Il range sur le second canal, répond REG-ACK
ok et reste en ligne avec 2 canaux ATDMA 64-QAM 5,12 Msym/s (60 et 53 MHz), en plus de 2 ou 3 canaux descendants. Ses
données arrivent en segments sur les deux canaux et sont réassemblées sans perte (ping 20/20 à travers ce chemin). Le
débit montant agrégé n'a pas pu être mesuré : aucun client derrière le modem cette nuit, et SNMP/HTTP sont fermés côté
câble.

**Réception :** un seul flux RX du B210 à 27,808 MS/s, centré entre les canaux (LO à 56,5 MHz, dans l'écart de 0,6 MHz
entre les deux canaux de 6,4 MHz) :
- un anneau partagé (`cmts/usproc.py`, `SampleRing`) ;
- un processus par canal : DDC en C (`usdsp.c` `ddc_decim`, `cdsp.Ddc` : décalage, FIR 63 coefficients, décimation par
  2), puis le récepteur habituel ;
- le retard du FIR (31 échantillons) est retranché de l'origine des temps : même instant d'arrivée qu'en direct
  (+0,12 comptes contre +0,13 dans `tests/test_usproc.py`) ;
- coût : ~0,2 cœur de DDC par canal, ~40 % d'un cœur par processus au repos.

**Enregistrement (REG-RSP-MP) :**
- capacité 5.24 renvoyée (le MTC n'était jusqu'ici jamais activé : 5.24 = 0) ; 5.37 = 0 (MAP et UCD sur le primaire) ;
- TCC (TLV 46) :
  - « no action » sur le canal 1 ;
  - « add » du canal 2 : SID de ranging, technique 2 (ranging unicast), paramètres de ranging relatifs au canal 1 ;
  - l'UCD du canal 2 (306 octets) découpé en fragments 46.5 sur plusieurs TLV 46, avec la référence TCC en tête de
    chaque fragment ;
- cluster de SID (TLV 47) du flux montant, à la place du SID 24.3 ;
- MDD : liste d'ambiguïté (TLV 8 = canal 1) et rapport de puissance (TLV 12) ; les RNG-RSP portent la fenêtre de
  dynamique (TLV 14).

**Données (MTC) :**
- chaque grant de données d'un flux MTC est décodé en entier comme un segment (en-tête de 8 octets avec HCS, `IUC_SEG`
  sur l'IUC transmis au récepteur) ;
- les segments sont remis dans l'ordre par numéro de séquence, quel que soit le canal (`docsis_mac.Reassembler`) ;
- la fin de segment est du bourrage 0xFF ; un trou est abandonné dès que chaque canal l'a dépassé ;
- les requêtes sont en octets (FC 0xC8, ×4) ou dans l'en-tête de segment, et accordées sur chaque canal rangé.

| Problème | Cause / correction |
|---|---|
| Processus montants à 100 % de CPU | au démarrage, chaque processus saute le retard accumulé : fenêtres lues dans des zéros, bruit mesuré à 0, recherche de préambule partout → plancher de bruit jamais pris sur des zéros |
| REG-ACK 209 (reject-bad-tcc) avec le canal 2 à 67 MHz | au-delà de 65 MHz, haut de la bande montante EuroDOCSIS → `--us-spacing -7e6` (canal 2 à 53 MHz) |
| REG-ACK 209 toujours | le TCC de fenêtre de dynamique (46.9, canal 0), en « no action » comme après le TLV 14 des RNG-RSP. Trouvé en faisant varier le TCC à chaque tentative du modem (`MINI_CMTS_TCC_VARIANTS`) : sans lui, en ligne du premier coup |
| Grants en avance sans fin | le modem remplit chaque grant d'un segment de bourrage (PFI 0, 0xFF) ; ces segments ne prolongent plus les grants |

**Observations :**
- le modem déclare 52,5 dBmV (Phi = 54 dBmV à 2 canaux). `--us-gain 30` le fait descendre à 42,75 dBmV mais coûte 2,5 dB
  de MER ;
- diaphonie de ~−27 dB entre les deux canaux (image IQ autour du LO) ;
- MER des RNG-REQ : 22,4 dB sur le canal 1 contre 29 dB sur le canal 2.

**3 canaux descendants + 2 montants :** en ligne, mais le modem laisse souvent le canal descendant 2 (le central),
parfois le 3 ; 1 essai sur 4 avec les trois.
- Le canal central est posé sur l'oscillateur local TX : fuite mesurée au HackRF à ~−31 dBc (48 dB au-dessus de la DSP
  locale).
- La calibration ajoutée (`--tx-dc-cal`, le RX du B210 accordé un instant sur le TX, constante complexe compensatrice)
  baisse cette fuite de 2 à 24 dB, sans effet sur l'acquisition du canal 2 : désactivée par défaut.

**4 canaux descendants et plus :** toujours bloqué. Le modem ne se cale qu'avec une horloge maître de 27,808 MHz, qui ne
contient que 3 canaux de 8 MHz. Voie possible : un second B210 sur la même référence 10 MHz / PPS.
