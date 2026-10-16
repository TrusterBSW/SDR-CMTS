# Plan : agrégation montante, d'après le CMTS du réseau d'origine (12/10/2026)

Source : capture B210 d'un redémarrage du Sagem 3686 sur la prise du réseau d'origine (282 MHz, MER 36 dB),
décodée par `origin_listen.py`. Les résultats sont dans `~/origincap/rb1/` (portable) et
`/root/origincap/rb1/` (Proxmox). L'analyse se refait avec `tools/origin_maps.py`.

Le CMTS du réseau d'origine est un ARRIS (OUI 00:01:5c, ex-Cadant : C4/C4c ou E6000).

État de référence, gardé avant toute modification :
- tag git `ok-2026-10-12-avant-ecoute` (commit 9d6bf80 : 6 DS + 2 US, 277 / 24 Mbit/s) ;
- copie complète sur le Proxmox : `/root/mini-cmts.ok-2026-10-12` et `/root/cmts-tools.ok-2026-10-12`.

## Constat principal

| | Réseau d'origine | Nous |
|---|---|---|
| MAP envoyé avant ses allocations | ~2,5 ms | décidé ~25 ms avant (file d'émission USB) |
| Ack time | maintenant − 2,3 ms | position du récepteur, parfois 100 ms ou plus en retard |
| Boucle requête → grant | ~5-8 ms | ~45-55 ms |

- En mode MTC, le Sagem ne demande que les octets nouveaux. Nos journaux le montrent :
  152 requêtes/s × 8,7 Ko ≈ octets reçus.
- Le débit vaut donc environ les octets que le modem garde en attente, divisés par la durée
  de la boucle. C'est un plafond indépendant du nombre de canaux, d'où ~22 Mbit/s avec 1 ou 2 US.
- Le réseau d'origine sert un modem agrégé voisin ainsi : une requête de ~115 Ko est accordée en ~7 ms, en grants
  de 139 mini-slots, deux par canal et par MAP, sur les 5 canaux en même temps.

## A. Enregistrement identique au réseau d'origine (peu coûteux, à faire d'abord)

1. Ajouter TLV 47.3 = {1: 0, 2: 0, 3: 0, 4: 0}, que le réseau d'origine envoie explicitement et nous jamais.
2. Fenêtre de dynamique dans la TCC, sous la forme exacte du réseau d'origine : réf. dernière, 46.2 = 2,
   46.3 = 0, 46.9 = 6. Ne plus l'envoyer dans les RNG-RSP (TLV 14)  : le réseau d'origine ne le fait jamais.
   Notre 209 venait peut-être du double envoi.
3. TLV 52 = 60, TCC « sans action » en référence 0, 46.7 = 3.
4. Mesurer ensuite les octets que le modem garde en attente (requêtes cumulées moins octets reçus).

## B. Compenser notre latence (le gros du gain)

5. Requêtes MTC cumulatives : `pending[s_] = n` remplace ce qui reste dû au lieu de l'ajouter
   (`cmts/ranging.py`, `_request_bytes`).
6. Grants proactifs dimensionnés sur le débit mesuré × la boucle (~50 ms), répartis sur tous les
   canaux en même temps, comme le réseau d'origine, au lieu du tour de rôle. Il faut de la marge CPU au
   récepteur montant.
7. Raccourcir la boucle :
   - passer en C le reste du récepteur montant (~0,35-0,4 cœur gagné) ;
   - réduire `--tx-queue` ;
   - recaler l'ack time sur le dernier point réellement décodé.

## C. Réalisme

8. Pré-égalisation : coefficients calculés à partir de notre égaliseur de réception, envoyés en
   TLV 4 des RNG-RSP. Le réseau d'origine envoie 24 coefficients, tap principal 8, à chaque maintenance.
9. Maintenance toutes les ~20 s par canal, UCD toutes les 1,6 s, MDD toutes les 2 s, MAP de 3,2 ms.
10. (Écarté par l'utilisateur : 16-QAM et mini-slot de 12,5 µs comme le réseau d'origine, ça réduirait le débit.)
11. Même SID sur tous les canaux et TLV 47 pour chaque flux montant. Ça demande de réindexer nos
    tables par (SID, canal).
12. Capacités renvoyées telles quelles, comme le réseau d'origine. Nous mettons 3, 4, 12, 15 et 5.37 à 0.

## Autres valeurs relevées sur le réseau d'origine

- Mini-slot 2 ticks.
- Profils :
  - IUC 9 : 16-QAM, T=6, k=78, rafale max 14 ;
  - IUC 10 : 16-QAM, T=8, k=224, rafale max 255 ;
  - IUC 11 : QPSK ;
  - préambule 104 bits ; IM 640 bits ; SM 384 bits.
- Backoff 2-7 / 2-8. SYNC toutes les 10 ms.
- TCC : ranging des canaux ajoutés avec timing −48 et puissance 0, relatifs au canal 2.
  Les canaux ajoutés sont rangés ~4 s après le REG-RSP-MP.
- Maintenance du modem : pas de TLV 14, TLV 13 (T4) présent.
- Requêtes unicast (IUC 1 avec le SID du modem) pendant l'enregistrement, sur le canal du ranging.
- Flux du fichier de config : 24.14 = 0, 24.15 = 2 ; ni 24.16 ni 24.26/27.
- MDD : 32 DS, 20 US actifs, liste d'ambiguïté de 16 canaux, TLV 12, TLV 15 = 0608.

## Résultats au banc (12/10, 6 DS + 2 US, `--us-target -20`, iperf3 4 flux, 20 s)

| Essai | Montant (Mbit/s) | Descendant | Remarque |
|---|---|---|---|
| t60 référence | 24,2 | 224 | |
| t61 `--mtc-std` | 20,5 / 21,4 / 24,4 | 236 | TCC avec fenêtre de dynamique **acceptée** (plus de 209) ; débit inchangé |
| t62 `--mtc-std --mtc-owed` | **28,1 / 26,7 / 28,4** | 215 | +20 % ; requêtes toutes servies ; US2 en retard (60-112 ms) → grants en avance retenus |
| t63 idem `--grant-ahead-ms 0` | 14,4 / 5,4 / 35,2 | 148 | instable (envoi bloqué ~30 s) ; record ponctuel à 35,2 |

Limite actuelle : le CPU des processus de réception montante. Chacun tourne à 64-70 %, plus ~21 % de
DDC, en priorité 40, derrière l'émission. ~21 % du CPU reste libre au total. Prochaine étape : B7 (C du
récepteur montant) avant B6, puisque B6 ajoute des rafales.

## Suite (12/10 après-midi) : descendant, CoDel, récepteur montant en C

Les mesures sont maintenant faites avec 4, 8 et 16 flux. Avec 4 flux, TCP ne remplit pas les canaux
(70-80 %) : le descendant paraissait à ~220 Mbit/s, alors qu'avec 8-16 flux on a 260-284, comme la veille.

| Essai | Descendant 4 / 8 / 16 flux | Montant 4 / 8 flux | Ping en téléchargement |
|---|---|---|---|
| t65 `--ds-aqm 15` | 197 / 268 / 274 | 27,3 / 35,9 | 55 ms |
| t66 `--ds-aqm 30` | 218 / 260 / 281 | 30,2 / 34,8 | 64 ms |
| t67 sans CoDel | 211 / 266 / 282 | 32,1 / 37,1 | 88 ms |
| t68 chemin C des segments | 232 / 283 / 284 | 26,3 / 33,7 | 89 ms |
| **t69 chemin C + égaliseur recalculé en C** | 228 / 270 / 284 | **40,4 / 43,9, puis 46,6 / 44,8** | 94 ms |

- **CoDel** (`--ds-aqm MS`, désactivé par défaut) : pas d'effet mesurable sur le débit, ping en
  téléchargement 88 → 55-64 ms.
- **Chemin C des segments** (`usdsp.c seg_fast`, `us_phy._seg_fast`, actif par défaut ;
  `MINI_CMTS_US_FAST=0` pour revenir au Python) : il décode d'un seul appel une rafale de segment
  avec l'égaliseur en cache (filtre, préambule, symboles, égaliseur, débrouillage, RS). Le recalcul de
  l'égaliseur se fait aussi en C. Résultat identique au Python (`tests/test_seg_fast.py`).
  70-78 % des rafales passent par le C.
- Reste à faire : suivi de phase par blocs (`eq_track`, 37 ns/symbole), RS en SIMD, colle Python de
  `_handle` ; CPU encore à ~87 % sur 5 cœurs en envoi.

**Défauts depuis le 12/10 après-midi** (accord de l'utilisateur) : `--mtc-std` ,
`--mtc-owed` et `--ds-aqm 30`. Pour revenir en arrière : `--no-mtc-std`, `--no-mtc-owed`, `--ds-aqm 0`.
