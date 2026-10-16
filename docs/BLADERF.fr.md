# Faire tourner le CMTS sur un bladeRF 2.0 micro (Nuand)

Le CMTS a été écrit autour du B210 d'Ettus (UHD). Il tourne maintenant aussi sur un **bladeRF 2.0
micro** (`--sdr bladerf`). La puce RF est la même (AD9361), mais la carte, le FPGA, le firmware USB
et la bibliothèque sont différents. Cette page réunit les choix faits pour qu'il marche, ce qu'il
atteint et où il s'arrête, mesuré le 15/10/2026. Version anglaise : `docs/BLADERF.md`.

## Résultat

| | bladeRF 2.0 micro | B210, pour comparaison (mesures antérieures) |
|---|---|---|
| Configuration | 3 DS (256-QAM, 6,952 Msym/s) + 1 US (ATDMA 64-QAM, 5,12 Msym/s) | jusqu'à 6 DS + 2 US |
| Descendant, 8 et 16 flux TCP | **138 Mbit/s** (les 3 canaux pleins, ~46 chacun) | ~47 par canal : 283 avec 6 DS |
| Montant, 8 flux | **24,5 Mbit/s** (un canal plein) | ~48-52 avec 2 canaux |
| Ping à travers le modem | ~144 ms | ~68 ms |
| MER montant | 22 dB | 27-30 dB |

Modem : Sagem F@st 3686 (DOCSIS 3.0), en ligne, les 3 canaux agrégés. Hôte : Threadripper 2950X,
Debian 12, libbladeRF 2.4.1, FPGA du bladeRF 0.14.0.

```bash
python3 tools/bladerf_check.py --loop        # carte, versions, USB 3, gains, 10 s TX+RX, retard TX->RX
python3 sdr_cmts.py --all-in-one @configs/bladerf.args
```
Câblage : **TX1** vers le coax du modem (comme la TX/RX du B210), **RX1** pour le montant (comme
son RX2), par les mêmes coupleurs et le même atténuateur.

## Choix

- **libbladeRF appelée directement** (`cmts/sdrdev.py`, `ctypes` sur `libbladeRF.so`) : ni SoapySDR
  ni autre couche, et pas le module Python `bladerf`, absent de certaines distributions. Le
  processus radio parle à une petite interface (`UhdRadio`, `BladeRFRadio`) : le reste du CMTS est
  le même pour les deux cartes.
- **Un seul débit pour l'émission et la réception.** Sur le bladeRF 2.0, les deux sens ne peuvent
  pas avoir des débits différents : régler l'un règle l'autre (mesuré). D'où un tick par
  échantillon (pas d'horloge maître au-dessus du débit) et pas de décimation du montant par le
  FPGA ; les processus montants extraient chaque canal du flux large bande en logiciel (DDC en C),
  comme ils le faisaient déjà avec plusieurs canaux montants.
- **Horodatage matériel** (`SC16_Q11_META`) : le premier bloc ouvre la salve à son horodatage,
  chaque bloc suivant porte `TX_UPDATE_TIMESTAMP` (bourré de zéros s'il y a un trou), comme le
  chemin B210 horodate chaque bloc. Réception : des tampons entiers lus à la suite, chaque lecture
  demandant l'horodatage qui suit la précédente ; `RX_NOW` seulement au départ et après une perte.
  Les échantillons antérieurs à l'heure de départ sont jetés.
- **Heure de l'appareil.** Les compteurs du bladeRF ne se règlent pas : heure = compteur TX + un
  décalage tenu en logiciel. Les compteurs TX et RX sont en fait la même horloge (une salve émise
  revient en réception ~3 µs plus tard) ; les lire l'un après l'autre par USB avait fait croire à
  un décalage de 165 µs. Il est maintenant pris d'une lecture RX encadrée de deux lectures TX.
- **Montant sous la plage de réception.** Le bladeRF 2.0 ne reçoit qu'à partir de 70 MHz (il émet
  dès 47 MHz). L'oscillateur de réception est à 70 MHz et l'unique canal montant à 61,8 MHz
  (58,6-65 MHz, l'EuroDOCSIS s'arrête à 65 MHz) : le DDC le décale de 8,2 MHz.
- **Gains.** TX 50 dB (échelle libbladeRF ; les 3 canaux à peu près au niveau du B210), RX 14 dB :
  à 20 dB le modem arrivait au-dessus de la cible du ranging déjà à sa puissance minimale, et le
  ranging n'aboutissait jamais.

## Pièges rencontrés (gérés par le pilote)

- **FPGA plus récent que la bibliothèque.** La carte avait le FPGA 0.16.0 en flash (outils bladeRF
  2025.10) ; la libbladeRF 2.4.1 de Debian 12 lit alors mal les métadonnées des flux : tampons RX
  de 508 échantillons dont l'horodatage avançait de 2044, chaque lecture marquée en dépassement,
  la réception à 7 MS/s sur 27,8. Avec le FPGA de la même version (0.14), ça marche. Le pilote
  refuse ce mélange et donne la commande pour charger le bon FPGA jusqu'à la coupure de courant :
  `bladeRF-cli -l /usr/share/Nuand/bladeRF/hostedxA4.rbf` (ou installer libbladeRF ≥ 2.6).
- **L'énumération `bladerf_format` a changé** : `SC16_Q11_META` vaut 1 jusqu'à libbladeRF 2.5 et 2
  à partir de 2.6 (`SC16_Q11_PACKED` inséré avant). Choisi d'après `bladerf_version()` ;
  `SDR_CMTS_BLADERF_META_FORMAT` le force.
- **Un gain réglé avant l'activation du module est ignoré** (libbladeRF 2.4.1 répond « succès ») :
  réglé de nouveau une fois les modules actifs ; le journal affiche les gains réellement en place.
- **Lire avec `RX_NOW` à chaque fois** rendait les quelques échantillons déjà arrivés (~50 par
  appel) : la réception prenait un retard énorme.
- La carte est réinitialisée à sa fermeture ; la rouvrir aussitôt répond « aucun appareil » :
  nouvelles tentatives pendant quelques secondes.

## Limites

- **3 DS + 1 US sur cet hôte.** Émission et réception au même débit, échantillons 16 bits :
  27,8 MS/s dans les deux sens (222 Mo/s d'USB) tient ; 34,8, 41,7 et 55,6 MS/s (4, 5, 6 DS) non
  (émission en retard, dépassements en réception), mesuré avec `tools/bladerf_check.py --rate`.
- **Deux canaux montants** demandent de recevoir jusqu'à ~50 MHz depuis l'oscillateur à 70 MHz :
  environ 50 MS/s ou plus, donc l'émission aussi.
- **La voie vers 6 DS + 2 US** : les échantillons 8 bits (`SC8_Q7_META`, libbladeRF ≥ 2.5 avec un
  FPGA récent) divisent le trafic USB par deux, 55,6 MS/s dans les deux sens reviennent à
  ~222 Mo/s. Pas encore fait. Le CPU compte alors autant : le 2950X utilisé ici (mémoire sur un
  seul canal) ne suivait déjà pas 6 DS avec un B210.
- **Latence plus élevée** : le tampon d'émission de libbladeRF (64 × 16384 échantillons ≈ 38 ms)
  et la réserve USB plus profonde : ping ~144 ms contre ~68 avec le B210. À régler.
- **MER montant de 22 dB** (bruit à −54 dBFS contre −63 sur le B210) : suffisant pour le 64-QAM.
- **Pas de 10 MHz externe** géré par ce pilote (un avertissement), et un seul `--sdr` pour toutes
  les radios : un B210 et un bladeRF ne peuvent pas encore être mélangés dans un même CMTS
  (`--ds-radios`).
- **Les échantillons émis en retard ne sont pas signalés** par l'interface synchrone de
  libbladeRF (seuls les underruns, par appel) : le compteur `L` de la ligne d'état reste à 0.
- **bladeRF 1** (LMS6002D) : refusé pour le montant, il ne reçoit qu'à partir de 300 MHz (il
  faudrait le transverter XB-200).
