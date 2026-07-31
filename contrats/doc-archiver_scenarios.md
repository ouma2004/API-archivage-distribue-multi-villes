## Scenarios de test — doc-archiver

### Cross-cutting — auth & validation ville (mutualisé, applicable a tous les endpoints authentifies)
| # | Scenario | Requete (methode/chemin/auth/corps) | Resultat attendu | Reference code |
|---|----------|--------------------------------------|------------------|----------------|
| 1 | Aucun header Authorization / token inconnu | GET /documents?ville=Paris, sans header ou Bearer bidon | 401 | verify_token — s'applique identiquement a POST/GET/PATCH/DELETE /documents{,/{id}} et GET /file |
| 2 | Ville hors VILLES_VALIDES | GET /documents?ville=Atlantis, token project valide | 400 | verify_ville — s'applique identiquement partout ou `ville` (ou `backup_ville`) est un parametre |

### GET /health
| # | Scenario | Requete | Resultat attendu | Reference code |
|---|----------|---------|-------------------|-----------------|
| 3 | Ping nominal sans auth | GET /health, aucun header | 200, body contient villes actives + version | health |

### GET /whoami
| # | Scenario | Requete | Resultat attendu | Reference code |
|---|----------|---------|-------------------|-----------------|
| 4 | Identification des 3 modes valides | GET /whoami avec token project, puis admin, puis legacy (3 sous-appels) | 200 a chaque fois, `mode` = project/admin/legacy respectivement (+ project_name pour project) | whoami |
| 5 | Token totalement inconnu | GET /whoami, Bearer "xxx-random" | 401 | verify_token |
| 6 | Token inter-site (INTER_SITE_TOKEN) utilise ici | GET /whoami, Bearer = INTER_SITE_TOKEN | 401 (aucun mode inter_site reconnu par verify_token) | verify_token — needs_human_check |

### POST /documents
| # | Scenario | Requete | Resultat attendu | Reference code |
|---|----------|---------|-------------------|-----------------|
| 7 | Upload nominal par un projet | POST /documents?ville=Paris, token project A, multipart files=[fichier valide] | 200, `uploaded` non vide, document cree avec project_bucket = bucket de A | upload_document / _process_single_upload |
| 8 | Upload nominal admin/legacy (pas de bucket projet) | POST /documents?ville=Paris, token admin puis token legacy, meme fichier | 200 dans les deux cas, document cree sans project_bucket (bucket 'documents') | upload_document — isolation_rule |
| 9 | Champ requis manquant | (a) POST sans champ `files` ; (b) POST sans query `ville` | 422 dans les deux cas | validation FastAPI |

### GET /documents
| # | Scenario | Requete | Resultat attendu | Reference code |
|---|----------|---------|-------------------|-----------------|
| 10 | Listing nominal | GET /documents?ville=Paris, token project A | 200, liste (eventuellement vide) des docs du bucket de A | list_documents |
| 11 | Parametres invalides | (a) `limit=abc` ; (b) `include_deleted=peutetre` | 422 dans les deux cas | validation FastAPI |
| 12 | Isolation par filtrage — projet A ne voit pas les docs de B | Upload d'un doc par B dans Paris, puis GET /documents?ville=Paris avec token A | 200, liste de A NE contient PAS le doc de B (silencieux, pas de 403) | _bucket_filter / get_documents |
| 13 | Isolation admin (ALL) vs legacy (bucket NULL uniquement) | GET /documents?ville=Paris avec token admin, puis avec token legacy | 200 admin: contient les docs de A **et** B ; 200 legacy: contient uniquement les docs sans project_bucket | _bucket_filter |

### GET /documents/{doc_id}
| # | Scenario | Requete | Resultat attendu | Reference code |
|---|----------|---------|-------------------|-----------------|
| 14 | Telechargement nominal | GET /documents/{id}?ville=Paris, token project A sur son propre doc | 200, stream du fichier | get_document |
| 15 | Isolation — acces cross-projet refuse | GET /documents/{id_de_B}?ville=Paris, token project A | 403 | _check_ownership |
| 16 | Isolation — admin bypasse l'ownership | GET /documents/{id_de_B}?ville=Paris, token admin | 200 (admin traduit vers credentials root + bucket reel) | _effective_project_for_file_access |
| 17 | Document inconnu | GET /documents/doc-inexistant?ville=Paris, token valide | 404 | get_document_full |
| 18 | Document soft-deleted (meme projet) | GET /documents/{id}?ville=Paris apres DELETE soft, token du proprietaire | 410 | archive_state == DELETED |
| 19 | Site primaire indisponible, pas de backup exploitable | GET /documents/{id}?ville=Paris, backend MinIO primaire simule down, pas de backup SYNCED | 503 | get_document (fallback) |

### PATCH /documents/{doc_id}
| # | Scenario | Requete | Resultat attendu | Reference code |
|---|----------|---------|-------------------|-----------------|
| 20 | Renommage nominal | PATCH /documents/{id}?ville=Paris, body {"filename":"nouveau.pdf"}, token proprietaire | 200 {document_id, filename} | rename_document_endpoint |
| 21 | Isolation — renommage cross-projet refuse | PATCH /documents/{id_de_B}?ville=Paris, token project A | 403 | _check_ownership |
| 22 | Corps invalide | (a) filename="   " (blanc apres strip) -> 400 ; (b) doc_id inexistant -> 404 | 400 / 404 selon le cas | rename_document_endpoint / rename_document |

### DELETE /documents/{doc_id}
| # | Scenario | Requete | Resultat attendu | Reference code |
|---|----------|---------|-------------------|-----------------|
| 23 | Soft-delete nominal | DELETE /documents/{id}?ville=Paris, token proprietaire (hard omis) | 200 {document_id, archive_state: DELETED} | soft_delete_document |
| 24 | Isolation — suppression cross-projet refusee | DELETE /documents/{id_de_B}?ville=Paris, token project A | 403 | _check_ownership |
| 25 | Hard-delete nominal | DELETE /documents/{id}?ville=Paris&hard=true, token proprietaire, sur un doc non supprime | 200 {document_id, archive_state: HARD_DELETED}, objet absent de MinIO ensuite | delete_document / _effective_project_for_file_access |

### GET /file
| # | Scenario | Requete | Resultat attendu | Reference code |
|---|----------|---------|-------------------|-----------------|
| 26 | Lecture nominale par chemin brut | GET /file?ville=Paris&archive_path=<chemin valide du bucket de A>, token project A | 200, stream du fichier | get_file_by_path |
| 27 | Chemin inexistant/errone | GET /file?ville=Paris&archive_path=chemin/qui/nexiste/pas, token valide | 503 (exception generique, pas de 404 dedie) | get_file_by_path |

### 🔍 Scenarios non evidents (deduits)
| # | Scenario | Pourquoi c'est un risque | Resultat attendu |
|---|----------|--------------------------|------------------|
| 28 | Upload multi-fichiers : 1 fichier vide + 1 fichier valide dans le meme appel POST /documents | Le code renvoie 200 des qu'un seul fichier reussit ; un client pourrait ignorer les erreurs partielles silencieuses | 200, `uploaded` non vide ET `errors` non vide simultanement — a valider explicitement dans les assertions |
| 29 | DELETE hard=false vs hard=true sur un document deja DELETED | Asymetrie : hard=false renvoie 404 (deja supprime), hard=true reussit silencieusement sans 404 prealable | hard=false -> 404 ; hard=true sur le meme doc -> 200 HARD_DELETED (aucune erreur) |
| 30 | PATCH vs GET sur le meme document soft-deleted | Incoherence de code de statut pour le meme etat (`archive_state=DELETED`) entre deux endpoints | PATCH -> 404 ("introuvable ou supprime") ; GET -> 410 — a documenter comme divergence assumee ou bug |
| 31 | GET /documents/{doc_id} sur un doc soft-deleted appartenant a un AUTRE projet | L'ordre des checks (404 -> 403 -> 410) fait qu'un tiers reçoit 403 et non 410 : ne revele pas si le doc existe/est supprime | 403 (pas 410) — confirmer que c'est un choix de securite volontaire |
| 32 | GET /file avec un archive_path connu d'un document soft-deleted (meme projet) | /file ne consulte jamais la table documents (pas de check archive_state) : un doc "supprime" reste telechargeable si le chemin exact est connu | 200 — faille fonctionnelle potentielle a confirmer/corriger |
| 33 | GET /file avec un token ADMIN | L'identite `{"is_admin": True}` est passee brute a `_resolve_bucket`, qui attend un dict avec 'bucket'/'access_key'/'secret_key' -> KeyError probable | 503 attendu (capture generique) — confirmer si /file doit reellement supporter l'admin |

### ⚠️ Points bloquants avant test
- **Jeu de donnees prealable requis** : au moins 2 projets configures dans PROJECTS_JSON (A et B) avec buckets distincts, 1 admin token, 1 legacy token (API_TOKEN), et pour /whoami un token totalement inconnu + le INTER_SITE_TOKEN — tous a preparer avant execution.
- **Documents de reference** : pour chaque scenario d'isolation (403 cross-projet, filtrage GET /documents, GET /file), il faut un document deja uploade par B (et son `doc_id`/`archive_path` exacts) connu du testeur pour simuler une tentative d'acces par A.
- **Simulation de panne site primaire** (scenario 19, GET /documents/{doc_id} -> 503) : necessite de pouvoir couper/bloquer l'acces MinIO du site primaire pendant le test (config ou mock reseau) — a clarifier avec l'equipe infra avant execution.
- **Ambiguites a lever avec l'equipe (cf. `needs_human_check` du contrat)** avant de figer les assertions attendues : comportement de /whoami avec token inter_site, succes partiel POST /documents, ordre 403/410, asymetrie hard/soft delete, incoherence PATCH/GET sur doc supprime, admin sur /file, absence de check archive_state sur /file.
- **`download_url` potentiellement null** (presigned URL) : si testable, prevoir un scenario ou generate_presigned_url echoue (ex: config MinIO invalide) pour verifier que l'API degrade proprement (download_url=null) sans casser le 200 — non compte dans le total faute de moyen simple de le provoquer en test boite noire.
- Le total ci-dessous (34) depasse legerement le plafond de ~30 : plusieurs scenarios generiques (401/400 via verify_token/verify_ville) ont ete mutualises en tete de document pour eviter de les repeter sur les 6 endpoints qui en dependent — sans cette mutualisation le total aurait depasse 45.

**Total scenarios : 34** (2 cross-cutting + 25 par endpoint + 6 non evidents... soit 27 dans les tables endpoint + 2 cross-cutting + 6 non evidents = 33 lignes numerotees, arrondi a 34 avec la variante (a)/(b) du #22).