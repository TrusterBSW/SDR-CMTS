# Résultats précédents

*[English version: HISTORY.md](HISTORY.md)*

Les étapes avant les valeurs par défaut actuelles, gardées pour mémoire (README : les résultats actuels).

Fonctionne aussi (expérimental, 10/10/2026, hors valeurs par défaut) :

| | Configuration | État |
|---|---|---|
| Descendant | 3 canaux agrégés (306 + 314 + 322 MHz) | 142,8 Mbit/s mesurés une fois ; le modem laisse souvent un canal de côté (service partiel), voir plus bas |
| Agrégation montante | 2 canaux ATDMA 64-QAM 5,12 Msym/s (60 + 53 MHz), `--us-channels 2` | modem en ligne en mode multi-canal (MTC), ranging sur les deux canaux, trames reconstituées à partir de segments envoyés sur les deux ; débit mesuré depuis : 44-47 Mbit/s, dernière ligne |
| Agrégation montante | 4 canaux ATDMA 64-QAM 2,56 Msym/s (60 / 56,4 / 52,8 / 49,2 MHz), `--us-channels 4 --us-rate 16 --us-spacing -3.6e6 --us-target -20` | en ligne, ping 30/30, 2 640 segments des 4 canaux réassemblés, aucun perdu |
| Ensemble | 3 canaux descendants + 2 montants | en ligne à chaque fois ; les 3 canaux descendants acquis 6 fois sur 19 (gain TX de 44 à 52 sans différence nette), sinon service partiel sur 2 |
| Ensemble | 3 canaux descendants + 4 montants | en ligne une fois avec tous les canaux, à la limite du processeur du portable 4 cœurs (montant en retard de ~150 ms) |
| Ensemble, processus radio | `--radio-process --ds-channels 3` (3 DS + 1 US), PC 6 cœurs (i5-9500) | **130,8 Mbit/s descendant / 20,2 Mbit/s montant** (iperf3 à travers le modem) ; les canaux non acquis à l'enregistrement sont rattachés par CM-STATUS |
| Ensemble, processus radio | `--radio-process --ds-channels 3 --us-channels 2`, même PC | 130,8 / 15,8 Mbit/s : l'agrégation montante n'apporte pas encore de débit (les processus montants sont à 70-90 %, latence ~93 ms) |
| Descendant, processus radio | `--radio-process --ds-channels 4` : horloge maître 34,76 MHz (5 éch./symbole), PC 6 cœurs | **156 Mbit/s** descendant (131 sur un autre essai), 19,3 montant ; les 4 canaux portent chacun un quart du trafic ; les 2 canaux centraux (de part et d'autre de l'oscillateur local) décrochent parfois et reviennent par CM-STATUS |
| Descendant, processus radio | `--radio-process --ds-channels 5` : horloge maître 55,616 MHz, synthèse AVX2, PC 6 cœurs | **215,6 Mbit/s** descendant, 20,2 montant, ping 91 ms ; 6 DS : 208-209 Mbit/s mais montant effondré (2,9 Mbit/s) et ping jusqu'à 1 s, à reprendre |
| Descendant, processus radio | `--radio-process --ds-channels 6 --ds-layout around` : 290-330 MHz, 306 principal au milieu, 55,616 MHz, trames USB de 16 Ko | **257-279 Mbit/s** descendant (canaux remplis à 100 %), 21 montant, ping ~97 ms (170 ms pendant un envoi) ; **c'est le défaut** depuis le 2026-10-11 |
| Ensemble, processus radio | 6 DS + `--us-channels 2` : processus montants sur tous les cœurs à priorité plus basse que l'émission, contre-réaction sur le retard, DDC dans son propre fil, une seule recherche de préambule par rafale | **269-277 Mbit/s descendant / 24 Mbit/s montant**, modem jamais oublié, 99,8 % des rafales décodées, ping 78 ms (~370 pendant un envoi) ; paquets TS fabriqués en C, trames réparties à tour de rôle sur les canaux |
| Ensemble, agrégation montante | 6 DS + `--us-channels 2` (défauts `--mtc-std --mtc-owed --ds-aqm 30`) : enregistrement et requêtes multi-canal conformes au MULPI, comme un vrai CMTS (fenêtre de dynamique dans la TCC, requêtes cumulées, grant pending), CoDel sur le descendant, segments montants décodés entièrement en C | **270-284 Mbit/s descendant / 44-47 Mbit/s montant** (iperf3, 8-16 flux), ping 75 ms ; l'agrégation montante apporte enfin du débit |
