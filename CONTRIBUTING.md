# Contributing

*[Version française plus bas](#contribuer)*

Contributions are welcome: bug reports with the CMTS log (`captures/cmts_*.log`), tests, fixes,
support for other radios or modems.

Because mini-cmts is also offered under a commercial licence (see `COMMERCIAL.md`), the copyright
holder needs the right to relicense every part of it. By submitting a contribution (pull request,
patch or code in an issue), you agree that:

1. it is your own work, or you have the right to submit it;
2. it is licensed to the project under the GNU AGPL v3.0 only;
3. you grant BILLY Rémi a perpetual, worldwide, non-exclusive, royalty-free and irrevocable licence
   to use, modify, sublicense and distribute it, including under commercial terms.

Please add a `Signed-off-by:` line to your commits (`git commit -s`) to confirm this.

Before a pull request: `python3 tests/run.py` must pass. Never connect the bench to an operator's
network.

---

## Contribuer

Les contributions sont les bienvenues : rapports de bogue avec le journal du CMTS
(`captures/cmts_*.log`), tests, corrections, prise en charge d'autres radios ou modems.

mini-cmts étant aussi proposé sous licence commerciale (voir `COMMERCIAL.md`), le titulaire des
droits doit pouvoir en relicencier chaque partie. En proposant une contribution (pull request,
correctif ou code dans une issue), vous acceptez que :

1. elle soit votre propre travail, ou que vous ayez le droit de la proposer ;
2. elle soit placée sous GNU AGPL v3.0 seulement au sein du projet ;
3. vous accordiez à BILLY Rémi une licence perpétuelle, mondiale, non exclusive, gratuite et
   irrévocable pour l'utiliser, la modifier, la sous-licencier et la distribuer, y compris sous
   conditions commerciales.

Ajoutez une ligne `Signed-off-by:` à vos commits (`git commit -s`) pour le confirmer.

Avant une pull request : `python3 tests/run.py` doit passer. Ne jamais relier le banc au réseau
d'un opérateur.
