# doc-archiver — Archivage distribué multi-villes avec isolation par projet

API FastAPI d'archivage pur de documents (PDF, images, ou tout autre type), avec
stockage MinIO par ville, métadonnées PostgreSQL par ville, réplication
inter-sites automatique, et isolation multi-tenant par projet (bucket + compte
MinIO dédié par client).

**Rôle strict** : stocker des documents et les répliquer entre sites. Aucune
extraction, OCR, embedding ou déduplication — archivage pur.

---

## Sommaire

1. [Architecture générale](#architecture-générale)
2. [Prérequis](#prérequis)
3. [Déploiement local (Casa / Rabat sur minikube)](#déploiement-local-casa--rabat-sur-minikube)
4. [Déploiement du site distant (Fès sur Azure)](#déploiement-du-site-distant-fès-sur-azure)
5. [Tunnel VPN WireGuard entre les sites](#tunnel-vpn-wireguard-entre-les-sites)
6. [Système multi-projet (isolation par token)](#système-multi-projet-isolation-par-token)
7. [Créer un nouveau projet](#créer-un-nouveau-projet)
8. [Tests de bout en bout](#tests-de-bout-en-bout)
9. [Intégration Odoo](#intégration-odoo)
10. [Sécurité et bonnes pratiques](#sécurité-et-bonnes-pratiques)
11. [Dépannage](#dépannage)

---

## Architecture générale

Chaque ville est un **site physique indépendant** :

```
┌─────────────────────────┐        VPN WireGuard        ┌─────────────────────────┐
│   PC LOCAL (minikube)    │◄────────────────────────────►│   AZURE VM (k3s)         │
│                          │                              │                          │
│  namespace: ville-casa   │                              │  namespace: ville-fes    │
│    - postgres-casa       │                              │    - postgres-fes        │
│    - minio-casa (x4)     │                              │    - minio-fes (x4)      │
│                          │                              │                          │
│  namespace: ville-rabat  │                              │  namespace: doc-archiver │
│    - postgres-rabat      │                              │    - api (FastAPI)       │
│    - minio-rabat         │                              │                          │
│                          │                              │                          │
│  namespace: doc-archiver │                              │                          │
│    - api (FastAPI)       │                              │                          │
└─────────────────────────┘                              └─────────────────────────┘
```

- **Casa** et **Fès** : `siteType: CLUSTER` — MinIO en mode distribué (plusieurs
  replicas), pas de réplication obligatoire vers un autre site.
- **Rabat** : `siteType: SINGLE_SERVER` — un seul nœud MinIO, réplication
  automatique et obligatoire vers Fès (`backupOf: fes`) en cas de perte du
  serveur local.
- La communication inter-sites passe **uniquement en HTTP au-dessus du VPN
  WireGuard** — jamais de connexion PostgreSQL directe entre sites.

### Couche d'isolation par projet (nouveau)

Au-dessus de cette architecture par ville, chaque ville peut héberger
plusieurs **projets** (clients), chacun avec :
- son propre bucket MinIO (`documents-<projet>`)
- son propre compte IAM MinIO, restreint à ce bucket
- son propre token API

Un **token admin** distinct permet de voir tous les projets d'une ville, tous
buckets confondus — utile pour la supervision, jamais utilisé pour l'usage
métier normal.

```
Ville Casa
├── bucket "documents"            ← mode legacy (token global dev-secret-token)
├── bucket "documents-projeta"    ← token projet A uniquement
├── bucket "documents-projetb"    ← token projet B uniquement
└── (token admin voit les trois)
```

---

## Prérequis

- Docker Desktop (Windows/Mac/Linux)
- `minikube`, `kubectl`, `helm`
- `mc` (client MinIO) — [voir installation](#installer-mc-windows)
- Un compte Azure avec une VM Ubuntu pour le site distant
- WireGuard installé sur toutes les machines concernées

### Installer `mc` (Windows)

```powershell
New-Item -ItemType Directory -Path C:\mc -Force
Invoke-WebRequest -Uri "https://dl.min.io/client/mc/release/windows-amd64/mc.exe" -OutFile "C:\mc\mc.exe"
[Environment]::SetEnvironmentVariable("Path", $env:Path + ";C:\mc", "User")
# Fermer et rouvrir PowerShell, puis :
mc.exe --version
```

### Installer `mc` (Linux/Azure)

```bash
curl https://dl.min.io/client/mc/release/linux-amd64/mc -o mc
chmod +x mc
sudo mv mc /usr/local/bin/
```

---

## Déploiement local (Casa / Rabat sur minikube)

### 1. Démarrer minikube

Pour éviter d'avoir à relancer `minikube tunnel` à chaque session, mappez les
ports NodePort directement au démarrage :

```bash
minikube start --driver=docker --memory=7000 --cpus=4 \
  --ports=30800:30800,30900:30900,30901:30901,30432:30432,\
30902:30902,30903:30903,30433:30433,30904:30904,30905:30905,30434:30434
```

### 2. Builder l'image de l'API

```bash
minikube image build -t doc-archiver-api:latest .
```

### 3. Déployer

```bash
helm install archiver ./doc-archiver-chart \
  --set villes.casa.enabled=true \
  --set villes.rabat.enabled=true
```

Pour un déploiement déjà existant, utilisez `helm upgrade` plutôt que
`helm install` — et réaffirmez **tous** les `--set` déjà actifs, sinon Helm
peut désactiver silencieusement une ville non répétée :

```bash
helm upgrade archiver ./doc-archiver-chart \
  --set villes.casa.enabled=true \
  --set villes.rabat.enabled=true
```

### 4. Vérifier

```bash
kubectl get pods -A
kubectl get pvc -n ville-casa
kubectl get pvc -n ville-rabat
```

Si un PVC reste `Pending` avec l'erreur `pod has unbound immediate
PersistentVolumeClaims`, vérifiez le nom de la StorageClass disponible sur
votre cluster :

```bash
kubectl get storageclass
```

Sur k3s/minikube, c'est généralement `standard` ou `local-path`, pas toujours
celle par défaut du chart — adaptez `postgres.storageClassName` et
`minio.storageClassName` dans `values.yaml` en conséquence.

---

## Déploiement du site distant (Fès sur Azure)

### 1. Provisionner la VM

```bash
az group create -n rg-doc-archiver -l francecentral
az vm create \
  --resource-group rg-doc-archiver \
  --name vm-fes \
  --image Ubuntu2404 \
  --size Standard_B2s \
  --admin-username azureuser \
  --generate-ssh-keys
```

### 2. Installer Docker, k3s, Helm

```bash
ssh azureuser@<ip_publique_azure>

curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

curl -sfL https://get.k3s.io | sh -
sudo chmod 644 /etc/rancher/k3s/k3s.yaml
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> ~/.bashrc

sudo apt install git -y
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
```

### 3. Copier le code (sans Git, via `scp`)

Depuis votre PC local :

```bash
docker build -t doc-archiver-api:latest .
docker save doc-archiver-api:latest -o doc-archiver-api.tar

scp doc-archiver-api.tar azureuser@<ip_publique_azure>:~/
scp -r doc-archiver-chart azureuser@<ip_publique_azure>:~/
```

### 4. Importer l'image et déployer sur Azure

```bash
sudo k3s ctr images import ~/doc-archiver-api.tar

cd ~/doc-archiver-chart
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

helm install archiver-fes . \
  --set villes.fes.enabled=true \
  --set api.image.pullPolicy=Never
```

`pullPolicy=Never` est indispensable : sans ça, k3s tente de retélécharger
l'image depuis un registre externe au lieu d'utiliser celle importée
manuellement.

---

## Tunnel VPN WireGuard entre les sites

### Ouverture réseau nécessaire (une seule fois, côté administrateur réseau)

**Un seul port à faire ouvrir : UDP 51820**, sur le NSG Azure de la VM. Une
fois le tunnel WireGuard actif, tout le trafic applicatif (API, MinIO) passe
*à l'intérieur* de ce tunnel — aucun autre port NSG n'est nécessaire. Ne
jamais ouvrir les ports NodePort MinIO/Postgres directement sur le NSG : un
NodePort écoute par défaut sur `0.0.0.0`, donc l'ouvrir au niveau NSG
l'exposerait à tout Internet, pas seulement au VPN.

```bash
az network nsg rule create \
  --resource-group rg-doc-archiver \
  --nsg-name vm-fesNSG \
  --name allow-wireguard \
  --priority 400 \
  --destination-port-ranges 51820 \
  --protocol Udp \
  --access Allow
```

### Configuration WireGuard (les deux côtés)

```bash
sudo apt update && sudo apt install wireguard -y
sudo mkdir -p /etc/wireguard && sudo chmod 700 /etc/wireguard
sudo sh -c 'wg genkey | tee /etc/wireguard/privatekey | wg pubkey > /etc/wireguard/publickey'
sudo cat /etc/wireguard/privatekey
sudo cat /etc/wireguard/publickey
```

**Sur Azure (Fès)** — `/etc/wireguard/wg0.conf` :
```ini
[Interface]
Address = 10.10.0.1/24
ListenPort = 51820
PrivateKey = <privatekey_azure>

[Peer]
PublicKey = <publickey_local>
AllowedIPs = 10.10.0.2/32
```

**En local (Casa/Rabat)** — `/etc/wireguard/wg0.conf` :
```ini
[Interface]
Address = 10.10.0.2/24
PrivateKey = <privatekey_local>

[Peer]
PublicKey = <publickey_azure>
Endpoint = <IP_publique_azure>:51820
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
```

Activation :
```bash
sudo wg-quick up wg0
sudo systemctl enable wg-quick@wg0
```

Test croisé :
```bash
ping -c 3 10.10.0.1   # depuis local
ping -c 3 10.10.0.2   # depuis Azure
```

### Rendre les NodePort locaux joignables par Azure (Windows)

Avec `minikube --driver=docker`, les NodePort ne sont visibles que sur
`localhost` de la machine hôte, pas sur l'IP WireGuard. Il faut un pont, en
PowerShell **Administrateur** :

```powershell
$MINIKUBE_IP = minikube ip
$ports = 30800,30900,30901,30432,30902,30903,30433,30904,30905,30434
foreach ($p in $ports) {
    netsh interface portproxy add v4tov4 listenaddress=10.10.0.2 listenport=$p connectaddress=$MINIKUBE_IP connectport=$p
}
New-NetFirewallRule -DisplayName "doc-archiver ports" -Direction Inbound -Protocol TCP -LocalPort $ports -Action Allow
```

> ⚠️ **Limite connue** : Kubernetes réserve toute la plage de ports
> 30000-32767 sur les IPs locales, y compris l'IP WireGuard. Si un service
> MinIO NodePort dans cette plage ne répond pas via le VPN malgré une
> configuration correcte, utilisez un tunnel SSH ou un pont `socat`/`portproxy`
> sur un port **hors** de cette plage (ex: 39900+) plutôt que le NodePort brut.
> Voir [Dépannage](#dépannage).

---

## Système multi-projet (isolation par token)

### Principe

- Un **token = un projet = un bucket + un compte MinIO dédié**.
- L'API ne fait jamais confiance à un paramètre fourni par l'appelant pour
  choisir le bucket — le token, résolu côté serveur, détermine seul le bucket
  utilisé.
- Un **token admin** distinct voit tous les projets d'une ville.
- L'ancien token global (`API_TOKEN`) continue de fonctionner en mode
  *legacy*, sur le bucket unique `documents` — aucune régression pour les
  déploiements qui n'ont pas encore migré.

### Configuration (`values.yaml`)

```yaml
api:
  apiToken: dev-secret-token          # mode legacy, inchangé
  adminToken: "admin-secret-a-changer" # nouveau — voit tous les projets
  projectsJson: '{"tok_projetA_xxx":{"name":"ProjetA","bucket":"documents-projeta","access_key":"adminprojeta","secret_key":"SecretProjetA"}}'
```

Voir [`values.yaml.example`](./values.yaml.example) pour un exemple complet
avec deux projets.

### Endpoint `/whoami`

Permet de vérifier à quel projet un token correspond, sans rien modifier :

```bash
curl http://localhost:30800/whoami -H "Authorization: Bearer <token>"
```

Réponses possibles :
```json
{"mode": "project", "project_name": "ProjetA"}
{"mode": "admin",   "project_name": "Administrateur (tous projets)"}
{"mode": "legacy",  "project_name": null}
```

---

## Créer un nouveau projet

### 1. Créer le bucket et le compte MinIO

```powershell
mc.exe mb minio-casa/documents-<projet>
mc.exe admin user add minio-casa admin<projet> <secret>

$policyJson = @"
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:*"],
    "Resource": ["arn:aws:s3:::documents-<projet>", "arn:aws:s3:::documents-<projet>/*"]
  }]
}
"@
[System.IO.File]::WriteAllText("$env:TEMP\<projet>-policy.json", $policyJson)

mc.exe admin policy create minio-casa <projet>-policy "$env:TEMP\<projet>-policy.json"
mc.exe admin policy attach minio-casa <projet>-policy --user admin<projet>
```

> ⚠️ Toujours utiliser `[System.IO.File]::WriteAllText(...)`, jamais
> `Out-File -Encoding utf8`, qui ajoute un BOM invisible que le parseur JSON
> de MinIO refuse (`invalid character 'ï'`).

### 2. Fonction PowerShell d'automatisation

```powershell
function New-ArchiverProject {
    param(
        [string]$ProjectName,
        [string]$AliasName = "minio-casa",
        [string]$AccessKey,
        [string]$SecretKey
    )
    $bucket = "documents-$ProjectName"
    mc.exe mb "$AliasName/$bucket"
    mc.exe admin user add $AliasName $AccessKey $SecretKey

    $policyJson = @"
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:*"],
    "Resource": ["arn:aws:s3:::$bucket", "arn:aws:s3:::$bucket/*"]
  }]
}
"@
    $policyPath = "$env:TEMP\$ProjectName-policy.json"
    [System.IO.File]::WriteAllText($policyPath, $policyJson)
    mc.exe admin policy create $AliasName "$ProjectName-policy" $policyPath
    mc.exe admin policy attach $AliasName "$ProjectName-policy" --user $AccessKey

    $token = "tok_$($ProjectName)_$([guid]::NewGuid().ToString('N').Substring(0,8))"
    $fragment = "`"$token`":{`"name`":`"$ProjectName`",`"bucket`":`"$bucket`",`"access_key`":`"$AccessKey`",`"secret_key`":`"$SecretKey`"}"

    Write-Host "`nProjet '$ProjectName' créé."
    Write-Host "Fragment à ajouter dans projectsJson :"
    Write-Host $fragment
    $fragment | Set-Clipboard
    Write-Host "`n(Copié dans le presse-papiers — Ctrl+V pour coller.)"

    return $fragment
}
```

Usage :
```powershell
New-ArchiverProject -ProjectName "projetc" -AccessKey "adminprojetc" -SecretKey "SecretProjetC789"
```

### 3. Ajouter le fragment dans `values.yaml` et redéployer

```bash
helm upgrade archiver ./doc-archiver-chart \
  --set villes.casa.enabled=true \
  --set villes.rabat.enabled=true
```

> La création de projet reste **volontairement manuelle** (pas de bouton
> "créer un projet" côté Odoo) — un geste conscient avant modification de la
> configuration vivante, plutôt qu'une automatisation qui pourrait créer des
> ressources sans supervision.

---

## Tests de bout en bout

### Suite de 5 tests d'isolement

```powershell
# 1. Upload dans chaque projet
curl.exe -X POST "http://localhost:30800/documents?ville=casa" -H "Authorization: Bearer <token_a>" -F "file=@test_a.pdf"
curl.exe -X POST "http://localhost:30800/documents?ville=casa" -H "Authorization: Bearer <token_b>" -F "file=@test_b.pdf"

# 2. Isolement — chaque projet ne voit que ses documents
curl.exe "http://localhost:30800/documents?ville=casa" -H "Authorization: Bearer <token_a>"
curl.exe "http://localhost:30800/documents?ville=casa" -H "Authorization: Bearer <token_b>"

# 3. Le test clé — accès croisé par ID deviné, doit renvoyer 403
curl.exe -i "http://localhost:30800/documents/<id_doc_b>?ville=casa" -H "Authorization: Bearer <token_a>"

# 4. Admin voit tout
curl.exe "http://localhost:30800/documents?ville=casa" -H "Authorization: Bearer <admin_token>"

# 5. Non-régression — mode legacy toujours fonctionnel
curl.exe -X POST "http://localhost:30800/documents?ville=casa" -H "Authorization: Bearer dev-secret-token" -F "file=@test_legacy.pdf"
```

### Vérification MinIO directe

```powershell
mc.exe ls minio-casa/documents-projeta/
mc.exe ls minio-casa/documents-projetb/
mc.exe ls minio-casa/documents/
```

### Test de la propagation de suppression (Rabat → Fès)

```bash
# Upload sur Rabat (SINGLE_SERVER, backup_of=fes) et attendre la réplication
curl -X POST "http://10.10.0.2:30800/documents?ville=rabat" \
  -H "Authorization: Bearer dev-secret-token" -F "file=@test.pdf"

# Suppression sur Rabat
curl -X DELETE "http://10.10.0.2:30800/documents/<id>?ville=rabat&hard=true" \
  -H "Authorization: Bearer dev-secret-token"

# Vérifier que la copie de secours a bien disparu sur Fès
curl "http://10.10.0.1:30800/documents?ville=fes&include_deleted=true" \
  -H "Authorization: Bearer dev-secret-token"
```

### Automatisation avec Postman/Newman

Exportez la collection Postman contenant les 5 tests, puis rejouez-la en
ligne de commande après chaque déploiement :

```powershell
npm install -g newman
newman run "collection.json" -e "environment.json"
```

---

## Intégration Odoo

Le module `doc_archiver_storage` intercepte le stockage des pièces jointes
Odoo (`ir.attachment`) et les redirige vers l'API, sans dupliquer de logique
métier.

### Configuration d'un serveur (`archive.storage.server`)

| Champ | Description |
|---|---|
| API URL | URL du déploiement API pour cette ville |
| City | Ville cible (`fes`, `casa`, `rabat`) |
| API Token | Token du projet (ou token legacy en transition) |
| Project Name | Lecture seule, résolu automatiquement via `/whoami` |

> Le champ **Backup City** a été retiré : la réplication inter-sites est
> désormais entièrement pilotée côté serveur (`{VILLE}_BACKUP_OF` dans le
> Helm chart), pas au niveau d'un enregistrement Odoo individuel.

### Bouton "Tester la connexion"

Appelle `/whoami` et affiche le nom du projet résolu — confirme que le token
saisi correspond bien au projet attendu avant tout usage réel.

---

## Sécurité et bonnes pratiques

- **Ne jamais committer `values.yaml`** (contient les tokens et secrets
  MinIO). Ajoutez-le à `.gitignore` :
  ```bash
  echo "values.yaml" >> .gitignore
  echo "values-projects.yaml" >> .gitignore
  ```
- Vérifiez qu'aucune version antérieure n'a été committée par erreur :
  ```bash
  git log --all --full-history -- "*/values.yaml"
  ```
- Le token admin ne doit être connu que d'un nombre restreint de personnes —
  ne pas le stocker dans un champ Odoo visible par plusieurs utilisateurs.
- Ne jamais ouvrir de NodePort MinIO/Postgres directement sur un NSG/firewall
  public — seul le port VPN (UDP 51820) doit être exposé.
- Utilisez `ufw` en complément du VPN pour restreindre l'accès aux NodePort à
  la seule interface `wg0` :
  ```bash
  sudo ufw allow in on wg0 to any port 30800
  sudo ufw deny 30800/tcp
  ```

---

## Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| `PVC pending — unbound immediate PersistentVolumeClaims` | Mauvais nom de StorageClass | Vérifier `kubectl get storageclass`, ajuster `values.yaml` |
| `pod has Insufficient cpu` | Trop de replicas MinIO pour la VM | Réduire `resources.requests.cpu` ou le nombre de replicas |
| NodePort injoignable via VPN (curl local OK, curl VPN KO) | Port dans la plage 30000-32767 réservée par Kubernetes | Utiliser un pont `socat`/`portproxy` sur un port hors plage (ex: 39900) |
| `invalid character 'ï'` lors de `mc admin policy create` | BOM ajouté par `Out-File -Encoding utf8` | Utiliser `[System.IO.File]::WriteAllText(...)` |
| Token projet renvoie 401 malgré une config correcte | `access_key`/`secret_key` ne correspondent pas exactement entre MinIO et `projectsJson` | Vérifier caractère par caractère les deux valeurs |
| Suppression sur un site ne se propage pas au site de secours | Ancienne version du code, sans la propagation par `archive_path` | Vérifier que `schedule_backup_deletion` est bien appelé dans `delete_document_endpoint` |
