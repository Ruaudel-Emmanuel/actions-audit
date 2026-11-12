#!/usr/bin/env python3
"""
fix_pins.py — Pousse les corrections d'épinglage SHA depuis le dernier rapport d'audit
(/var/log/actions-audit-last.json, produit par audit_actions.py).

Pour chaque finding « unpinned » avec un SHA résolu :
  - remplace `uses: owner/repo@ref` par `uses: owner/repo@<sha40>` dans le fichier concerné
  - push direct sur main, ou branche chore/pin-actions-sha + PR si main est protégé

Rapport Telegram. Usage : sudo python3 fix_pins.py [--repo NOM] [--dry-run]
"""
import base64
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

API = "https://api.github.com"
ENV_PAT = "/home/ubuntu/.env"
ENV_TG = "/etc/vps-watchdog-telegram.env"
REPORT_JSON = "/var/log/actions-audit-last.json"
BRANCH = "chore/pin-actions-sha"


def get_pat():
    with open(ENV_PAT) as f:
        for line in f:
            if line.startswith("GITHUB_PAT="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"GITHUB_PAT introuvable dans {ENV_PAT}")


def api(method, url, pat, body=None, raw=False):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {pat}",
                 "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
                 "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            if raw:
                return r.status, data.decode("utf-8", "replace")
            return r.status, (json.loads(data) if data else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


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


def push_file(repo, wf, new_txt, pat, dry):
    """Push le fichier modifié sur main, ou branche+PR si protégé. Retourne (statut, info)."""
    st, cur = api("GET", f"{API}/repos/{repo}/contents/{wf}?ref=main", pat)
    if st != 200:
        return "error", f"GET main: HTTP {st}"
    sha = cur["sha"]
    content_b64 = base64.b64encode(new_txt.encode()).decode()
    if dry:
        return "dry", None
    st, res = api("PUT", f"{API}/repos/{repo}/contents/{wf}", pat,
                  {"message": f"fix: épingle les actions en SHA-40 ({wf}) [skip ci]",
                   "content": content_b64, "branch": "main", "sha": sha})
    if st in (200, 201):
        return "pushed", None
    # main protégé -> branche + PR
    st, ref = api("GET", f"{API}/repos/{repo}/git/ref/heads/main", pat)
    if st != 200:
        return "error", f"GET ref main: HTTP {st}"
    api("POST", f"{API}/repos/{repo}/git/refs", pat, {"ref": f"refs/heads/{BRANCH}", "sha": ref["object"]["sha"]})
    st, curb = api("GET", f"{API}/repos/{repo}/contents/{wf}?ref={BRANCH}", pat)
    body = {"message": f"fix: épingle les actions en SHA-40 ({wf}) [skip ci]",
            "content": content_b64, "branch": BRANCH}
    if st == 200:
        body["sha"] = curb["sha"]
    st, _ = api("PUT", f"{API}/repos/{repo}/contents/{wf}", pat, body)
    if st not in (200, 201):
        return "error", f"PUT branche: HTTP {st}"
    st, pr = api("POST", f"{API}/repos/{repo}/pulls", pat,
                 {"title": "fix: épingle les GitHub Actions en SHA-40",
                  "head": BRANCH, "base": "main",
                  "body": "Remplace les `uses: ...@vX` par le SHA-40 résolu (audit mensuel actions-audit). Aucun changement fonctionnel."})
    return "pr", pr.get("html_url") if st in (201, 422) else f"PR HTTP {st}"


def main():
    pat = get_pat()
    dry = "--dry-run" in sys.argv
    only_repo = sys.argv[sys.argv.index("--repo") + 1] if "--repo" in sys.argv else None
    report = json.load(open(REPORT_JSON))

    jobs = {}
    for repo, fs in report["findings"].items():
        if only_repo and not repo.endswith("/" + only_repo):
            continue
        for f in fs:
            if f.get("status") == "unpinned" and f.get("sha"):
                jobs.setdefault((repo, f["file"]), []).append(f)

    results = {"pushed": [], "pr": [], "dry": [], "error": []}
    for (repo, wf), fs in sorted(jobs.items()):
        st, txt = api("GET", f"{API}/repos/{repo}/contents/{wf}?ref=main", pat, raw=True)
        if st != 200:
            results["error"].append((f"{repo}/{wf}", f"GET raw: HTTP {st}"))
            continue
        changed = 0
        for f in fs:
            body_ref, ref = f["uses"].rsplit("@", 1)
            pattern = re.compile(r"(uses:\s*" + re.escape(body_ref) + r")@" + re.escape(ref) + r"\b")
            txt, n = pattern.subn(r"\1@" + f["sha"], txt)
            changed += n
        if changed == 0:
            results["error"].append((f"{repo}/{wf}", "aucun pattern remplacé"))
            continue
        status, info = push_file(repo, wf, txt, pat, dry)
        results[status].append((f"{repo}/{wf}", info, [f["uses"] + " -> @" + f["sha"][:12] for f in fs]))
        print(f"  {status:7s} {repo}/{wf} ({changed} pin(s))")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"📌 Fix épinglage SHA — {now}",
             f"pushés: {len(results['pushed'])} · PR: {len(results['pr'])} · dry: {len(results['dry'])} · erreurs: {len(results['error'])}"]
    for item in results["pr"]:
        lines.append(f"  🔀 PR: {item[0]} -> {item[1]}")
    for item in results["error"]:
        lines.append(f"  ❌ {item[0]}: {item[1]}")
    report_txt = "\n".join(lines)
    print(report_txt)
    send_telegram(report_txt)
    sys.exit(1 if results["error"] else 0)


if __name__ == "__main__":
    main()
