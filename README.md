# CamRec : enregistrement des caméras du studio

Interface web pour enregistrer les flux des caméras PTZ (RTMP ou RTSP) sur un Raspberry Pi 5.
Les flux sont copiés tels quels (`ffmpeg -c copy`, pas de réencodage) en fichiers MP4 de 30 min.

## Installation sur le Pi

```bash
# Docker (si absent)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # puis se déconnecter / reconnecter

# Application
mkdir -p ~/camrec && cd ~/camrec      # copier ici : app.py, index.html, Dockerfile, docker-compose.yml
mkdir -p config recordings            # créés par toi, sinon Docker les crée en root
docker compose up -d --build
```

Interface : `http://<ip-du-pi>:8090`

Les fichiers appartiennent à l'utilisateur `1000:1000` (le premier compte du Pi). Si ton `id -u` est différent :
`PUID=$(id -u) PGID=$(id -g) docker compose up -d --build`.

## Disques

L'interface liste les points de montage sous `/media`, `/mnt` et `/recordings` avec leur espace libre.

- **SSD USB ou NVMe** : il doit être monté sous `/mnt/...` ou `/media/...` sur le Pi. Pour un montage permanent :
  ```bash
  sudo mkdir -p /mnt/ssd
  sudo blkid                                  # repérer l'UUID du disque
  echo 'UUID=xxxx  /mnt/ssd  ext4  defaults,nofail  0  2' | sudo tee -a /etc/fstab
  sudo mount -a && sudo chown $USER: /mnt/ssd
  ```
  Avec un disque exFAT/NTFS, ajouter `uid=1000,gid=1000` aux options du montage.
- **Clé ou disque branché à chaud** : il apparaît après « Rafraîchir les disques » s'il est monté sous `/media` (`rslave` dans le compose).
- **NAS** : monter le partage SMB/NFS sous `/mnt/nas` sur le Pi, il apparaîtra dans la liste.
- `/recordings` = dossier `./recordings` à côté du compose, sur la carte SD : à éviter pour un spectacle entier.

Chaque enregistrement crée `<disque>/<projet>/cam1_000.mp4, cam1_001.mp4…` plus un `cam1.log` (erreurs ffmpeg).
Relancer un enregistrement dans le même projet continue la numérotation sans rien écraser.

## Fonctionnement

- **Caméras** : cocher celles à enregistrer. Clic sur l'aperçu = image de la caméra. Pendant l'enregistrement, l'aperçu se met à jour toutes les 5 s (pris dans le flux enregistré, sans deuxième connexion à la caméra).
- **Coupure réseau** : si un flux tombe, ffmpeg est relancé automatiquement et un nouveau fichier commence ; l'état passe à « reconnexion… ».
- **Arrêt** : les fichiers MP4 sont finalisés proprement (arrêt via l'UI, l'API, ou `docker compose stop`, avec 30 s de délai).
- **Réglages des caméras** : nom et URL modifiables dans l'interface (sauvés dans `config/config.json`).

## API (pour Companion)

| Méthode | URL | Effet |
|---|---|---|
| POST | `/api/start` | Démarre avec les derniers réglages (caméras, disque, projet). Corps JSON facultatif : `{"cams":["cam1","cam3"],"dest":"/mnt/ssd","project":"Spectacle","segment_time":1800}` |
| POST | `/api/stop` | Arrête tout et finalise les fichiers |
| GET | `/api/status` | État : `recording` (true/false), caméras, taille, espace libre |
| GET | `/api/locations` | Disques disponibles |

Companion : module « Generic HTTP », action POST sur `http://<ip-du-pi>:8090/api/start`.
Attention : relancer avec un projet déjà utilisé reprend le même dossier ; pour un nouveau dossier par spectacle, envoyer `"project": ""` (nom automatique `rec_date_heure`).

## Limites

- Pas d'authentification : à garder sur le réseau du studio.
- Le CPU n'est pas le facteur limitant (copie sans réencodage) ; c'est le débit disque et réseau. Ethernet obligatoire.
