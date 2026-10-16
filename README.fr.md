# mini-CMTS — un CMTS EuroDOCSIS logiciel sur USRP B210 (ou bladeRF)

*[English version: README.md](README.md)*

Un CMTS (Cable Modem Termination System) écrit en Python, avec des noyaux en C. Il pilote un ou plusieurs
**USRP B210** Ettus/NI (ou un **bladeRF 2.0 micro** de Nuand) et met **en ligne** un vrai modem câble, non modifié, dans un labo fermé :
- voie descendante EuroDOCSIS, voie montante DOCSIS 1.x/2.0, ranging ;
- DHCP, TFTP, ToD, enregistrement DOCSIS 1.1 et 3.0 ;
- agrégation de canaux descendants et montants (DOCSIS 3.0), pont Ethernet vers le PC Linux ;
- paquet Debian, service systemd, page d'état dans le navigateur.

À notre connaissance, aucun CMTS libre ne faisait cela avec un SDR bon marché. Le projet sert à
apprendre DOCSIS concrètement, et à permettre à d'autres de le faire.

**État : outil de labo expérimental.** Validé avec un modem (Sagem F@st 3686, DOCSIS 3.0).

## Résultats (modem Sagem F@st 3686 ; un B210 : hôte i5-9500 6 cœurs, 12/10/2026)

| | Configuration | Mesuré (iperf3 à travers le modem) |
|---|---|---|
| Descendant | 6 canaux agrégés EuroDOCSIS 256-QAM (290-330 MHz, 306 principal au milieu) | **276-284 Mbit/s** (8-16 flux TCP ; ~235 avec 4 flux, 120-175 avec 1 flux, limités par la latence) |
| Montant | 2 canaux agrégés ATDMA 64-QAM 5,12 Msym/s (60 + 53 MHz), `--us-channels 2 --us-target -20` | **51-52 Mbit/s** (8 flux, essai de 45 min) ; 22-24 avec un seul canal |
| Latence | ping à travers le modem | ~75 ms au repos, ~55 ms pendant un téléchargement (CoDel) |
| Deux B210 | 2 + 2 DS, 2 US, sans 10 MHz commun (Threadripper 2950X, `configs/2xb210.toml`) | **188-189 / 49,5 Mbit/s**, essai de 86 min avec la suiveuse recalée (12,4 ms au total) |
| bladeRF 2.0 micro | 3 DS + 1 US (`configs/bladerf.toml`) | **138 / 24,5 Mbit/s**, voir `docs/BLADERF.fr.md` |

Proches des limites physiques des canaux utilisés : 6 × 256-QAM ≈ 300 Mbit/s ; 2 × 64-QAM ATDMA
≈ 50 Mbit/s, une fois retirés le RS et les en-têtes de rafale.

Étapes précédentes (1 à 5 canaux, processus unique, agrégation montante avant qu'elle n'apporte
de débit) : [docs/HISTORY.fr.md](docs/HISTORY.fr.md).

## ⚠ Sécurité et cadre légal

- **Coax fermé uniquement.** Ne jamais relier ce montage au réseau d'un opérateur : vous
  émettriez sur des fréquences sans autorisation et perturberiez d'autres abonnés.
- **Ne jamais brancher l'entrée RX du B210 sans atténuation** : un modem peut émettre jusqu'à
  +60 dBmV. Voir le câblage plus bas (un atténuateur de 8 dB et l'isolation d'un répartiteur 1:2).
- Uniquement avec vos propres modems. Ordre : lancer le programme, puis allumer le modem ; pour
  arrêter, éteindre le modem d'abord, puis Ctrl-C.

## Matériel

- USRP B210 sur un **contrôleur USB 3 dédié**, PC Linux. Testé : Arch Linux (UHD 4.9,
  Python 3.14, portable 4 cœurs), Debian 13 (UHD 4.8, Python 3.13, i5-9500 6 cœurs), Debian 12
  (UHD 4.3, Python 3.11, Threadripper 2950X 16 cœurs avec deux B210).
- Ou un **bladeRF 2.0 micro** (`--sdr bladerf`, 3 DS + 1 US) : voir `docs/BLADERF.fr.md`.
- Un répartiteur 1:2 (coupleur A), un atténuateur 8 dB, un second répartiteur pour l'observation.

```
B210 TX/RX ── coupleur A [entrée] ──┬── sortie 1 ── modem câble
                                    └── sortie 2 ── 8 dB ── coupleur B [entrée] ──┬── B210 RX2
                                                                                   └── (récepteur d'observation)
```

La voie montante du modem atteint RX2 par l'isolation entre les sorties du coupleur A.

Deux B210 : leurs TX/RX sur un coupleur 1:2, leurs RX2 sur un autre (derrière l'atténuateur de
8 dB), et ces deux coupleurs et le modem sur les **sorties** d'un coupleur 1:3.

## Installation et démarrage

Guide complet (hôte, USB, priorité temps réel, radios, service) : [docs/INSTALL.fr.md](docs/INSTALL.fr.md).

```bash
# Debian 12+ / Ubuntu 24.04+ : le paquet (dépendances installées par apt, noyaux C compilés pour ce CPU)
./packaging/build-deb.sh && sudo apt install ./mini-cmts_*_all.deb
sudo mini-cmts --config /etc/mini-cmts/mini-cmts.toml          # puis allumer le modem

# ou depuis les sources
sudo prlimit --pid $$ --rtprio=99:99 --nice=40:40               # priorité temps réel pour ce shell
python3 mini_cmts.py --all-in-one --config configs/b210.toml
```

`--all-in-one` lance aussi `provision.py` sur l'interface TAP `cmts0` (10.10.0.1/24) :
- DHCP, adresses 10.10.0.10 à 99 ;
- TFTP, avec un fichier de configuration généré ;
- ToD.

Sans cette option, mettez vos propres serveurs DHCP/TFTP/ToD sur `cmts0`, comme dans une
installation DOCSIS classique.

Journal attendu, en une minute environ : `RANGING TERMINÉ`, DHCP, TFTP, puis
`REG-ACK ok -> MODEM EN LIGNE`.

- **Fichiers de banc** (`--config`, TOML) : les clés sont les options ci-dessous (`--ds-channels`
  -> `ds_channels`), `[provisioning]` porte celles de `provision.py`, la ligne de commande
  l'emporte sur le fichier, une clé mal écrite est refusée. Exemples dans `configs/` :
  `b210.toml` (6 DS + 2 US), `2xb210.toml`, `bladerf.toml`.
- **Page d'état** : <http://127.0.0.1:8090/> pendant qu'il tourne : débit sur 30 min, radios
  (underflows, paquets en retard, marge d'émission), canaux descendants (remplissage), canaux
  montants (bruit, retard du récepteur), modems (état, temps, puissance, MER), alertes récentes.
  `--status-http 0.0.0.0:8090` pour le réseau du labo, `none` pour la couper.
  - **Capacités** de chaque modem, telles qu'il les annonce : version DOCSIS, récepteurs
    descendants et émetteurs montants, BPI+, IPv6, débits symbole, profils de réception
    (REG-REQ-MP) ; fabricant, modèle, numéro de série, version logicielle, composants intégrés
    (eCM, eMTA/eDVA, eRouter) d'après son DHCP ; les équipements derrière lui (eRouter, eMTA, PC).
  - **Configuration** : les options courantes (radio, nombre de canaux, fréquences, modulations,
    niveaux) dans un formulaire, vérifiées (bandes EuroDOCSIS, canaux par radio, chevauchements),
    écrites dans le fichier de banc `--config` (commentaires gardés, `.bak`), puis le CMTS
    redémarre dessus. Depuis la machine elle-même seulement (ou `ssh -L 8090:127.0.0.1:8090 hôte`) ;
    `--status-edit on` depuis partout où la page est joignable, `off` en lecture seule.
- **Service** : `sudo systemctl enable --now mini-cmts` lance le banc de
  `/etc/mini-cmts/mini-cmts.toml` ; `journalctl -u mini-cmts -f` montre sa console.

Valeurs par défaut (depuis le 10/10/2026), celles des résultats ci-dessus :

| Option | Défaut | Rôle |
|---|---|---|
| `--freq` | 306 MHz | premier canal descendant ; les suivants tous les `--ds-spacing` (8 MHz) |
| `--ds-channels` | 6 | 1 à 6 (horloge maître 27,808 / 34,76 / 55,616 MHz) ; 6 : 234-258 / 21 Mbit/s sur le PC 6 cœurs |
| `--qam` | 256 | modulation descendante |
| `--us-freq`, `--us-rate`, `--us-mode`, `--us-mod` | 60 MHz, 32 (5,12 Msym/s), atdma, 64qam | canal montant |
| `--us-target` | −14 dBFS | niveau visé pour les rafales des modems |
| `--grant-ahead-hold/-every/-scale` | oui, 3, 4 | grants non sollicités qui entretiennent l'envoi |
| `--us-process` | oui | démodulation montante dans un processus séparé |
| `--radio-process` | oui | B210, encodage J.83, mise en forme et émission dans un processus séparé (`cmts/radio.py`) : sans lui, 3 canaux descendants décrochent sur un PC plus lent (GIL partagé avec la couche MAC) |
| `--us-channels`, `--us-spacing` | 1, −7 MHz | canaux montants (2 : agrégation montante DOCSIS 3.0, le second 7 MHz plus bas) |
| `--tx-queue` | 3 | blocs prêts avant l'envoi (~1,9 ms chacun). Avec `--tx-frames 48` : latence descendante ~25 ms au lieu de 36, ping au repos 100 → 78 ms, marge d'émission minimale ~7 ms (essai long à faire) |
| `--send-frame-size`, `--tx-frames` | 16360, 48 (réserve 14 ms) | trames USB d'envoi de 16 Ko (UHD : 8 Ko) et nombre en vol : moitié moins de soumissions USB, fil d'envoi 23 → 16 % d'un cœur à 55,6 MS/s, 6 DS sans retard d'émission |
| `--ds-layout` | around | `--freq` (le canal principal) au milieu, les autres de part et d'autre (`up` : tous au-dessus) ; en 6 DS, la seule disposition où le Sagem utilise ses grants montants (234-247 / 21 Mbit/s) |
| `--recv-frame-size` | 16360 | trames USB de réception de 16 Ko : fil de réception de la radio 24 → 14 % d'un cœur à 27,8 MS/s |
| `--us-cpus` | auto | `own` : un cœur par processus montant ; `shared` : tous les cœurs, priorité temps réel 10 en dessous de l'émission ; `auto` : `shared` seulement si des cœurs réservés laisseraient moins de 4 cœurs à l'émission de 5-6 DS (6 DS + 2 US) |
| `--us-lag-limit` | 150 ms | contre-réaction : un canal montant dont le récepteur a ce retard ne reçoit plus de grant de données (la moitié : plus de grant en avance) jusqu'à ce qu'il ait rattrapé ; 0 = sans |
| `--mtc-std` | oui | enregistrement multi-canal montant conforme au MULPI, comme un vrai CMTS (ARRIS) : fenêtre de dynamique dans la TCC (action 2, obligatoire en mode multi-canal) et non dans les RNG-RSP, critères 47.3 à 0, TLV 52 = 60 ; `--no-mtc-std` : l'ancien enregistrement, sans fenêtre de dynamique |
| `--mtc-owed` | oui | requêtes multi-canal cumulées (le modem ne demande que les octets nouveaux), accordées sur chaque canal à chaque MAP, grant pending tant qu'il reste dû ; `--no-mtc-owed` : chaque requête remplace la précédente |
| `--ds-aqm` | 30 ms | CoDel sur les files descendantes : quelques trames jetées une à une au lieu de rafales à la limite `--ds-queue-ms` ; ping pendant un téléchargement 88 → 55 ms ; 0 = sans |
| `--tx-dc-cal` | non | mesure et compense au démarrage la fuite de l'oscillateur local TX du B210 (−2 à −24 dB ; n'a pas aidé le 3e canal) |
| `--sdr`, `--sdr-args` | b210, — | `bladerf` : un **bladeRF 2.0 micro** à la place du B210 (libbladeRF appelée directement, `cmts/sdrdev.py`), TX1 vers le modem, RX1 pour le montant. Validé en **3 DS + 1 US** (138 / 24,5 Mbit/s) : émission et réception au même débit (27,8 MS/s en 16 bits au plus), réception à partir de 70 MHz (un seul canal montant, à 61,8 MHz). `--sdr-args "*:serial=…"` choisit la carte. Choix, pièges et limites : `docs/BLADERF.fr.md` ; vérification : `tools/bladerf_check.py --loop` |
| `--ds-radios`, `--radio-serials`, `--clock-source` | 1, —, internal | plusieurs radios (branche `multi-b210`) : les canaux descendants répartis en blocs adjacents, une radio par bloc ; la première reçoit aussi le montant. Sans 10 MHz commun (`internal`), la suiveuse se recale sur la principale par groupes de 8 paquets MPEG (dérive mesurée entre deux B210 : 2,5 ppm, 8,9 ms/h) ; 2 B210 en 2 + 2 DS : 189 / 50 Mbit/s |
| `--config` | — | un fichier de banc en TOML (ci-dessus) ; `@fichier.args` (une option par ligne) marche toujours |
| `--status-http` | 127.0.0.1:8090 | la page d'état ; `none` : coupée |
| `--status-edit` | auto | le formulaire de configuration : modifications depuis cette machine seulement ; `on` depuis partout, `off` lecture seule |
| `--prov-args` | — | par ex. `"--us-burst 64000 --us-rate 1000000000"` (déjà les défauts ; 1 Gbit/s dans les deux sens) ; `--us-buffer` (défaut 128 Ko) fixe le tampon montant du modem (TLV 24.35) : ping pendant un envoi 237 → 93 ms |

`python3 mini_cmts.py --help` donne tout ; `--sim SECONDES` simule un modem sans matériel.

## Fonctionnement

- **Voie descendante** (`cmts/downstream.py`, `cmts/j83a.py`, C : `cmts/dsdsp.c`) :
  - multiplexeur MPEG-TS qui porte SYNC, UCD, MAP, MDD et données, files de données gérées par
    CoDel ;
  - codeur J.83 annexe A (RS, entrelaceur, randomiseur, 256-QAM), mise en forme RRC ;
  - canaux agrégés décalés et sommés en une passe C, envoyés au B210 à 4, 6 ou 8 échantillons
    par symbole ;
  - chaque bloc est horodaté : la voie descendante sert de base de temps.
- **Voie montante** (`cmts/upstream.py`, `cmts/us_phy.py`, C : `cmts/usdsp.c`, processus :
  `cmts/usproc.py`) : le flux reçu est découpé selon les intervalles des MAP. Pour chaque rafale :
  - recherche du préambule, interpolation des symboles ;
  - porteuse et gain, égaliseur mémorisé par modem ;
  - compensation de l'écart d'horloge symbole, mesuré par modem ;
  - suivi décisionnel, désembrouillage, Reed-Solomon ;
  - trames MAC et concaténations ;
  - une rafale de segment d'un modem dont l'égaliseur est connu (la grande majorité du trafic
    multi-canal) est décodée d'un seul appel C, égaliseur recalculé en C si besoin (`seg_fast`) ;
    le reste passe par le chemin Python (`MINI_CMTS_US_FAST=0` : tout en Python).
- **MAC** (`cmts/ranging.py`, `cmts/registration.py`, `cmts/docsis_mac.py`) :
  - ranging (temps, puissance, fréquence) ;
  - demandes de bande passante et grants, y compris les grants en avance ;
  - REG-REQ(-MP) / REG-RSP(-MP) / REG-ACK avec CMTS MIC ;
  - RCC et DSID pour l'agrégation, MDD pour DOCSIS 3.0.
- **Agrégation montante** (`--us-channels 2`) :
  - le B210 reçoit un seul flux à 27,808 MS/s centré entre les canaux montants ;
  - un processus par canal extrait le sien (DDC en C : décalage, filtre, décimation) et le
    démodule ;
  - un modem DOCSIS 3.0 reçoit dans son REG-RSP-MP le mode multi-canal (MTC), une configuration
    des canaux d'émission (TCC, TLV 46 : garder le canal 1, ajouter le canal 2 avec son UCD, la
    fenêtre de dynamique), un cluster de SID (TLV 47, critères 47.3) et le TLV 52, comme le fait un
    vrai CMTS (relevé sur un CMTS ARRIS en réception seule, `tools/origin_maps.py`) ;
  - il envoie alors ses données en segments (en-tête de 8 octets, numéro de séquence) sur les
    deux canaux ; le CMTS les remet dans l'ordre et les recoupe en trames MAC (CCF) ;
  - les requêtes sont en octets (requêtes « queue-depth ») et ne portent que les octets nouveaux :
    le CMTS les cumule, les accorde sur chaque canal rangé et signale par un grant pending ce qu'il
    doit encore.
- **Plusieurs radios** (`--ds-radios 2`, branche `multi-b210`, `cmts/radio.py`) :
  - chaque radio émet un bloc de canaux adjacents dans son propre processus ; la première reçoit aussi le montant ;
  - la MAC numérote les blocs ; une suiveuse règle son horloge sur celle de la principale et horodate son bloc k
    comme la principale ;
  - sans 10 MHz commun, les oscillateurs dérivent (2,5 ppm mesurés entre deux B210, 8,9 ms/h, `tools/clock_drift.py`) :
    chaque radio ajuste une droite de son horloge contre celle de l'hôte, et la suiveuse insère ou retire 8 paquets
    MPEG nuls (un cycle d'embrouillage J.83-A, ~235 µs) quand elle s'écarte de la principale ;
  - 2 B210 en 2 + 2 DS : 186-189 / 50 Mbit/s pendant 86 min, avec 12,4 ms de correction cumulée.
- **Pont** (`cmts/bridge.py`) : trames Ethernet entre les modems et l'interface TAP. Les trames de
  Linux sont lues par lots par une fonction C (toutes celles en attente, en un appel) et changées en
  PDU descendantes par le fil qui construit les blocs TS, un budget d'octets par bloc : une fenêtre
  TCP arrivée d'un coup ne peut plus priver les radios du GIL (elle bloquait les deux B210 au début
  de chaque téléchargement).
- **Provisionnement** (`provision.py`, `cmts/cmcfg.py`) : DHCP, TFTP, ToD, générateur de fichier
  de configuration.

## Outils d'analyse du réseau d'origine des modems (réception seule)

Pour voir comment un vrai CMTS pilote un modem (c'est ce qui a débloqué l'agrégation montante, voir
plus bas), la voie descendante du réseau d'où vient le modem peut être enregistrée et décodée, **en
réception seule** : le B210 ou un HackRF sur un coupleur, sa sortie TX débranchée, le CMTS du labo
arrêté.

| Outil | Rôle |
|---|---|
| `tools/origin_rx.sh` | enregistre la voie descendante avec `uhd_rx_cfile` (aucun flux d'émission) |
| `origin_listen.py` | décode l'enregistrement en parallèle : SYNC, UCD, MAP, MDD et les messages destinés à la MAC d'un modem, dans un pcap |
| `origin_raw.py` | les octets décodés bruts, pour comparer plusieurs copies d'un message |
| `tools/origin_maps.py` | profils de rafale montants (UCD) et usage des MAP par canal, à partir de ce pcap |
| `tools/hackrf_us.sh`, `tools/hackrf_reboot_cap.sh` | la même chose avec un HackRF (bande montante, redémarrage d'un modem) |

Seuls les messages de gestion sont gardés. Un enregistrement brut contient aussi le trafic d'autres
abonnés : ne pas le conserver ni le partager. Ne jamais émettre sur ce réseau.

## Ce qui a été difficile

- **Le temps réel sur un PC** :
  - priorité temps réel ;
  - garder libre le cœur qui traite les interruptions USB du B210 ;
  - éloigner les autres interruptions : celles du pavé tactile suffisaient à casser la voie
    descendante ;
  - ne jamais écrire de fichier depuis le fil d'émission.
- **La rafale maximale** du fichier de configuration (TLV 24.9) plafonne chaque demande du modem.
  La valeur par défaut, 3044 octets, limitait l'envoi à ~3,5 Mbit/s.
- **Le Sagem ajoute une demande piggyback nulle** à presque chaque trame : les grants en avance ne
  doivent pas s'arrêter dessus.
- **L'horloge symbole montante du modem avait 4,3 ppm d'écart** : sans effet sur les rafales
  courtes, fatal sur celles de 3 ms.
- **En TDMA 16-QAM, le préambule est lui aussi en 16-QAM** : il a fallu retrouver la
  correspondance étiquettes/points par recherche exhaustive.
- **Agrégation montante avec le Sagem** : un canal montant au-delà de 65 MHz (haut de la bande
  montante EuroDOCSIS) vaut un REG-ACK 209 (reject-bad-tcc), sans autre détail. La TCC de fenêtre
  de dynamique, obligatoire en mode multi-canal, valait aussi un 209 tant qu'elle était envoyée en
  « no action » après le TLV 14 des RNG-RSP. Elle est acceptée sous la forme d'un vrai CMTS : action
  « change », UCID 0, et rien dans les RNG-RSP. Il a fallu l'écouter, en réception seule, pour le
  voir. Le modem remplit chaque grant d'un segment de bourrage : ces segments ne doivent pas
  relancer les grants en avance.
- **Le deuxième canal montant n'apportait rien** (22-24 Mbit/s avec 1 ou 2 canaux) :
  - chaque requête multi-canal ne porte que les octets nouveaux, et le CMTS les remplaçait au lieu
    de les cumuler ;
  - les récepteurs montants, à moitié en Python, saturaient le processeur.

  Requêtes cumulées avec grant pending, puis rafales de segment décodées en C : 44-47 Mbit/s.
- **UHD calibre la quadrature TX de l'AD9361 à l'ouverture des flux**, avec un ton de test
  rebouclé par le filtre analogique RX ; ce ton monte avec l'horloge maître. Avec le filtre RX à
  la largeur montante (12,4 MHz), la calibration ratait dès 41,712 MHz : l'image TX (miroir du
  signal autour de l'oscillateur local) restait à −21 à −24 dBc au lieu de −51, et le modem ne se
  calait jamais au-delà de 34,76 MHz. Les flux s'ouvrent maintenant avec un filtre RX de 56 MHz,
  resserré ensuite (`radio.TX_CAL_RX_BW`) : le modem se cale à 41,712 et 55,616 MHz. 5 canaux à
  55,616 MS/s dépassent alors le processeur de l'hôte Proxmox (6 cœurs).
- **À 3 canaux, celui du milieu tombe sur l'oscillateur local TX** : sa fuite y était à ~−31 dBc.

Détails, mesures et tous les bogues rencontrés : `docs/JOURNAL.fr.md` et `docs/TESTS.fr.md`.

## Tests

```bash
python3 tests/run.py            # tous les tests sans radio ni root (aussi dans GitHub Actions)
python3 tests/run.py --sudo     # plus test_provision_tap.py (crée cmts0, lance provision.py)
```

La plupart des tests rejouent de vraies rafales du modem (`tests/data/sagem3686/`) ; GNU Radio
(gr-dtv) et tshark vérifient la voie descendante de façon indépendante. Sur le banc :
`docs/TESTS.fr.md`, `tools/burst_test.sh` (téléchargements après un temps calme),
`tools/arg_effects.py` (un second B210 vérifie ce que chaque option change à l'antenne).

## Feuille de route

- Un 10 MHz / PPS commun pour plusieurs B210 (`--clock-source external`) : plus rien à recaler ;
  puis 3 + 3 DS et plus de radios sur un hôte avec assez de bande passante mémoire.
- bladeRF : échantillons 8 bits (libbladeRF 2.5+) pour 2 US et 4 à 6 DS ; un pilote par radio,
  pour mélanger B210 et bladeRF.
- Plusieurs modems à la fois (testé avec un seul), et un second modèle de modem.
- Montant : moins de processeur par canal (suivi de phase par blocs, Reed-Solomon en SIMD), plus
  de 2 canaux en production.

## Licence

**AGPL-3.0-only, ou licence commerciale** (double licence) : voir `COMMERCIAL.md`. Libre
d'utiliser, de modifier et de partager ; une version modifiée distribuée ou utilisée à travers un
réseau doit être accompagnée de son code source. L'intégrer dans un produit fermé, ou garder ses
modifications privées, demande une licence commerciale du titulaire des droits. Contributions :
`CONTRIBUTING.md`. Copyright © 2026 BILLY Rémi.

Normes de référence (non incluses) : CableLabs DOCSIS MULPIv3.0 et PHYv3.0, UIT-T J.83,
ETSI EN 300 429.
