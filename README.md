# MyMiam

Journal nutritionnel personnel : dictée du navigateur ou du clavier du téléphone,
interprétation avec Luna sur le forfait ChatGPT/Codex, estimation automatique des
portions et précisions facultatives, calculs depuis
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
Le transport vers Renfo utilise libcurl avec vérification TLS et sans redirection,
pour transmettre correctement SNI même si l'OpenSSL du système interprète mal
un hostname sslip.io commençant par une adresse IPv4.

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

## Saisie et dictée

Le bouton **Dicter mon repas** utilise `SpeechRecognition` du navigateur, en
français. Aucun modèle de transcription n'est installé sur le PC et aucun appel
à une API de transcription facturée n'est effectué. Selon le navigateur, l'audio
est transmis à son fournisseur de reconnaissance vocale : seul le texte obtenu
est envoyé à Luna. Fonctionnalité dépendante du navigateur, de sa connexion et
de l'autorisation du micro ; le texte et le clavier vocal du téléphone restent
utilisables. HTTPS et `Permissions-Policy: microphone=(self)` sont nécessaires.
[Compatibilité et fonctionnement](https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition).
Le modèle Luna connecté accepte le texte, sans entrée audio directe.
[Modalités du modèle](https://developers.openai.com/api/docs/models/gpt-5.6-luna).

**Envoyer à Luna** ferme le formulaire dès que l'envoi est conservé. Un worker
traite une file SQLite persistante, enregistre la meilleure estimation sans
question ni confirmation et déduit Matin/Midi/Soir/Collation depuis le texte.
L'heure locale et le créneau du bouton servent de repli. Le Dashboard indique
la progression et les erreurs, avec réessai ou annulation. Le texte d'un envoi
échoué reste conservé jusqu'à son retrait. Les envois réussis ou annulés sont
purgés après 24 heures ; les repas enregistrés restent dans le journal.

Luna peut proposer jusqu'à deux précisions facultatives de deux ou trois choix.
Cliquer recalcule les nutriments depuis le catalogue sans nouvel appel IA.
Les masses explicitement données restent fixes. Une composition non retrouvée
reste inconnue et modifiable, sans inventer de calories.

En développement, lancer aussi `.venv/bin/python scripts/process_captures.py`.
Pour l'installation de référence :

```sh
cp deploy/mymiam-captures.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mymiam-captures.service
```

Le worker utilise un verrou exclusif, reprend les traitements interrompus au
redémarrage et limite les doublons grâce aux identifiants d'envoi.

## Skills et outils de Luna

MyMiam charge explicitement deux skills du dépôt à chaque interprétation :

- [`mymiam-meal-capture`](skills/mymiam-meal-capture/SKILL.md) : créneau, unités,
  portions, hypothèses et choix facultatifs, sans question bloquante.
- [`mymiam-food-catalogue`](skills/mymiam-food-catalogue/SKILL.md) : correspondances
  alimentaires, provenance et valeurs inconnues.

Le chargeur ne lit que ces deux chemins contrôlés. Les skills sont intégrés aux
instructions de ce workflow ; il ne s'agit pas d'un upload de skill hébergé ni
d'une installation dans le profil global de Codex.

Luna dispose de deux fonctions locales, regroupées dans `nutrition` :
`search_foods` recherche plusieurs aliments dans Ciqual et les produits Open Food
Facts déjà enregistrés ; `calculate_portions` calcule les nutriments de portions
à partir des résultats trouvés. Aucun accès système ou aux identifiants de compte
n'est exposé. Garmin, objectifs et statistiques restent des calculs de MyMiam.

La première étape requiert un appel d'outil ; normalement une recherche groupée
puis la fiche finale suffisent. Au maximum trois étapes d'outils, six appels et
une réponse finale sans outils. Les appels utilisent toujours Luna et le quota
du forfait connecté, sans clé API ni repli payant. Le total des jetons et les noms
d'outils sont consignés dans `instance/last_usage.json`, sans le récit ni les
arguments. Une recherche locale peut nécessiter un échange supplémentaire avec
Luna et consomme donc davantage du quota qu'une fiche sans recherche.

Les identifiants retenus doivent avoir été retournés par les outils pour ce
repas ; le serveur les vérifie puis recalcule les nutriments. Il conserve le choix
fait par Luna entre les candidats, au lieu de prendre systématiquement le premier.
Un aliment sans correspondance reste enregistré avec une composition inconnue.
Les résultats d'outils ne sont traités qu'après un flux OpenAI terminé avec succès.
L'historique nécessaire est transmis explicitement, avec le raisonnement chiffré,
sans `previous_response_id` ni stockage distant demandé.

Les plugins installés dans ChatGPT/Codex (par exemple Calorie Tracker) ne sont pas
hérités par ces appels. La voie du forfait ne prend pas en charge les connecteurs
MCP hébergés. Les fonctions locales sont le branchement utilisé dans MyMiam ;
un éventuel adaptateur MCP local pourrait réutiliser leur logique ultérieurement.
[Limites officielles du forfait](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations),
[protocole de fonctions](https://developers.openai.com/api/docs/guides/function-calling).

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
  Macros initiales modifiables : 20 % protéines, 45 % glucides, 35 % lipides.
  Ce choix se situe dans les intervalles Anses pour l'adulte (protéines 10–20 %,
  glucides 40–55 %, lipides 35–40 % de l'apport énergétique total), et ne constitue
  pas une prescription individualisée. [Référence Anses, tableau 4](https://www.anses.fr/sites/default/files/NUT2012SA0103Ra-1.pdf).
- Pour les jours passés renseignés par Garmin, le **total quotidien remplace**
  l'estimation du profil ; ne pas ajouter les activités à nouveau. Aujourd'hui,
  le total Garmin est accumulé et affiché séparément de la projection sur 24 h.
- Le déficit est signé (`dépense − apports`) ; les surplus réduisent le cumul.
  Le cumul porte automatiquement sur les jours passés avec au moins un repas
  et une énergie calculable. Aucune confirmation de journée n'est nécessaire.
  Ajouter, modifier, déplacer ou supprimer un repas recalcule les bilans.
  Sans aucun repas enregistré, une journée est non saisie et exclue du cumul
  et des moyennes : elle ne produit aucun déficit artificiel, même avec Garmin.
  Un seul repas suffit pour la réintégrer automatiquement. Les paramètres du profil sont
  conservés lors de la saisie pour stabiliser les objectifs historiques ; les
  objectifs d'aujourd'hui restent modifiables avec le profil.
- Le Dashboard affiche le déficit journalier et son cumul sur 7, 30 ou 90 jours.
  Les journées manquantes interrompent les graphiques ; aujourd'hui est exclu
  des bilans définitifs. Sans repas saisi, les
  apports restent affichés inconnus. Le total Garmin observé est présenté
  séparément de l'objectif calculé sur une journée de 24 heures.
  Le journal est organisé en Matin, Midi et Soir, avec ajout direct dans chaque
  créneau ; les collations sont déduites du texte ou sélectionnables en saisie manuelle.
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
les inconnues nutritionnelles, l'idempotence, les bilans automatiques et le comptage
Garmin. Les tests navigateur utilisent des réponses simulées distinctes de toute
donnée personnelle ; les connexions réelles restent à valider par le propriétaire.

Code sous MIT. Barlow Condensed sous SIL Open Font License, dans
`static/assets/fonts/OFL.txt`. Le code public n'inclut ni repas, ni profils,
ni sessions, ni jetons OpenAI/Garmin, ni mots de passe.
