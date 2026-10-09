# mini-CMTS — un CMTS EuroDOCSIS logiciel sur un seul USRP B210

*[English version: README.md](README.md)*

Un CMTS (Cable Modem Termination System) écrit en Python, avec des noyaux en C. Il pilote un seul
**USRP B210** Ettus/NI et met **en ligne** un vrai modem câble, non modifié, dans un labo fermé :
- voie descendante EuroDOCSIS, voie montante DOCSIS 1.x/2.0, ranging ;
- DHCP, TFTP, ToD, enregistrement DOCSIS 1.1 et 3.0 ;
- agrégation de canaux descendants, pont Ethernet vers le PC Linux.

À notre connaissance, aucun CMTS libre ne faisait cela avec un SDR bon marché. Le projet sert à
apprendre DOCSIS concrètement, et à permettre à d'autres de le faire.

**État : outil de labo expérimental.** Validé avec un modem (Sagem F@st 3686, DOCSIS 3.0).

## Résultats (Sagem F@st 3686, 10/10/2026)

| | Configuration | Mesuré |
|---|---|---|
| Descendant | 2 canaux agrégés EuroDOCSIS 256-QAM (306 + 314 MHz) | **93 Mbit/s** (speedtest sur le PC du CMTS) |
| Montant | 1 canal ATDMA 64-QAM, 5,12 Msym/s (60 MHz) | **22 Mbit/s** |
| Latence | ping à travers le modem | 56-72 ms à vide, 92-139 ms en charge |

Ce sont les limites physiques des canaux utilisés : 2 × 256-QAM ≈ 100 Mbit/s ; 1 × 64-QAM ATDMA
≈ 25 Mbit/s, une fois retirés le RS et les en-têtes de rafale.

## ⚠ Sécurité et cadre légal

- **Coax fermé uniquement.** Ne jamais relier ce montage au réseau d'un opérateur : vous
  émettriez sur des fréquences sans autorisation et perturberiez d'autres abonnés.
- **Ne jamais brancher l'entrée RX du B210 sans atténuation** : un modem peut émettre jusqu'à
  +60 dBmV. Voir le câblage plus bas (un atténuateur de 8 dB et l'isolation d'un répartiteur 1:2).
- Uniquement avec vos propres modems. Ordre : lancer le programme, puis allumer le modem ; pour
  arrêter, éteindre le modem d'abord, puis Ctrl-C.

## Matériel

- USRP B210 sur un **contrôleur USB 3 dédié**, PC Linux. Testé sur Arch Linux, UHD 4.9,
  Python 3.14, portable 4 cœurs / 8 threads.
- Un répartiteur 1:2 (coupleur A), un atténuateur 8 dB, un second répartiteur pour l'observation.

```
B210 TX/RX ── coupleur A [entrée] ──┬── sortie 1 ── modem câble
                                    └── sortie 2 ── 8 dB ── coupleur B [entrée] ──┬── B210 RX2
                                                                                   └── (récepteur d'observation)
```

La voie montante du modem atteint RX2 par l'isolation entre les sorties du coupleur A.

## Démarrage rapide

```bash
# priorité temps réel pour le shell qui le lance (ou via limits.d)
sudo prlimit --pid $$ --rtprio=99:99 --nice=40:40
python3 mini_cmts.py --all-in-one          # puis allumer le modem
```

`--all-in-one` lance aussi `provision.py` sur l'interface TAP `cmts0` (10.10.0.1/24) :
- DHCP, adresses 10.10.0.10 à 99 ;
- TFTP, avec un fichier de configuration généré ;
- ToD.

Sans cette option, mettez vos propres serveurs DHCP/TFTP/ToD sur `cmts0`, comme dans une
installation DOCSIS classique.

Journal attendu, en une minute environ : `RANGING TERMINÉ`, DHCP, TFTP, puis
`REG-ACK ok -> MODEM EN LIGNE`.

Valeurs par défaut (depuis le 10/10/2026), celles des résultats ci-dessus :

| Option | Défaut | Rôle |
|---|---|---|
| `--freq` | 306 MHz | premier canal descendant ; les suivants tous les `--ds-spacing` (8 MHz) |
| `--ds-channels` | 2 | 1 à 6 (horloge 27,808 / 41,712 / 55,616 MHz ; 3 et plus expérimental) |
| `--qam` | 256 | modulation descendante |
| `--us-freq`, `--us-rate`, `--us-mode`, `--us-mod` | 60 MHz, 32 (5,12 Msym/s), atdma, 64qam | canal montant |
| `--us-target` | −14 dBFS | niveau visé pour les rafales des modems |
| `--grant-ahead-hold/-every/-scale` | oui, 3, 4 | grants non sollicités qui entretiennent l'envoi |
| `--us-process` | oui | démodulation montante dans un processus séparé |
| `--prov-args` | — | par ex. `"--us-burst 64000 --us-rate 200000000"` (déjà les défauts) |

`python3 mini_cmts.py --help` donne tout ; `--sim SECONDES` simule un modem sans matériel.

## Fonctionnement

- **Voie descendante** (`cmts/downstream.py`, `cmts/j83a.py`, C : `cmts/dsdsp.c`) :
  - multiplexeur MPEG-TS qui porte SYNC, UCD, MAP, MDD et données ;
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
  - trames MAC et concaténations.
- **MAC** (`cmts/ranging.py`, `cmts/registration.py`, `cmts/docsis_mac.py`) :
  - ranging (temps, puissance, fréquence) ;
  - demandes de bande passante et grants, y compris les grants en avance ;
  - REG-REQ(-MP) / REG-RSP(-MP) / REG-ACK avec CMTS MIC ;
  - RCC et DSID pour l'agrégation, MDD pour DOCSIS 3.0.
- **Pont** (`cmts/bridge.py`) : trames Ethernet entre les modems et l'interface TAP.
- **Provisionnement** (`provision.py`, `cmts/cmcfg.py`) : DHCP, TFTP, ToD, générateur de fichier
  de configuration.

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

Détails, mesures et tous les bogues rencontrés : `docs/JOURNAL.fr.md` et `docs/TESTS.fr.md`.

## Tests

```bash
for t in tests/test_*.py; do python3 $t | tail -1; done   # test_provision_tap.py demande sudo
```

La plupart des tests rejouent de vraies rafales du modem (`tests/data/sagem3686/`).

## Feuille de route

- 4 canaux descendants en temps réel : la synthèse en C est faite, le processeur du portable de
  test est la limite. Puis 5 à 6.
- Agrégation montante DOCSIS 3.0 (plusieurs canaux montants).
- Répartition du CMTS sur plusieurs SDR et PC.

## Licence

À choisir par le propriétaire du projet avant publication.

Normes de référence (non incluses) : CableLabs DOCSIS MULPIv3.0 et PHYv3.0, UIT-T J.83,
ETSI EN 300 429.
