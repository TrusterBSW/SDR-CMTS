
<h1 align="center">sdr-cmts</h1>

<p align="center">
  <b>Votre propre réseau câblé, sur votre propre coax.</b><br>
  Un CMTS EuroDOCSIS 3.0 sur radio logicielle : de vrais modems câble en ligne, agrégés, à pleine vitesse.
</p>

<p align="center">
  <a href="README.md">English version</a> ·
  <a href="#installation">Installation</a> ·
  <a href="#démarrage-rapide">Démarrage rapide</a> ·
  <a href="#documentation">Documentation</a>
</p>

<p align="center">
  <img alt="licence" src="https://img.shields.io/badge/licence-AGPL--3.0--only%20%7C%20commerciale-blue">
  <img alt="plateformes" src="https://img.shields.io/badge/plateformes-Debian%2012%20%7C%2013%20%7C%20Ubuntu%2024.04-informational">
  <img alt="norme" src="https://img.shields.io/badge/EuroDOCSIS-3.0-success">
  <img alt="radios" src="https://img.shields.io/badge/radios-USRP%20B210%20%7C%20bladeRF%202.0-orange">
</p>

---

sdr-cmts est un CMTS (Cable Modem Termination System), la tête de réseau d'un réseau câblé, écrit en
Python avec des noyaux en C. Il pilote un ou plusieurs **USRP B210** d'Ettus (ou un **bladeRF 2.0
micro** de Nuand) et met en ligne de vrais modems câble, non modifiés, sur un coax fermé : le modem
fait son ranging, s'enregistre, reçoit son adresse et son fichier de configuration, et le PC derrière
lui navigue sur Internet à travers lui. À notre connaissance, c'est le premier CMTS libre sur une
radio logicielle bon marché.

- **EuroDOCSIS 3.0 complet** : jusqu'à 6 canaux descendants 256-QAM agrégés par B210, voie
  montante ATDMA jusqu'en 64-QAM à 5,12 Msym/s avec l'agrégation montante DOCSIS 3.0 (mode
  multi-canal MTC), ranging, enregistrement (DOCSIS 1.1 à 3.0), CoDel sur les files descendantes.
- **À la vitesse des canaux** : 283-284 Mbit/s en descendant et 48-52 en montant à travers un Sagem
  F@st 3686 avec un seul B210, ping ~75 ms.
- **Plusieurs radios** : deux B210 se partagent les canaux descendants, la suiveuse recalée sur la
  principale sans 10 MHz commun ; ou un bladeRF 2.0 micro (3 DS + 1 US).
- **Tout ce qu'il faut au modem** : DHCP, TFTP avec un fichier de configuration généré, heure, et un
  pont Ethernet (TAP `cmts0`) vers les équipements derrière les modems.
- **Une page d'état** : canaux, radios, récepteurs montants, modems et ce qu'ils annoncent (version
  DOCSIS, récepteurs, émetteurs, fabricant, modèle, eMTA et eRouter intégrés), débit, alertes, et un
  formulaire pour les options courantes.
- **Prêt à l'emploi** : un paquet Debian, un service systemd, les réglages de l'hôte remis après
  chaque arrêt, une radio simulée pour l'essayer, ou dimensionner un PC, sans aucun matériel.

> [!WARNING]
> **Coax fermé uniquement.** Un CMTS émet : relié au réseau câblé d'un opérateur, il émettrait sur
> des fréquences sans autorisation et perturberait d'autres abonnés. sdr-cmts est fait pour un coax
> privé entre la radio et vos propres modems, avec des atténuateurs.

**État : outil de labo expérimental.** Validé avec un seul modem : un Sagem F@st 3686 (DOCSIS 3.0)
sous firmware de l'opérateur SFR (FAST3686_SFR-PC20_3.649.0) ; toutes les mesures ci-dessous ont été
faites avec lui. **EuroDOCSIS uniquement** (canaux de 8 MHz, UIT-T J.83 annexe A) : le DOCSIS
nord-américain (annexe B, canaux de 6 MHz) n'est pas implémenté.

## Sommaire

- [Installation](#installation)
- [Démarrage rapide](#démarrage-rapide)
- [Fonctionnement](#fonctionnement)
- [Configuration](#configuration)
- [Matériel et câblage](#matériel-et-câblage)
- [Page web](#page-web)
- [Service et réglages de l'hôte](#service-et-réglages-de-lhôte)
- [Normes et radios](#normes-et-radios)
- [Mesures](#mesures)
- [Ce qui a été difficile](#ce-qui-a-été-difficile)
- [Outils pour le réseau d'origine du modem](#outils-pour-le-réseau-dorigine-du-modem)
- [Documentation](#documentation)
- [Compiler depuis les sources](#compiler-depuis-les-sources)
- [Feuille de route](#feuille-de-route)
- [Licence](#licence)

## Installation

Un seul paquet pour Debian 12 et 13, Proxmox VE 8 et 9, et Ubuntu 24.04 ou plus récent. En une ligne :

```bash
curl -fsSL https://raw.githubusercontent.com/TrusterBSW/SDR-CMTS/main/install.sh | sudo sh
# --uhd-images : aussi les images FPGA de l'USRP B210
curl -fsSL https://raw.githubusercontent.com/TrusterBSW/SDR-CMTS/main/install.sh | sudo sh -s -- --uhd-images
```

Ou à la main :

```bash
wget https://github.com/TrusterBSW/SDR-CMTS/raw/main/releases/sdr-cmts_0.1.0_all.deb
sudo apt install ./sdr-cmts_0.1.0_all.deb
sudo uhd_images_downloader -t b2xx            # les images FPGA du B210, une fois
```

Le paquet est `Architecture: all` : des sources Python et C, les noyaux C compilés à l'installation
pour le processeur de la machine (AVX2 là où il existe). apt installe Python, NumPy, SciPy, gcc et
UHD ; libbladeRF est suggérée. `packaging/build-all-debs.sh` le construit dans un conteneur propre,
puis l'installe et le vérifie dans un conteneur neuf de chaque distribution ; GitHub Actions fait de
même à chaque étiquette et le publie sur la page [Releases](https://github.com/TrusterBSW/SDR-CMTS/releases).

L'hôte, l'USB, la priorité temps réel et les radios en détail : [`docs/INSTALL.fr.md`](docs/INSTALL.fr.md).

## Démarrage rapide

```bash
uhd_find_devices                                    # 1. le B210, en USB 3
sudoedit /etc/sdr-cmts/sdr-cmts.toml                # 2. le banc : radio, canaux, provisionnement
sudo sdr-cmts --config /etc/sdr-cmts/sdr-cmts.toml  # 3. à l'antenne ; puis allumer le modem
xdg-open http://127.0.0.1:8090/                     # 4. la page d'état
sudo systemctl enable --now sdr-cmts                # 5. en service, maintenant et à chaque démarrage
```

En une minute environ, le journal affiche `RANGING TERMINÉ`, DHCP, TFTP, puis `REG-ACK ok -> MODEM EN
LIGNE`, et le PC derrière le modem reçoit une adresse. Pour essayer sans aucune radio :

```bash
SDR_CMTS_SDR_SIM=1 sdr-cmts --tap none --ds-channels 4     # toute la chaîne sur une radio simulée
```

`sdr-cmts --help` liste les options, `man sdr-cmts` dit tout.

## Fonctionnement

```
 le coax                  processus radio (un par radio)             processus MAC (sdr_cmts.py)
 ───────                  ──────────────────────────────             ──────────────────────────────────
                          ┌ encodage J.83 A, mise en forme RRC,  ┌ SYNC, UCD, MAP, MDD, PDU de données
 descendant ◄── B210 TX ◄─┤ canaux décalés et sommés (C) ◄───────┤ blocs TS, files avec CoDel
                          └ envoi horodaté (UHD / libbladeRF)    │ ranging, enregistrement, requêtes,
                                                                 │ grants, agrégation (RCC, DSID, TCC)
 montant ────► B210 RX ──► anneau d'échantillons (mémoire partagée)
                             └► un processus par canal ─────────►└ pont : TAP cmts0 ◄──► Linux
                                montant : DDC, démodulation (C)     provision.py : DHCP / TFTP / ToD
                                                                    page d'état :8090
```

- **La voie descendante est la base de temps** : chaque bloc de paquets TS porte l'heure de
  l'appareil à laquelle il passe à l'antenne, si bien que chaque horodatage DOCSIS correspond à un
  échantillon montant exact. Le récepteur sait où tombe chaque intervalle des MAP et mesure l'erreur
  de temps de chaque modem à quelques nanosecondes.
- **Le lourd est en C** (encodage J.83, synthèse des canaux agrégés en AVX2, DDC, démodulation des
  rafales, Reed-Solomon, découpage TS, lecture du TAP), dans des processus à part : le Python de la
  MAC ne retient jamais les radios.
- **L'agrégation montante comme un vrai CMTS** : mode multi-canal MTC, configuration des canaux
  d'émission avec sa fenêtre de dynamique, clusters de SID, requêtes « queue-depth » cumulées,
  segments réassemblés en trames MAC.
- **Plusieurs radios sans horloge commune** : chaque radio ajuste une droite de son horloge contre
  celle de l'hôte, et une suiveuse insère ou retire des groupes de 8 paquets MPEG nuls (un cycle
  d'embrouillage J.83-A, ~235 µs) quand elle s'écarte de la principale (2,5 ppm mesurés entre deux B210).

Chaque étape, mesure et bogue : [`docs/JOURNAL.fr.md`](docs/JOURNAL.fr.md).

## Configuration

Un fichier TOML, `/etc/sdr-cmts/sdr-cmts.toml`. Les clés sont les options longues (`--ds-channels`
devient `ds_channels`), les sections ne font que les regrouper, sauf `[provisioning]`, dont les clés
sont celles du serveur de provisionnement. La ligne de commande l'emporte sur le fichier ; une clé mal
écrite est refusée, avec les noms les plus proches.

```toml
[radio]
sdr = "b210"                # ou "bladerf"
args = "type=b200"          # arguments UHD (",serial=..." pour en choisir un)

[downstream]
freq = 306e6                # canal principal (grille européenne 114 + 8n MHz)
ds_channels = 6             # canaux de 8 MHz agrégés, jusqu'à 6 par B210
ds_layout = "around"        # les autres de part et d'autre du principal
qam = 256

[upstream]
us_channels = 2             # 60 et 53 MHz : agrégation montante DOCSIS 3.0
us_target = -20             # dBFS des rafales après ranging

[provisioning]              # DHCP, TFTP, ToD sur le pont cmts0
dns = "1.1.1.1"

[host]
all_in_one = true           # le serveur de provisionnement aussi
status_http = "127.0.0.1:8090"
```

Exemples dans [`configs/`](configs/) : `b210.toml` (6 DS + 2 US), `2xb210.toml` (deux B210),
`bladerf.toml`. Toutes les options sont dans `man sdr-cmts` ; les mesures derrière les valeurs par
défaut dans [`docs/OPTIONS.fr.md`](docs/OPTIONS.fr.md).

## Matériel et câblage

- Un **USRP B210** sur un contrôleur USB 3 à lui seul, partagé avec rien d'autre ; ou un bladeRF 2.0 micro.
- Un PC : 3 DS + 1 US tiennent sur un portable 4 cœurs, 6 DS + 2 US sur un i5-9500 6 cœurs.
- Un coupleur 1:2, un atténuateur de 8 dB, un second coupleur pour l'observation.

```
B210 TX/RX ── coupleur A [entrée] ──┬── sortie 1 ── modem câble
                                    └── sortie 2 ── 8 dB ── coupleur B [entrée] ──┬── B210 RX2
                                                                                   └── (récepteur d'observation)
```

La voie montante du modem atteint RX2 par l'isolation entre les sorties du coupleur A. **Jamais RX2
sans atténuation** : un modem émet jusqu'à +60 dBmV. Deux B210 : leurs TX/RX sur un coupleur 1:2,
leurs RX2 sur un autre (derrière l'atténuateur de 8 dB), les deux coupleurs et le modem sur les
sorties d'un coupleur 1:3. Lancer le CMTS, puis allumer le modem ; éteindre le modem avant d'arrêter.

## Page web

`http://127.0.0.1:8090/` pendant qu'il tourne (`status_http = "0.0.0.0:8090"` pour le réseau du labo) :

- débit des 30 dernières minutes, radios (underflows, paquets en retard, marge d'émission), canaux
  descendants et leur remplissage, canaux montants (bruit, retard du récepteur), alertes récentes ;
- chaque modem : état, temps, puissance, MER, et ce qu'il annonce dans son enregistrement et son DHCP
  (version DOCSIS, récepteurs et émetteurs, BPI+, IPv6, fabricant, modèle, numéro de série, version
  logicielle, eMTA / eDVA / eRouter intégrés), les équipements derrière lui ;
- un formulaire **Configuration** pour les options courantes (radio, canaux, fréquences,
  modulations, niveaux), vérifiées sur les bandes EuroDOCSIS, écrites dans le fichier de banc (`.bak`
  gardé), le CMTS redémarré dessus. Depuis la machine elle-même seulement (ou `ssh -L
  8090:127.0.0.1:8090 hôte`) ; `--status-edit on` depuis partout où la page est joignable.

`/status.json` donne les mêmes données, `/source.tar.gz` le code source du programme.

## Service et réglages de l'hôte

```bash
sudo systemctl enable --now sdr-cmts      # le banc de /etc/sdr-cmts/sdr-cmts.toml
journalctl -u sdr-cmts -f                 # sa console
```

- Un SDR débranché arrête le CMTS (code 3) ; le service le relance toutes les 15 s jusqu'à son retour.
- Pendant qu'il tourne, le CMTS met le gouverneur du processeur en `performance`, bloque les états
  de veille profonds et écarte les autres interruptions de ses cœurs. Après chaque arrêt, même tué,
  tout revient aux valeurs par défaut du noyau (`sdr-cmts --restore-host`, lancé par le service).
- Journaux et captures : `/var/lib/sdr-cmts/captures/`.

## Normes et radios

| | |
|---|---|
| Descendant | EuroDOCSIS, J.83 annexe A, 8 MHz, 6,952 Msym/s, 64/256-QAM, 1 à 6 canaux agrégés par B210 |
| Montant | TDMA (DOCSIS 1.x) ou ATDMA (2.0), QPSK à 64-QAM, 1,28 à 5,12 Msym/s, 5-65 MHz, 1 à 4 canaux |
| MAC | enregistrement DOCSIS 1.1 à 3.0, MDD, RCC, DSID, mode multi-canal MTC, CM-STATUS |
| USRP B210 | UHD 4.3 à 4.9 ; jusqu'à 6 DS + 2 US sur un seul ; deux B210 se partagent le descendant |
| bladeRF 2.0 micro | libbladeRF, 3 DS + 1 US ; choix et limites dans [`docs/BLADERF.fr.md`](docs/BLADERF.fr.md) |
| Non implémenté | DOCSIS nord-américain (annexe B), DOCSIS 3.1 (OFDM), chiffrement BPI+ |

## Mesures

Sagem F@st 3686, iperf3 à travers le modem :

| Banc | Résultat |
|---|---|
| Un B210, 6 DS 256-QAM (290-330 MHz) + 2 US ATDMA 64-QAM, i5-9500 | **283-284 Mbit/s** descendant (8-16 flux), **48-52** montant (8 flux), ping ~75 ms |
| Le même, 4 téléchargements après un temps calme | aucun blocage d'émission, aucun canal perdu |
| Le même, speedtest depuis Internet (l'hôte faisant le NAT) | 205-228 Mbit/s descendant, aucun incident |
| Deux B210, 2 + 2 DS + 2 US, sans 10 MHz commun, Threadripper 2950X | **188-189 / 49,5 Mbit/s**, 86 min avec la suiveuse recalée (12,4 ms) |
| bladeRF 2.0 micro, 3 DS + 1 US | **138 / 24,5 Mbit/s** |
| L'effet de chaque option, vu par un second B210 (fréquence, niveau, QAM, spectre, MDD, UCD) | tous conformes (`tools/arg_effects.py`) |

Proche des limites physiques des canaux (6 × 256-QAM ≈ 300 Mbit/s, 2 × 64-QAM ATDMA ≈ 50 Mbit/s).
Chaque essai avec sa date : [`docs/TESTS.fr.md`](docs/TESTS.fr.md) ; les étapes précédentes :
[`docs/HISTORY.fr.md`](docs/HISTORY.fr.md).

## Ce qui a été difficile

- **Le temps réel sur un PC** : priorité temps réel, le cœur des interruptions USB du B210 laissé
  libre, les autres interruptions (un pavé tactile, la carte réseau sous un speedtest) écartées, aucun
  fichier écrit depuis le chemin d'émission, le ramasse-miettes de Python figé au démarrage.
- **La rafale maximale du modem** (TLV 24.9 du fichier de configuration) plafonne chaque requête :
  la valeur par défaut, 3044 octets, limitait l'envoi à ~3,5 Mbit/s.
- **L'agrégation montante avec le Sagem** : un canal au-delà de 65 MHz vaut un REG-ACK 209 ; la
  fenêtre de dynamique, obligatoire en mode multi-canal, n'est acceptée que sous la forme qu'envoie un
  vrai CMTS (action « change », rien dans les RNG-RSP), ce qu'a montré l'écoute d'un CMTS, en réception
  seule. Les requêtes multi-canal ne portent que les octets nouveaux : il faut les cumuler.
- **UHD calibre la quadrature TX de l'AD9361** à l'ouverture des flux, par le filtre analogique RX :
  avec ce filtre à la largeur montante, le modem ne se calait jamais au-delà de 34,76 MHz d'horloge
  maître. Les flux s'ouvrent maintenant avec un filtre RX de 56 MHz, resserré ensuite.
- **L'horloge symbole montante du modem avait 4,3 ppm d'écart** : sans effet sur les rafales courtes,
  fatale sur celles de 3 ms.
- **Un `multiprocessing.Event` attendu par un processus qui meurt** bloque celui qui le lève : un SDR
  débranché figeait tout le CMTS, jusqu'à ce qu'un drapeau partagé le remplace.

## Outils pour le réseau d'origine du modem

Pour voir comment un vrai CMTS pilote un modem, la voie descendante du réseau d'où vient le modem
peut être enregistrée et décodée, **en réception seule** : la radio sur un coupleur, sa sortie TX
débranchée, le CMTS du labo arrêté.

| Outil | Rôle |
|---|---|
| `tools/origin_rx.sh` | enregistre la voie descendante avec `uhd_rx_cfile` (aucun flux d'émission) |
| `origin_listen.py` | décode l'enregistrement : SYNC, UCD, MAP, MDD et les messages destinés à la MAC d'un modem, dans un pcap |
| `origin_raw.py` | les octets décodés bruts, pour comparer plusieurs copies d'un message |
| `tools/origin_maps.py` | profils de rafale montants (UCD) et usage des MAP par canal, à partir de ce pcap |
| `tools/hackrf_us.sh`, `tools/hackrf_reboot_cap.sh` | la même chose avec un HackRF |

Seuls les messages de gestion sont gardés. Un enregistrement brut contient aussi le trafic d'autres
abonnés : ne pas le conserver ni le partager. Ne jamais émettre sur ce réseau.

## Documentation

| Page | Contenu |
|---|---|
| `sdr-cmts --help`, [`man sdr-cmts`](man/sdr-cmts.1) | chaque option, le fichier de banc, la page d'état, les fichiers, les codes de sortie |
| [`docs/INSTALL.fr.md`](docs/INSTALL.fr.md) | l'hôte, l'USB, la priorité temps réel, les radios, le service, l'essai sans radio |
| [`docs/OPTIONS.fr.md`](docs/OPTIONS.fr.md) | les principales options et les mesures derrière leurs valeurs par défaut |
| [`docs/BLADERF.fr.md`](docs/BLADERF.fr.md) | le bladeRF 2.0 micro : choix, pièges, limites |
| [`docs/JOURNAL.fr.md`](docs/JOURNAL.fr.md) | toute l'histoire : chaque étape, mesure et bogue |
| [`docs/TESTS.fr.md`](docs/TESTS.fr.md) | chaque essai sur le banc, avec sa date et son résultat |
| [`docs/HISTORY.fr.md`](docs/HISTORY.fr.md) | les résultats des étapes précédentes |

Installés avec le paquet dans `/usr/share/doc/sdr-cmts/` ; depuis les sources, `man -l man/sdr-cmts.1`.

## Compiler depuis les sources

```bash
sudo apt install python3-numpy python3-scipy python3-uhd uhd-host gcc iproute2
python3 tests/run.py                          # tous les tests sans radio (quelques minutes)
sudo prlimit --pid $$ --rtprio=99:99 --nice=40:40
python3 sdr_cmts.py --all-in-one --config configs/b210.toml     # depuis ici, en root ou avec sudo
./packaging/build-deb.sh                      # le .deb (demande seulement dpkg-deb)
./packaging/build-all-debs.sh                 # construit et vérifié dans chaque distribution (docker)
python3 packaging/gen_man.py --write          # man/sdr-cmts.1, après avoir changé une option
```

Python 3.11 ou plus récent. Les noyaux C (`cmts/*.c`) sont compilés à la première utilisation ; sans
compilateur, les versions Python de secours sont bien trop lentes pour une radio.

## Feuille de route

Souhaitées ensuite :
- **Un modem câble logiciel (SDR CM)** : le côté modem de DOCSIS sur une seconde radio, pour éprouver
  le CMTS face à un modem entièrement connu. Au labo, face à ce CMTS seulement.
- **ZeroMQ entre le CMTS et le SDR CM** : les échantillons échangés en logiciel au lieu de passer par
  l'antenne, comme srsRAN pour la LTE : tout le lien DOCSIS sur un seul PC sans radio, pour le
  développement et la CI.
- **Un CMTS réparti sur plusieurs PC** : les radios (et leur trafic USB) sur des machines distinctes,
  la MAC sur une seule, les horloges reliées (10 MHz / PPS, ou PTP).

Autres évolutions possibles :
- Un 10 MHz / PPS commun pour plusieurs B210 (`--clock-source external`), puis 3 + 3 DS et plus de radios.
- Le DOCSIS nord-américain : voie descendante J.83 annexe B ; le montant et la MAC sont déjà les mêmes.
- bladeRF : échantillons 8 bits (libbladeRF 2.5+) pour 2 US et 4 à 6 DS ; B210 et bladeRF mélangés.
- Plusieurs modems à la fois, un second modèle de modem ; BPI+ et provisionnement IPv6.
- Décharger le processeur (mesuré sur l'i5-9500, le 01/11/2026) :
  - **GPU intégré** (OpenCL, `tools/gpu_bench.py`) : la synthèse de 6 canaux, 0,80 cœur en AVX2,
    prend 0,62 ms par bloc de 1,88 ms sur un Intel UHD 630, il reste 0,32 cœur sur le processeur ;
    ~0,7 cœur gagné avec des tampons sans copie, 10 à 15 % d'un 6 + 2 ;
  - **le FPGA du B210** : le format USB 12 bits (`sc12`) réduit le trafic USB d'un quart ; les DDC du
    FPGA sur deux voies RX plafonnent l'horloge maître à 30,72 MHz (3 DS) ; une image « edge QAM »
    (J.83, QAM, mise en forme et mélange dans le FPGA, comme les vrais CMTS) retirerait ~1,3 cœur et
    ~90 % du trafic USB d'émission, au prix de mois de travail sur Spartan-6 ;
  - **le chemin des données de la MAC en C** (`tools/mac_load.sh`) : la MAC coûte ~0,15 cœur au
    repos et ~20 µs par trame descendante, ~1 cœur en 6 + 2 à pleine charge ; le trajet des trames en
    C gagnerait ~0,4 à 0,6 cœur et ne tiendrait plus le GIL de Python ; le plan de contrôle reste en
    Python. À mesurer avec un modem.

## Licence

**AGPL-3.0-only, ou licence commerciale** (double licence, [`COMMERCIAL.md`](COMMERCIAL.md)) : libre
d'utiliser, de modifier et de partager ; une version modifiée distribuée ou utilisée à travers un
réseau doit être accompagnée de son code source ; l'intégrer dans un produit fermé, ou garder ses
modifications privées, demande une licence commerciale. Contributions :
[`CONTRIBUTING.md`](CONTRIBUTING.md). © 2026 BILLY Rémi.

DOCSIS® et EuroDOCSIS™ sont des marques de Cable Television Laboratories, Inc. (CableLabs) ; ce
projet n'est ni affilié à CableLabs, ni certifié, ni approuvé par lui. Normes de référence (non
incluses) : CableLabs DOCSIS MULPIv3.0 et PHYv3.0, UIT-T J.83, ETSI EN 300 429.
