#!/usr/bin/env python3
"""
audit_actions.py — Audit MENSUEL des GitHub Actions non épinglées en SHA-40.

Pour chaque dépôt actif, scanne tous les .github/workflows/*.yml et liste les `uses:` :
  - owner/repo@<sha40>        -> ✅ épinglé
  - owner/repo@<tag/branche>  -> ❌ NON épinglé (résout le SHA actuel du ref pour faciliter le fix)
  - owner/repo (sans @)       -> ❌ NON épinglé (branche par défaut)
  - docker://img:tag          -> ⚠️ image (épinglable par digest @sha256:...)
  - ./.github/actions/...     -> ✅ action locale

Sortie : stdout + /var/log/actions-audit-last.txt + .json + Telegram.
Exit 1 si au moins une action non épinglée.

Usage :
  python3 audit_actions.py                # tous les dépôts actifs
  python3 audit_actions.py --repo NOM     # un seul dépôt (test)
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

import yaml

API = "https://api.github.com"
ENV_PAT = "/home/ubuntu/.env"
ENV_TG = "/etc/vps-watchdog-telegram.env"
LOG_TXT = "/var/log/actions-audit-last.txt"
LOG_JSON = "/var/log/actions-audit-last.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def get_pat():
    with open(ENV_PAT) as f:
        for line in f:
            if line.startswith("GITHUB_PAT="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"GITHUB_PAT introuvable dans {ENV_PAT}")


def api(method, url, pat, raw=False):
    req = urllib.request.Request(url, method=method,
        headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            if raw:
                return r.status, data.decode("utf-8", "replace")
            return r.status, (json.loads(data) if data else {})
    except urllib.error.HTTPError as e:
        if raw:
            return e.code, ""
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def list_workflow_files(full, pat):
    st, tree = api("GET", f"{API}/repos/{full}/git/trees/HEAD?recursive=1", pat)
    if st != 200:
        return []
    return [t["path"] for t in tree.get("tree", [])
            if t["path"].startswith(".github/workflows/") and t["path"].endswith((".yml", ".yaml"))]


def find_uses(txt):
    """Retourne [(where, uses), ...] depuis le YAML du workflow."""
    try:
        doc = yaml.safe_load(txt)
    except Exception:
        return []
    out = []
    jobs = (doc or {}).get("jobs") or {}
    for jname, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if "uses" in job:  # reusable workflow
            out.append((f"job « {jname} » (reusable)", str(job["uses"])))
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "uses" in step:
                where = f"job « {jname} » step « {step.get('name') or step.get('id') or '?'} »"
                out.append((where, str(step["uses"])))
    return out


def resolve_ref_sha(owner_repo, ref):
    """SHA du commit pointé par un tag/branche via ls-remote (anonyme, repos publics)."""
    url = f"https://github.com/{owner_repo}.git"
    for pattern in (f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", f"refs/heads/{ref}"):
        r = subprocess.run(["git", "ls-remote", url, pattern],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.split()[0]
    return None


def classify(uses):
    """Retourne (status, owner_repo, ref, resolved_sha).
    status: ok | local | docker | unpinned"""
    if uses.startswith("./"):
        return "local", None, None, None
    if uses.startswith("docker://"):
        return "docker", None, None, None
    ref = None
    body = uses
    if "@" in uses:
        body, ref = uses.rsplit("@", 1)
    owner_repo = body.split("/")[0] + "/" + body.split("/")[1] if "/" in body else body
    if ref and SHA_RE.match(ref):
        return "ok", owner_repo, ref, ref
    if not ref:
        ref = "HEAD"
    sha = resolve_ref_sha(owner_repo, ref) if ref != "HEAD" else resolve_ref_sha(owner_repo, "HEAD")
    return "unpinned", owner_repo, ref, sha


def send_telegram(text):
    try:
        with open(ENV_TG) as f:
            env = dict(l.strip().split("=", 1) for l in f if "=" in l)
        token, chat = env["TG_BOT_TOKEN"].strip('"'), env["TG_CHAT_ID"].strip('"')
        body = json.dumps({"chat_id": chat, "text": text[:4000]}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=body, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:
        print(f"(telegram non envoyé: {e})", file=sys.stderr)
        return False


def main():
    os.umask(0o077)
    pat = get_pat()
    only_repo = sys.argv[sys.argv.index("--repo") + 1] if "--repo" in sys.argv else None

    st, repos = api("GET", f"{API}/user/repos?per_page=100&type=all", pat)
    repos = [r["full_name"] for r in repos if not r.get("archived")]
    if only_repo:
        repos = [r for r in repos if r.endswith("/" + only_repo)]

    report = {}       # repo -> [findings]
    n_files = 0
    for full in repos:
        files = list_workflow_files(full, pat)
        for wf in files:
            n_files += 1
            st, txt = api("GET", f"{API}/repos/{full}/contents/{wf}", pat, raw=True)
            if st != 200:
                continue
            for where, uses in find_uses(txt):
                status, owner_repo, ref, sha = classify(uses)
                if status in ("ok", "local"):
                    continue
                report.setdefault(full, []).append({
                    "file": wf, "where": where, "uses": uses,
                    "status": status, "owner_repo": owner_repo, "ref": ref, "sha": sha})
        if full in report:
            print(f"  ⚠️  {full}: {len(report[full])} action(s) non épinglée(s)")

    n_unpinned = sum(1 for v in report.values() for f in v if f["status"] == "unpinned")
    n_docker = sum(1 for v in report.values() for f in v if f["status"] == "docker")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = (f"📌 Audit Actions non épinglées en SHA — {now}\n"
              f"{len(repos)} dépôts · {n_files} fichiers workflows scannés · "
              f"❌ {n_unpinned} non épinglées · ⚠️ {n_docker} images docker")
    lines = []
    for full, findings in report.items():
        for f in findings:
            fix = f"  -> épingler: {f['owner_repo']}@{f['sha']}" if f["sha"] else ""
            lines.append(f"  ❌ {full} · {f['file']}\n     {f['uses']}{fix}")
    body = "\n".join(lines[:40]) if lines else "Toutes les actions sont épinglées en SHA 🎉"
    report_txt = header + "\n" + body + ("\n… (tronqué)" if len(lines) > 40 else "")
    print(report_txt)

    with open(LOG_TXT, "w") as f:
        f.write(report_txt + "\n")
    with open(LOG_JSON, "w") as f:
        json.dump({"date": now, "repos": len(repos), "files": n_files, "findings": report}, f, ensure_ascii=False, indent=1)
    os.chmod(LOG_TXT, 0o600)
    os.chmod(LOG_JSON, 0o600)
    send_telegram(report_txt)
    sys.exit(1 if n_unpinned else 0)


if __name__ == "__main__":
    main()
