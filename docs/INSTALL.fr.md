# Installer mini-cmts

*[English version: INSTALL.md](INSTALL.md)*

Deux façons : le paquet Debian (Debian 12 ou plus récent, Ubuntu 24.04 ou plus récent), ou les
sources (tout Linux avec Python 3.11+, UHD et un compilateur C). Les deux demandent le même
matériel et le même soin au câblage : lire d'abord la partie **Sécurité** du README. Coax fermé
uniquement.

## 1. L'hôte

- **Processeur** : le coût du descendant croît avec le nombre de canaux par radio :
  - 1 à 3 DS + 1 US : un portable 4 cœurs ;
  - 6 DS + 2 US sur un B210 : un PC de bureau 6 cœurs (i5-9500 ; la performance d'un cœur compte
    plus que leur nombre) ;
  - 2 B210 avec 2 + 2 DS : essayé sur un Threadripper 2950X 16 cœurs.
- **USB 3** : chaque B210 sur un contrôleur USB 3 à lui seul, partagé avec rien d'autre (ni carte
  son, ni autre SDR, ni concentrateur). `lsusb -t` montre l'arbre. Des échantillons en retard ou
  manquants (`U`, `L`, `O` dans la ligne d'état) viennent le plus souvent d'un contrôleur partagé,
  pas du logiciel.
- **Priorité temps réel** : les processus radio et montants élèvent eux-mêmes leur priorité
  (`--rt-prio`, 50 par défaut). Le paquet Debian l'autorise au groupe `usrp`
  (`/etc/security/limits.d/99-mini-cmts.conf`) et au service ; depuis les sources :
  `sudo prlimit --pid $$ --rtprio=99:99 --nice=40:40` dans le shell qui le lance, ou les mêmes
  lignes dans limits.d.
- `sudo` sans mot de passe pour l'utilisateur qui le lance, ou le lancer en root : il crée le TAP
  `cmts0`, éloigne les interruptions du cœur de l'USB du B210, règle le gouverneur du processeur,
  lance `provision.py` (DHCP, TFTP, ToD sur des ports privilégiés).

## 2a. Le paquet Debian

```bash
./packaging/build-deb.sh            # -> mini-cmts_<version>_all.deb (demande dpkg-deb, python3, NumPy)
sudo apt install ./mini-cmts_*_all.deb
sudo uhd_images_downloader -t b2xx  # images FPGA du B210, une fois (uhd-host)
```

`apt` installe les dépendances : Python 3.11+, NumPy, SciPy, gcc (les noyaux C sont compilés à
l'installation pour ce processeur, AVX2 compris), iproute2, sudo ; recommandés : `python3-uhd`,
`uhd-host` ; suggérés : `bladerf`, `libbladerf2`, `bladerf-fpga-hostedxa4` pour un bladeRF,
`tshark`, `iperf3`.

Ce qu'il installe :

| Chemin | Quoi |
|---|---|
| `/usr/bin/mini-cmts` | le CMTS (`mini_cmts.py`), mêmes options ; `man mini-cmts` les décrit toutes |
| `/usr/lib/mini-cmts/` | le code, `tools/bladerf_check.py`, `tools/clock_drift.py` |
| `/etc/mini-cmts/mini-cmts.toml` | le banc du service (gardé lors des mises à jour) |
| `/usr/share/mini-cmts/configs/` | exemples : `b210.toml` (6 DS + 2 US), `2xb210.toml`, `bladerf.toml` |
| `mini-cmts.service` | le service systemd, **ni activé ni démarré** par l'installation |
| `/var/lib/mini-cmts/captures/` | les journaux et captures du service |

Disque déplacé sur un autre processeur ? `sudo dpkg-reconfigure mini-cmts` recompile les noyaux C.

## 2b. Depuis les sources

```bash
# Debian / Ubuntu
sudo apt install python3-numpy python3-scipy python3-uhd uhd-host gcc iproute2
# Arch Linux
sudo pacman -S python-numpy python-scipy libuhd gcc iproute2
sudo uhd_images_downloader -t b2xx
git clone <ce dépôt> && cd mini-cmts
python3 tests/run.py                # tous les tests sans radio (quelques minutes)
```

Les noyaux C (`cmts/*.c`) sont compilés à la première utilisation dans `cmts/_*.so` ; sans
compilateur C, les versions Python de secours sont bien trop lentes pour une radio.

## 3. La radio

- **B210** : `uhd_find_devices` doit le voir, en USB 3 (`uhd_usrp_probe` affiche `USB 3`).
  Un clone de B210 avec sa propre image FPGA (par exemple LibreSDR) la reçoit dans
  `~/.config/uhd.conf` :

  ```ini
  [serial=JN76WAJ]
  fpga=usrp_b210_fpga_libresdr.bin
  ```
- **bladeRF 2.0 micro** : `python3 tools/bladerf_check.py --loop` vérifie la carte, les versions,
  l'USB 3, les gains et 10 s d'émission + réception. Son FPGA doit correspondre à libbladeRF :
  voir `docs/BLADERF.fr.md`.
- **Câblage** : voir le README. Jamais l'entrée RX sans l'atténuateur de 8 dB.

## 4. Premier démarrage

```bash
sudo mini-cmts --config /etc/mini-cmts/mini-cmts.toml          # paquet
python3 mini_cmts.py --all-in-one --config configs/b210.toml   # sources
```

Puis allumer le modem. En une minute environ : `RANGING TERMINÉ`, DHCP, TFTP,
`REG-ACK ok -> MODEM EN LIGNE`. Le PC joint ce qui est derrière le modem par `cmts0`
(10.10.0.1/24). Ctrl+C l'arrête proprement ; éteindre le modem d'abord.

La page d'état, sur la même machine : <http://127.0.0.1:8090/> (canaux, radios, modems et ce
qu'ils annoncent, débit, alertes récentes, et un formulaire pour les options courantes qui réécrit
le fichier de banc et redémarre le CMTS). `--status-http 0.0.0.0:8090` pour la lire depuis le
réseau du labo (le formulaire y reste en lecture seule sauf `--status-edit on` ; depuis un autre
PC : `ssh -L 8090:127.0.0.1:8090 <hôte>` puis <http://127.0.0.1:8090/>), `none` pour la couper.

## 5. En service

```bash
sudoedit /etc/mini-cmts/mini-cmts.toml          # le banc : radio, canaux, provisionnement
sudo systemctl enable --now mini-cmts
journalctl -u mini-cmts -f                      # la console du CMTS
sudo systemctl stop mini-cmts                   # Ctrl+C pour le CMTS : radios arrêtées proprement
```

Il redémarre après une panne (15 s plus tard) : un SDR débranché arrête le CMTS (code 3) et le
service le relance toutes les 15 s jusqu'à ce que le SDR revienne. Les réglages de l'hôte changés
pour le fonctionnement (gouverneur du processeur, interruptions écartées des cœurs du CMTS) sont
rétablis après chaque arrêt, même tué (`ExecStopPost=mini-cmts --restore-host` ; à la main :
`sudo mini-cmts --restore-host`). Le journal de chaque lancement est aussi dans
`/var/lib/mini-cmts/captures/cmts_<date>_<heure>.log`.

## 6. Le fichier de banc

Les clés sont les options de `mini-cmts --help` (`--ds-channels` -> `ds_channels`) ; les sections
ne font que les regrouper, sauf `[provisioning]` (les options de `provision.py`). La ligne de
commande l'emporte sur le fichier :

```bash
mini-cmts --config /etc/mini-cmts/mini-cmts.toml --ds-channels 2 --status-http none
```

Une clé mal orthographiée est refusée avec les noms les plus proches, plutôt que de garder en
silence une valeur par défaut.
