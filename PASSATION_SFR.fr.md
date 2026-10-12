# Passation — écoute du réseau SFR et agrégation montante (12/10/2026, ~02:00)

**Objectif** : lire ce que le CMTS SFR envoie au Sagem F@st 3686 pour que l'agrégation montante
(multi-US) fonctionne, puis comparer avec notre mini-CMTS et corriger le nôtre (plafond montant
actuel ~22-24 Mbit/s, voir la mémoire `modem-bridge-caps-usbframes`, `us-bonding-mtc-2026-10-10`).
Repérer au passage les configurations particulières de SFR.

À lire dans la config SFR :
- REG-RSP-MP : TLV 47 (SID clusters, critères 47.3.1-4), 24.16 (politique de requête : bits 9 et 10),
  24.26 (multiplicateur), TLV 43 constructeur, 24.x des flux montants ;
- UCD des 5 canaux : profils de rafale (IUC 9/10/11/12), préambule, FEC, longueur max ;
- MAP : taille des grants, SID utilisés par canal, opportunités de requête, segments (CCF) ;
- MDD, RNG-RSP, le fichier de config TFTP et le DHCP.

## 1. Règles de sécurité (impératives)

- **Réception seule.** Le CMTS du Proxmox (10.5.1.2) reste arrêté (`/root/cmts-tools/stop.sh`).
  Rien ne doit émettre sur le coax SFR.
- **Le B210 n'est PAS branché sur SFR.** L'utilisateur craint que les rafales du modem, juste à côté
  sur le coupleur, grillent son étage de réception. Ne lancer aucune commande B210 (`sfr_rx.sh`)
  sans qu'il le demande explicitement.
- **HackRF seul**, sur la 2e sortie du coupleur dont la 1re alimente le modem.
  - **Ampli autorisé par l'utilisateur** (`-a 1`).
  - Niveaux mesurés au port du HackRF : rafales du modem ~**−21 dBm** en trafic normal. Pendant le
    ranging au redémarrage, la pire rafale n'est que ~2,5 dB plus forte. La limite est −5 dBm avec
    l'ampli allumé, +10 dBm avec l'ampli coupé.
  - Ne pas retourner le coupleur : ça amènerait les rafales à ~−15 dBm.
- Ne toucher à aucune autre entité Home Assistant que la prise du modem.
- Ne rien commiter sans demande. Tout le travail SFR est encore non commité (voir §5).

## 2. Câblage et état au moment de la passation

- Prise SFR → coupleur 1→2 → modem Sagem (MAC 24:7f:20:a8:38:b8) et HackRF (USB, portable).
- Côté SFR, mesuré dans l'interface web du modem (http://192.168.100.1, l'utilisateur a les
  identifiants) :
  - descendant : 24 canaux 256-QAM de 282 à 514 MHz, −2 dBmV (≈ −51 dBm), SNR 44 dB ;
  - **primaire = 282 MHz** (SYNC/UCD/MAP vus) ;
  - montant : **5 canaux ATDMA 5,12 Msym/s** à 34,8 / 41,2 / 47,6 / 54,0 / 60,4 MHz,
    46,5 à 48,3 dBmV.
- L'accès réseau derrière le modem est actif : le portable reçoit une IP publique SFR en DHCP.
- Côté portable :
  - carte USB du modem = `enp0s20f0u4`, connexion NetworkManager `sfr-lab` (DHCP sans route par
    défaut, plus les IP statiques 192.168.0.13 et 192.168.100.10) ;
  - route manuelle `9.9.9.9 via <passerelle SFR>`, pour la boucle `watch -n 0.1 dig google.com @9.9.9.9`
    lancée par l'utilisateur (elle génère du trafic montant).

## 3. Contrôler la prise du modem (Home Assistant)

- Home Assistant : `http://10.5.1.203:8123`. Jeton longue durée dans `~/.config/ha_token`
  (mode 600, ne jamais l'afficher).
- Prise du modem : **`switch.0x70b3d52b600443a8`** (non renommée). Sa puissance se lit sur
  `sensor.0x70b3d52b600443a8_power`, ~12-14 W modem allumé.

```bash
H="Authorization: Bearer $(cat ~/.config/ha_token)"
# état / puissance
curl -s -H "$H" http://10.5.1.203:8123/api/states/switch.0x70b3d52b600443a8
curl -s -H "$H" http://10.5.1.203:8123/api/states/sensor.0x70b3d52b600443a8_power
# couper / rallumer
curl -s -H "$H" -H "Content-Type: application/json" -d '{"entity_id":"switch.0x70b3d52b600443a8"}' \
     http://10.5.1.203:8123/api/services/switch/turn_off     # (turn_on pour rallumer)
```

**`tools/modem_cycle.sh [secondes_coupure]`** (10 par défaut) fait un cycle complet, testé deux fois :
1. il coupe la prise, puis la rallume ;
2. il relance `sfr-lab` si NetworkManager l'a abandonnée ;
3. il attend le bail SFR, en ignorant le bail 192.168.100.x que le modem donne avant d'être en
   ligne, puis remet la route vers 9.9.9.9 ;
4. il affiche la chronologie, environ **100 s de la remise sous tension au retour en ligne**.

**`tools/hackrf_reboot_cap.sh [secondes] [fréquence]`** fait la capture d'un redémarrage :
- `tcpdump` sur `enp0s20f0u4` → `.eth.pcap` ;
- HackRF ampli allumé, LNA 32, VGA 16, filtre 15 MHz → `.s8` (+ `.t0`) ;
- `modem_cycle.sh` 5 s après le début → `.cycle`.

Par défaut 170 s sur 282 MHz, soit 40 Mo/s.

## 4. Ce qui a été mesuré (et le problème principal)

**Le HackRF plafonne à MER ~25 dB sur le 256-QAM SFR**, quel que soit le réglage :

| Réglage | MER |
|---|---|
| ampli coupé, LNA 40 / VGA 30 | 24,0 dB |
| **ampli allumé, LNA 32 / VGA 16** | **25,3 dB** |
| FI 2300-2450 MHz (`-i/-o/-m 1`) | 25,7 dB |

Ce qui ne change rien au plafond :
- filtre de 10 ou 15 MHz (20 MHz est moins bon) ;
- 15 ou 31 coefficients d'égaliseur, réglages de la PLL ;
- correction du déséquilibre I/Q.

Décentrer de 3 MHz est pire (21 dB). L'erreur résiduelle est surtout additive : 26,1 dB en radial,
24,6 dB en tangentiel. Le journal notait déjà ~25 dB avec le HackRF sur SFR.

Conséquences :
- **À 25 dB de MER, le 256-QAM fait ~10 % d'erreurs symbole**, soit ~20 octets faux par mot RS de
  204 octets. Reed-Solomon (t = 8, ou 16 effacements) ne peut pas suivre : ~90 % des octets de
  synchro sont justes, mais le RS dur ne passe presque jamais.
- Une partie des trames passe quand même (rafales d'erreurs). Sur 1,7 s, gr-dtv a donné
  ~2000-3000 trames, dont surtout des MAP, UCD et SYNC. Les types « exotiques » (11, 131, 176…)
  viennent de trames corrompues.
- Il faut donc soit **plusieurs copies** de chaque message (vote), soit un meilleur récepteur.

Autres mesures :
- Bande montante : rafales de ~80 µs, ~10 à 15 par seconde avec la boucle dig, sur les 5 canaux.
- Calibration : rafale max = descendant 282 + 30,2 dB, soit −20,7 dBm.
- Débits et calcul :

| Élément | Valeur |
|---|---|
| Wifi portable ↔ Proxmox (iperf3) | 845 Mbit/s en 1 flux |
| Décodage sur le portable | ~40 s par seconde capturée |
| Décodage sur le Proxmox | ~10 s par seconde capturée |

## 5. Fichiers

**Captures dans `~/sfrcap/` (portable).** Elles contiennent le trafic d'autres abonnés : les
supprimer une fois décodées.
- `rb_012416.s8` (6,8 Go, 170 s) : 282 MHz ampli allumé pendant un redémarrage par la prise.
  Coupure à 01:24:21, rallumage à 01:24:31, en ligne à 01:26:11. Le `.eth.pcap` et le `.cycle`
  donnent la chronologie Ethernet. **Pas encore décodé.**
- `us_reboot.s8` (6,8 Go, 171 s) : bande montante centrée sur 47,6 MHz, ampli coupé, LNA 8 /
  VGA 10, pendant un redémarrage. Premières rafales vers 64 s.
- La copie `scp` de `rb_012416.s8` vers `root@10.5.1.2:/root/sfrcap/` a peut-être été interrompue
  (session coupée) : vérifier la taille. Le Proxmox ne répondait pas au moment de la passation
  (ssh timeout) : vérifier qu'il est joignable.

**Code non commité**, état réel :
- `sfr_listen.py` : décodeur de longues captures. Garde la diffusion de gestion et les trames du
  modem, puis assemble pcap, DHCP, fichier TFTP et REG-RSP-MP. Validé sur nos propres pcaps CMTS
  et sur une capture B210 convertie en 8 bits. Options `--fmt s8 --fs 20e6 --center 282e6
  --freq 282e6`.
  - **Le chemin « RS souple » ajouté cette nuit (`SOFT`, `byte_reliability`,
    `soft_outer_decode`, statistiques RS) n'est pas validé.** Au premier essai, il affichait
    « 100 % avec effacements », manifestement faux : alignement du désentrelaceur à vérifier.
  - Utiliser **`--hard`** (gr-dtv, comme avant), ou retirer ce chemin.
- `cmts/rsee.c` : RS erreurs + effacements (et souple : efface les octets les moins fiables).
  **Testé unitairement, correct**, jusqu'à 2e + f ≤ 16. Compilé par `cdsp._build("rsee")`.
- `sfr_raw.py` : **écrit, jamais exécuté.** Il extrait les paquets MPEG bruts non corrigés avec
  une fiabilité par octet. La synchro est re-cherchée tous les 48 paquets, car les longues tranches
  glissent de symboles : sur une tranche de 1,7 s, la synchro n'était bonne qu'au milieu. Le but est
  de voter octet par octet entre copies ; `sfr_vote.py` reste à écrire.
- `tools/` :
  - `modem_cycle.sh`, `hackrf_reboot_cap.sh` : testés ;
  - `hackrf_us.sh`, `iq_level.py`, `sfr_rx.sh` (B210, ne pas utiliser sans accord) ;
  - `iq_level.py` : son « niveau au-dessus du plancher » ne mesure **pas** le SNR quand la bande est
    pleine de canaux. Le plancher est alors le flanc du filtre.

## 6. Pistes, par ordre conseillé

1. **Interface web du modem** (l'utilisateur a les identifiants). Regarder d'abord s'il affiche les
   flux de service, le nom du fichier de config ou le journal d'événements. C'est le moins cher.
2. **Décoder `rb_012416.s8` en `--hard`** sur le Proxmox, pour voir ce qui passe déjà : UCD et MDD
   (beaucoup de copies), MAP des 5 canaux, peut-être des fragments de DHCP, TFTP ou REG-RSP-MP.
   - Les UCD et les MAP suffisent déjà pour une bonne part de la comparaison : profils de rafale,
     façon dont SFR distribue les grants sur les 5 canaux.
   - Attention : un symbole qui glisse fait perdre la synchro dans les tranches longues.
     `--chunk 1.5` est probablement trop long : essayer 0,5 s, ou recaler la synchro comme
     `sfr_raw.py`.
3. **Vote sur plusieurs copies** pour les messages de redémarrage (REG-RSP-MP, DHCP, fichier de
   config) :
   - N redémarrages par la prise avec `hackrf_reboot_cap.sh` ;
   - `sfr_raw.py` sur chaque capture ;
   - aligner au niveau de la trame MAC : l'en-tête et la MAC du modem sont connus, le contenu est
     identique d'un redémarrage à l'autre, mais pas la position dans les paquets MPEG ;
   - vote pondéré par la fiabilité, puis vérification RS, HCS et CRC.

   À ~10 % d'erreurs octet, 5 à 7 copies devraient suffire. Chaque redémarrage prend ~3 min de
   capture (6,8 Go) et ~30 min de décodage sur le Proxmox.
4. **Comprendre le plafond de 25 dB du HackRF**. Ce n'est ni le bruit d'entrée (l'ampli n'apporte
   que +1,3 dB), ni l'I/Q, ni le filtre, ni le niveau de l'ADC. Probable bruit de phase ou de
   l'horloge du HackRF ; une référence 10 MHz externe aiderait peut-être.
5. **B210** (MER > 35 dB attendu) : seulement si l'utilisateur le décide, avec les niveaux mesurés
   ci-dessus.
6. Ensuite, la **comparaison avec notre CMTS** (`mini_cmts.py`) : REG-RSP-MP, UCD, stratégie de
   MAP. Puis les correctifs pour que le multi-US apporte du débit. Aujourd'hui, l'agrégation US
   n'apporte aucun gain.

## 7. Remettre le labo en état après l'écoute SFR

1. Arrêter la boucle `watch … dig` de l'utilisateur. Supprimer la route
   (`sudo ip route del 9.9.9.9`) et la connexion `sfr-lab` (`nmcli con delete sfr-lab`).
2. Débrancher le coax SFR et rebrancher le TX/RX du B210 dans la distribution du labo. C'est à
   l'utilisateur de le faire.
3. Remettre `enp0s20f0u4` dans le netns `lan` (`nmcli dev set enp0s20f0u4 managed no`, `ip link set
   … netns lan`, dhclient), puis relancer le CMTS (`/root/cmts-tools/run.sh`, toujours
   `--no-irq-herd` et le CPU 0 libre, 2 US).
4. Supprimer les captures IQ brutes (`~/sfrcap`, `/root/sfrcap`) une fois décodées.
