# Irigo Map

Ce projet génère une carte interactive `irigo_map.html` à partir des données GTFS statiques et des flux temps réel.

## Usage

1. Exécutez le script Python pour générer ou régénérer la carte :

```powershell
python irigo_map.py
```

2. Ouvrez le fichier HTML via un serveur local pour garantir le bon chargement des ressources externes (CDN). Par exemple :

```powershell
python -m http.server 8000
```

Puis ouvrez dans votre navigateur :

```
http://127.0.0.1:8000/irigo_map.html
```

> Ouvrir `irigo_map.html` directement avec `file://` peut parfois provoquer un rendu blanc si les ressources externes ne sont pas chargées correctement.

Le fond de carte utilise CARTO Positron, avec l’attribution OpenStreetMap et CARTO intégrée.

## Remarque

Une correction JS a été appliquée dans `irigo_map.html` pour supprimer une faute de frappe (`frefre`) dans le script de gestion d'état du zoom/centre.

## Prochains passages depuis Proudhon

Le script `gtfs_rt_prouhon.py` affiche les prochains bus depuis l'arrêt Proudhon
en direction du centre-ville (arrêt Foch-Maison Bleue). Pour que les données
soient actualisées, il faut laisser ce script tourner : il récupère les horaires,
met à jour le rapport toutes les 10 secondes et ouvre la page dans le navigateur
au premier rafraîchissement.

```powershell
python gtfs_rt_prouhon.py
```

Le rapport est écrit dans `gtfs_rt_prouhon.html` et la page se recharge elle aussi
automatiquement toutes les 10 secondes. Ouvrir seulement le fichier HTML ne lance
pas la mise à jour des données. Arrêtez le script avec `Ctrl+C`.

## Application web mobile (React)

L'application React est conçue pour être hébergée sur Render et accessible depuis
un téléphone, y compris hors du réseau Wi-Fi local. Le serveur Python sert
l'interface et fournit les données GTFS par une API, avec une actualisation
automatique toutes les 10 secondes lorsque la page est ouverte. La compilation
génère un `index.html` autonome à la racine, pour que le déploiement n'ait pas
besoin du dossier `frontend`.

### Déploiement sur Render

#### Depuis un dépôt GitHub (Blueprint)

1. Compilez l'interface depuis le dossier du projet :

```powershell
cd frontend
npm.cmd ci
npm.cmd run build
cd ..
```

La compilation crée `index.html` à la racine du projet. Envoyez sur GitHub les
fichiers du projet, y compris `render.yaml`, puis choisissez **New +** →
**Blueprint** dans Render et connectez le dépôt.

#### Depuis un téléversement de fichiers sur Render

1. Effectuez la compilation ci-dessus sur le PC.
2. Dans le service Render, remplacez les fichiers téléversés par ces cinq
   fichiers, tous à la racine : `Dockerfile`, `requirements.txt`,
   `gtfs_rt_prouhon.py`, `web_app.py` et `index.html`.
3. Relancez le déploiement. Aucun dossier `frontend` n'est nécessaire : le
   fichier `index.html` généré contient déjà l'interface complète.

Le plan gratuit peut mettre l'application en veille après une période sans visite ;
le premier chargement qui suit peut prendre un peu plus de temps. Le PC personnel
n'a pas besoin de rester allumé après le déploiement.

### Lancer en local pour le développement

Construisez d'abord l'interface React avec Node.js installé :

```powershell
cd frontend
npm install
npm run build
cd ..
python -m pip install -r requirements.txt
python -m flask --app web_app run --host 0.0.0.0 --port 8000
```

Ouvrez `http://127.0.0.1:8000` sur le PC. Pour tester depuis le téléphone sur
le même Wi-Fi, ouvrez `http://ADRESSE-IP-DU-PC:8000` et autorisez le port dans
le pare-feu Windows si nécessaire.
