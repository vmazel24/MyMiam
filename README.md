# MyMiam

Journal nutritionnel personnel : dictée du clavier du téléphone, interprétation
avec Luna sur le forfait ChatGPT/Codex, validation des portions, calculs depuis
Ciqual 2025 et Open Food Facts, objectifs, tendances et calories Garmin.

Interface adaptée de l'application Renfo du même auteur : typographie Barlow
Condensed, cartes, navigation et contrôles, avec une palette nutrition verte.
Application Flask/SQLite, JavaScript natif et PWA. Python 3.12 ou supérieur.

## Démarrer

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p instance
chmod 700 instance
curl --fail -L 'https://ciqual.anses.fr/cms/sites/default/files/inline-files/Table%20Ciqual%202025_FR_2025_11_03.xlsx' -o /tmp/ciqual-2025.xlsx
.venv/bin/python scripts/import_ciqual.py /tmp/ciqual-2025.xlsx
.venv/bin/gunicorn --bind 127.0.0.1:8092 --workers 1 --threads 8 --timeout 180 app:app
```

Ouvrir `http://127.0.0.1:8092`. Configurer `RENFO_URL` avec l'origine HTTPS de
son instance Renfo, `MYMIAM_ORIGIN` avec l'origine exacte de MyMiam et
`MYMIAM_SECURE_COOKIES=1` derrière le proxy HTTPS. `MYMIAM_INSTANCE` permet de
déplacer les données. Le port 8092 reste sur l'interface locale.

Les fichiers `deploy/` décrivent l'installation de référence : service systemd
utilisateur et virtual host Apache distinct. Adapter les chemins et le hostname.
Le renouvellement du certificat utilise un webroot ACME distinct ; ne pas
remplacer les configurations des autres sites.

## Compte Renfo

Les mots de passe sont vérifiés par l'API Renfo en HTTPS et ne sont pas conservés
dans MyMiam. Cette première version accepte uniquement l'utilisateur propriétaire
(`legacyOwner`), lie son UUID de façon atomique et utilise une session opaque
propre à MyMiam. Chaque requête privée vérifie la session auprès de Renfo.
Les comptes amis ne peuvent pas accéder au journal ni utiliser le forfait IA.
Renfo doit rester disponible pour vérifier les sessions.

## Connecter le forfait ChatGPT

Exécuter sur le PC hôte, dans sa session utilisateur :

```sh
.venv/bin/python scripts/connect_chatgpt.py --instance instance
```

Le navigateur ouvre une page sur `http://127.0.0.1:9455`. Cliquer sur
**Continue with ChatGPT** et autoriser l'utilisation du forfait. Le programme
utilise le parcours officiel d'enregistrement dynamique des clients open source,
vérifie l'identité signée, le nonce, l'état OAuth et les permissions, puis conserve
les jetons dans `instance/` avec des permissions restrictives. Le serveur renouvelle
les jetons sous verrou. Il sélectionne une variante Luna proposée au compte.

Le bouton de connexion dans le profil lance ce même assistant **sur le PC hôte** :
sur un téléphone distant, `127.0.0.1` désigne le téléphone ; effectuer l'autorisation
initiale sur le PC. L'autorisation MyMiam est distincte de celle du client Codex.
La sélection dans le catalogue n'est pas une preuve d'accès : un repas interprété
avec succès valide l'accès réel. Aucun jeton n'est renvoyé à l'interface.

Pas de clé API facturée ni de repli vers un autre modèle. Les appels consomment
le quota partagé inclus. Dans [les paramètres d'usage ChatGPT](https://chatgpt.com/settings/usage),
**désactiver l'utilisation de crédits après la limite**. MyMiam ne peut pas changer
ce paramètre du compte. Un quota épuisé laisse la saisie manuelle disponible.

Documentation : [enregistrement](https://developers.openai.com/siwc/token-sharing-open-source/sign-in),
[VM auto-hébergée](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms),
[requêtes sur le forfait](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).

## Données et calculs

- **Ciqual 2025**, Anses : 3 484 aliments, données par 100 g, Licence Ouverte 2.0.
  Import depuis le fichier officiel ; données téléchargées et SQLite exclues de Git.
  [Source et conditions](https://ciqual.anses.fr/cms/fr/telechargement).
- **Open Food Facts**, ODbL : recherche par code-barres, provenance conservée dans
  chaque aliment. La base n'est pas redistribuée dans ce dépôt. [Documentation](https://openfoodfacts.github.io/openfoodfacts-server/api/).
- Les valeurs absentes, traces et limites de détection restent inconnues. Si une
  valeur manque dans un aliment, le total de ce nutriment reste inconnu.
- Luna propose des aliments et des poids ; le serveur calcule les nutriments
  depuis les correspondances du catalogue, indépendamment des chiffres fournis
  par le navigateur. Les portions estimées et hypothèses restent visibles.
- Dépense de repos estimée par Mifflin–St Jeor, multipliée par un facteur d'activité
  modifiable sans Garmin. [Publication de l'équation](https://pubmed.ncbi.nlm.nih.gov/2305711/).
  Macros initiales modifiables : 20 % protéines, 45 % glucides, 35 % lipides ; ce
  sont des paramètres de départ, pas une prescription individualisée.
- Pour les jours passés renseignés par Garmin, le **total quotidien remplace**
  l'estimation du profil ; ne pas ajouter les activités à nouveau. Aujourd'hui,
  le total Garmin est accumulé et affiché séparément de la projection sur 24 h.
- Le déficit est signé (`dépense − apports`) ; les surplus réduisent le cumul.
  Le cumul porte sur les jours passés explicitement confirmés complets avec une
  énergie calculable. Zéro repas sans confirmation signifie une journée inconnue.
  Modifier un repas rouvre la journée. Les paramètres du profil sont conservés
  au moment de la confirmation, pour stabiliser l'historique des objectifs.
- Les repas favoris servent aussi de recettes personnelles. Les exports JSON
  contiennent les données du journal et aucun identifiant de connexion.

## Garmin

Connexion depuis le profil, mot de passe non conservé, code MFA si nécessaire.
Les jetons sont conservés dans `instance/garmin/`. Synchronisation manuelle des
sept jours se terminant à la date sélectionnée. Le connecteur communautaire
[python-garminconnect](https://github.com/cyberjunky/python-garminconnect) peut
nécessiter une reconnexion si Garmin change son service. Aucun accès officiel
entreprise n'est supposé. Les données réelles exigent la connexion de l'utilisateur.

`deploy/mymiam-garmin.timer` propose également une synchronisation horaire des
sept jours récents, active après la connexion initiale. Les synchronisations
manuelles, automatiques et la connexion partagent un verrou de fichier pour
éviter les courses sur les jetons. Les activités sont affichées à titre de détail ;
leurs calories ne sont jamais ajoutées au total quotidien déjà fourni par Garmin.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
```

Les tests vérifient l'authentification, les origines, l'isolation, les quantités,
les inconnues nutritionnelles, l'idempotence, les journées complètes et le comptage
Garmin. Les tests navigateur utilisent des réponses simulées distinctes de toute
donnée personnelle ; les connexions réelles restent à valider par le propriétaire.

Code sous MIT. Barlow Condensed sous SIL Open Font License, dans
`static/assets/fonts/OFL.txt`. Le code public n'inclut ni repas, ni profils,
ni sessions, ni jetons OpenAI/Garmin, ni mots de passe.
