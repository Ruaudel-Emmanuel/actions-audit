# actions-audit

Audit **mensuel** des GitHub Actions **non épinglées en SHA-40** dans tous les dépôts actifs.

⚠️ Pourquoi : un `uses: actions/checkout@v4` suit le tag — si le tag est compromis
ou déplacé (ex. incident tj-actions 2025), le code exécuté sur le runner est compromis.
Un `uses: actions/checkout@<sha40>` est immuable.

## 🧭 Principe

`audit_actions.py` scanne tous les `.github/workflows/*.yml` de chaque dépôt actif :

| `uses:` | verdict |
|---|---|
| `owner/repo@<40 hex>` | ✅ épinglé |
| `owner/repo@vX` / `@branche` / sans `@` | ❌ non épinglé — le script résout le **SHA actuel** du ref (via `git ls-remote`) pour faciliter le correctif |
| `docker://image:tag` | ⚠️ image (épinglable par digest `@sha256:...`) |
| `./.github/actions/...` | ✅ action locale |

Sorties : Telegram + `/var/log/actions-audit-last.txt` / `.json` (root 600). Exit 1 si non épinglées.

## 🚀 Utilisation manuelle

```bash
sudo python3 /home/ubuntu/projects/actions-audit/audit_actions.py              # tous les dépôts
sudo python3 /home/ubuntu/projects/actions-audit/audit_actions.py --repo NOM   # test 1 dépôt
```

## ⏰ Cron (VPS)

- `actions-audit.timer` : **le 1ᵉʳ de chaque mois à 06h00 Europe/Paris**, `Persistent=true`
- `actions-audit.service` : oneshot, exécute `audit_actions.py`

Vérification : `systemctl list-timers actions-audit.timer` · `journalctl -u actions-audit.service` · `cat /var/log/actions-audit-last.txt`

## 🔧 Correctif

Remplacer `owner/repo@tag` par `owner/repo@<sha40>` (le SHA est fourni dans le rapport).
Peut être automatisé en PR par dépôt (comme le nœud deps) si besoin.

## 🔒 Sécurité

Aucun secret commité. PAT (`Contents: Read`) lu dans `/home/ubuntu/.env` sur le VPS.
