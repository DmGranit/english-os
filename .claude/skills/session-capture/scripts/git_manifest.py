#!/usr/bin/env python3
"""git_manifest.py — детерминированный манифест сессии из git.
Коммиты+файлы С МОМЕНТА ПРОШЛОЙ КАПСУЛЫ (последний коммит, тронувший SESSION_CAPTURES/).
Faithful by construction: из git, не из памяти. Время — из --timestamp (shell date).

Usage: python git_manifest.py [--repo PATH] [--since-ref REF] [--fallback-n N] [--timestamp TS]
stdout: JSON {timestamp, since_ref, scope, commits[], files_changed_stat[], uncommitted[]}
"""
from __future__ import annotations
import argparse, json, subprocess, sys

def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8")

def find_since_ref(repo, fallback_n):
    r = _git(repo, "log", "-1", "--format=%H", "--", "SESSION_CAPTURES")
    ref = r.stdout.strip()
    if ref:
        return ref, "last-capture"
    r = _git(repo, "rev-list", "--max-count=1", f"HEAD~{fallback_n}")
    ref = r.stdout.strip()
    if ref:
        return ref, f"fallback-HEAD~{fallback_n}"
    r = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    lines = r.stdout.strip().splitlines()
    return (lines[0], "root") if lines else ("", "empty")

def manifest(repo, since_ref, fallback_n, timestamp):
    if since_ref:
        scope = "explicit"
    else:
        since_ref, scope = find_since_ref(repo, fallback_n)
    if scope == "empty" and not since_ref:
        return {"timestamp": timestamp, "since_ref": "", "scope": "empty",
                "commits": [], "files_changed_stat": [], "uncommitted": []}
    rng = f"{since_ref}..HEAD" if since_ref else "HEAD"
    commits = []
    for line in _git(repo, "log", rng, "--format=%h|%ci|%s").stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
    files = [l.strip() for l in _git(repo, "diff", "--stat", rng).stdout.strip().splitlines() if l.strip() and "|" in l]
    unc = [l for l in _git(repo, "status", "--porcelain").stdout.splitlines() if l]
    return {"timestamp": timestamp, "since_ref": since_ref, "scope": scope,
            "commits": commits, "files_changed_stat": files, "uncommitted": unc}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--since-ref", default=None)
    ap.add_argument("--fallback-n", type=int, default=20)
    ap.add_argument("--timestamp", default=None)
    a = ap.parse_args()
    ts = a.timestamp or _git(a.repo, "log", "-1", "--format=%ci").stdout.strip() or "unknown"
    print(json.dumps(manifest(a.repo, a.since_ref, a.fallback_n, ts), ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
