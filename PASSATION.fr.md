# Passation — mini-CMTS (état au 12/10/2026 au soir)

## 1. Où on en est

**Configuration de référence** : branche `main`, hôte Proxmox 10.5.1.2, Sagem F@st 3686.
- 6 DS en disposition « around » autour de 306 MHz, et 2 US (60 et 53 MHz).
- Défauts : `--mtc-std`, `--mtc-owed`, `--ds-aqm 30`, tampon montant du modem de 128 Ko.

| Mesure (iperf3 à travers le modem) | Valeur |
|---|---|
| Descendant, 1 à 16 flux | **283-284 Mbit/s** (capacité des 6 canaux) |
| Montant, 8 flux | **~52 Mbit/s** quand le Proxmox est calme ; 40-50 le 12/10 au soir (voir §5) |
| Montant, 1 flux | 11-25 Mbit/s (limite : rafales perdues sur l'US1, voir §4) |
| Ping au repos | ~72 ms |
| Ping pendant un téléchargement | ~72 ms |
| Ping pendant un envoi | ~87-93 ms (237 avant le tampon de 128 Ko) |

Ce qui a été fait le 12/10 (détails : `docs/TESTS.fr.md` T60-T65, `PLAN_AGREGATION_US.fr.md`) :
- **Écoute du CMTS SFR**, un ARRIS, en réception seule avec le B210. Lecture de son REG-RSP-MP, de ses UCD et de ses MAP.
- **Enregistrement multi-canal conforme** (`--mtc-std`) : le Sagem accepte enfin la fenêtre de dynamique, envoyée dans la TCC.
- **Requêtes montantes cumulées et grant pending** (`--mtc-owed`).
- **Récepteur montant en C** :
  - chemin rapide des segments ;
  - égaliseur recalculé en C ;
  - suivi de phase plus rapide ;
  - Reed-Solomon en une seule passe.
- **CoDel** sur la file descendante.
- **Tampon montant du modem de 128 Ko** (TLV 24.35).
- **Cause du bruit sur l'US1** : la 5e harmonique de l'oscillateur local de réception (5 × 56,5 MHz) replie le canal descendant 286-294 MHz dans l'US1. Voir §4.
- **TCP sur un seul flux en descendant** : la limite venait du client de test (carte USB-Ethernet sur un port USB 2.0). En USB 3, un seul flux fait 283 Mbit/s.
- **Multi-B210** sur la branche `multi-b210`. Testé avec un seul B210 seulement, voir §3.

## 2. Lancer, configurer, lire les journaux

Sur le Proxmox (`ssh root@10.5.1.2`). Le code de `main` est dans `/root/mini-cmts`, celui de la branche `multi-b210` dans `/root/mini-cmts-multi`.

```bash
/root/cmts-tools/stop.sh                                   # arrêter le CMTS en cours
setsid -f /root/cmts-tools/run.sh essai1 @configs/defaut.args            # main
setsid -f /root/cmts-tools/run_multi.sh essai2 @configs/multi-b210.args  # branche multi-b210
```

- **Fichiers de configuration** (`configs/*.args`) : une ou plusieurs options par ligne, avec des commentaires `#`.
  - On peut ajouter d'autres options après le fichier, et la dernière l'emporte : `run.sh essai3 @configs/defaut.args --us-channels 1`.
  - Ne pas mettre `stop.sh` et le démarrage dans une même commande `ssh '…'` qui contient « mini_cmts » : `stop.sh` tuerait la session ssh.
- **Journal court** (lignes d'état toutes les 5 s, enregistrements, alertes) : `/root/cmts-tools/<nom>.out`.
  - Suivi : `tail -f /root/cmts-tools/essai1.out`.
  - Dans la ligne d'état :
    - `U`/`L` : pertes d'émission ;
    - `marge TX min` : avance de l'émission ;
    - `US: bruit` : bruit des canaux montants ;
    - `retard max` : retard du récepteur montant ;
    - `en C` : rafales décodées par le chemin rapide ;
    - `pont ↑↓` : trafic ;
    - en fin de ligne, l'état de chaque modem (`online`, MER).
- **Journal complet**, avec le détail des REG-REQ : `/root/mini-cmts/captures/cmts_*.log` (le plus récent). Il est dans `/root/mini-cmts-multi/captures/` pour la branche.

  ```bash
  L=$(ls -t /root/mini-cmts/captures/cmts_*.log | head -1)
  grep -E "REG-REQ|capacités du modem|profil de réception|RCC|REG-ACK|EN LIGNE|CM-STATUS" $L
  ```

  - `capacités du modem` : version DOCSIS (0 = 1.0, 1 = 1.1, 2 = 2.0, 3 = 3.0), `canaux montants multiples` (nombre d'émetteurs), `canaux descendants multiples` (récepteurs), fragmentation, BPI…
  - `profil de réception (RCP)` : un profil par ligne, sous la forme `ID « nom » : modules M1:8@112-1002 ; N canaux (1>1P,…)`. `M1:8` signifie module 1, 8 canaux adjacents (`-` = sans limite). `1>1P` signifie que le canal de réception 1 est relié au module 1 et peut être primaire.
  - Un modem DOCSIS 2.0 envoie un REG-REQ simple : pas de profil de réception, et pas d'agrégation.
- **Tout voir dans Wireshark** : ajouter `--pcap captures/essai.pcap`, puis ouvrir le fichier avec le filtre `docsis`. Wireshark décode chaque TLV des REG-REQ et REG-RSP.
- Mesures : `iperf3 -s -B 10.10.0.1 -D` sur le Proxmox, puis `iperf3 -c 10.10.0.1 -B <IP du portable> -P 8 [-R]` sur le portable. Toujours mesurer avec 1, 4, 8 et 16 flux.

## 3. Prochaines étapes, dans l'ordre

1. **2e modem** : le décodeur Sagemcom DCIW344, avec un modem câble intégré, sans doute DOCSIS 2.0 ou 3.0 sans agrégation montante.
   - Le brancher sur une sortie de répartiteur TV (pas de domino).
   - Lire son REG-REQ (capacités, RCP éventuels).
   - Vérifier que les deux modems tiennent ensemble : ranging, SID, grants, et un éventuel PC derrière le décodeur.
2. **Usage réel** : YouTube, téléchargements, en surveillant le journal court.
3. **Multi-B210 (12 DS)**, sur la branche `multi-b210` :
   - `--ds-radios 2 --radio-serials A,B --clock-source external`, voir `configs/multi-b210.args` ;
   - **il faut deux contrôleurs USB** : le Proxmox n'en a qu'un, d'où le 2950X, avec un nœud NUMA et un contrôleur USB par B210 ;
   - numéros de série : `uhd_find_devices` ;
   - **10 MHz commun** : CLKOUT du HackRF → répartiteur → entrées « 10MHz » des deux B210. Sans référence commune (`--clock-source internal`), l'écart entre les deux radios grandit d'environ 1 ms toutes les 10 minutes ; c'est tolérable pour un premier essai court ;
   - combiner les deux sorties TX/RX avec un répartiteur monté à l'envers ;
   - déjà vérifié avec un seul B210 : la radio principale émet, la 2e échoue proprement, et le Sagem accepte un RCC de 12 canaux. Ses profils CLAB-8M-016/024 ont un seul module 112-1002 MHz, sans limite d'adjacence ;
   - avec 12 DS sur 266-354 MHz, les harmoniques 5 et 6 de l'oscillateur local de réception tombent dans le descendant, d'où du bruit sur le montant (§4) ;
   - fusionner dans `main` seulement une fois validé à deux B210.
4. **Fichiers de configuration complets (TOML)**. Les fichiers `@configs/*.args` sont la version simple, déjà utilisable. La suite :
   - un `--config lab.toml` lisible, avec des sections `[downstream]`, `[upstream]`, `[radios]` et `[provisioning]` ;
   - plusieurs profils de banc (`proxmox-6ds`, `2950x-12ds`…) ;
   - les options de `provision.py` (tampon, débits) dans le même fichier, au lieu de `--prov-args`.
5. **Filtre passe-bas (diplexeur CATV, ~65/85 MHz) devant RX2** : il supprimerait le repliement du descendant dans le montant (§4). C'est l'achat le plus utile pour le montant.
6. **Montant sur un seul flux** : moins de rafales perdues (filtre, ou pré-égalisation comme le CMTS ARRIS), puis réduire la boucle requête → grant.
7. **Encore du C dans le récepteur montant**, s'il manque du CPU : DDC (~22 % d'un cœur par canal), filtre adapté, le reste de la colle Python.
8. Réalisme, d'après le CMTS ARRIS (`PLAN_AGREGATION_US.fr.md`, partie C) :
   - pré-égalisation dans les RNG-RSP ;
   - maintenance toutes les ~20 s ;
   - même SID sur tous les canaux.

## 4. Pièges connus

- **Harmoniques de l'oscillateur local de réception** : avec 2 US, il est à 56,5 MHz.
  - Le descendant à ±6,7 MHz de 282,5 (5e harmonique) ou de 339 MHz (6e) se replie dans les canaux montants.
  - Un primaire à 314 MHz évite la 5e (bruit −60,5 au lieu de −47,6 dBFS), mais donne ~2 Mbit/s de moins en montant à 8 flux : 306 est conservé.
  - Le primaire doit être sur la grille européenne (114 + 8 n MHz) : le Sagem ne trouve jamais 310 MHz.
- **Charge du Proxmox** : le CMTS utilise ~4,3 cœurs sur 5, donc la moindre VM active se voit.
  - La carte son USB de la VM 103 (snapcast) causait des blocages USB de 30-58 ms : carte débranchée et VM arrêtée le 12/10.
  - Le 12/10 au soir, la VM Home Assistant (102) prenait 43 % d'un cœur : récepteur US1 en retard, montant à 40-50 Mbit/s au lieu de 52.
- **Prise Home Assistant du modem** (`switch.0x70b3d52b600443a8`) : ne répondait plus le 12/10 vers 16h (reste sur « on »).
- **Carte USB-Ethernet du portable** : à brancher sur un port USB 3. En USB 2.0, elle perd des paquets en rafales au-delà de ~300 Mbit/s. Le 12/10, elle a aussi cessé de recevoir pendant ~2 h, puis a été rebranchée.
- **NetworkManager** : `ipv4.never-default yes` sur « Connexion filaire 1 », sinon le modem devient la route par défaut du portable.
- **Copies de sûreté** : tag `ok-2026-10-12-avant-sfr`, `/root/mini-cmts.ok-2026-10-12` et `/root/cmts-tools.ok-2026-10-12`.

## 5. Annexe : écoute de SFR (terminée)

Câblage : prise SFR → coupleur → modem + B210 (RX2 seul, TX/RX débranché, sans atténuateur). Le CMTS du labo doit être arrêté pendant l'écoute.
- `tools/sfr_rx.sh rec SECONDES 40` (avec `CENTER=282e6 RATE=16e6`) : capture, MER 36 dB.
- `sfr_listen.py <fichier> --fs 16e6 --center 282e6 --freq 282e6 --hard` : décodage parallèle.
- `tools/sfr_maps.py <pcap>` : UCD et MAP.
- Résultats dans `~/sfrcap/rb1/` (portable) et `/root/sfrcap/rb1/`.
- Capture brute gardée : `/root/sfrcap/sfr_094245.sc16` (11,6 Go, contient le trafic d'autres abonnés : à supprimer quand plus utile).
- La prise et le portable : `tools/modem_cycle.sh`, `~/.config/ha_token`.
