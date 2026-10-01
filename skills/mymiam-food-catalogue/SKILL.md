---
name: mymiam-food-catalogue
description: "Choisir les aliments d'un repas MyMiam dans le catalogue Ciqual et les produits Open Food Facts enregistrés, en conservant leur provenance et les valeurs inconnues."
---

# Utiliser le catalogue MyMiam

Utiliser `nutrition.search_foods` avant la fiche finale : regrouper les recherches
courtes des différents aliments en un appel. Lire les noms et compositions retournés
pour choisir la correspondance ; ne pas choisir automatiquement le premier résultat.

- Préserver cuit/cru, morceau, espèce, matière grasse, produit et marque lorsqu'ils
  sont donnés. Ne pas substituer une pâte à pizza à une pizza prête à manger.
  Pour une pizza sans garniture précisée, préférer « Pizza (aliment moyen) ».
- Pour un produit de marque absent, rechercher une catégorie générique pertinente
  et signaler l'approximation dans `note`. Ne pas prétendre avoir trouvé la marque.
  Une recherche plus courte peut aider ; ne pas supprimer un qualificatif qui
  changerait la composition (notamment cru/cuit).
- `food_id` doit être un identifiant réellement renvoyé par l'outil pour ce repas,
  y compris dans les options de précision. Si aucune correspondance raisonnable
  n'existe, utiliser `food_id=null`, un libellé précis et une portion estimée :
  MyMiam conserve alors une composition inconnue et un repas modifiable.
- `nutrition.calculate_portions` peut vérifier les nutriments d'une ou plusieurs
  portions à partir des identifiants trouvés, si cela aide à apprécier l'impact
  d'une précision. Les valeurs viennent du catalogue ; une valeur absente reste
  inconnue. Le serveur recalcule toujours le bilan définitif.

Normalement un appel groupé de recherche puis la fiche suffisent. Limiter les
recherches supplémentaires aux correspondances difficiles ; éviter les calculs
redondants. Ne jamais inventer d'identifiant, de calories ou de macronutriments.
Les outils sont locaux, limités au catalogue et aux calculs : ils ne donnent accès
ni aux jetons de connexion, ni au système, ni aux comptes Garmin ou Renfo.
