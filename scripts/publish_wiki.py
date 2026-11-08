#!/usr/bin/env python3
"""Publish docs/wiki/*.md to the GitHub Wiki of this repository (a separate git repo: <repo>.wiki.git).

The pages are kept in docs/wiki (reviewed with the code); this script converts them for the GitHub Wiki:
  - page links "01-Requirements.md#x"  -> "01-Requirements#x"
  - images and diagrams (../diagrams, ../screenshots) -> raw.githubusercontent.com URLs on the main branch
  - other repo links (../c4, ../guidelines/git.md, ../../README.md) -> github.com tree/blob URLs
  - a _Sidebar.md with the page list is generated.

  python scripts/publish_wiki.py              clone the wiki repo, replace its pages, commit, push
  python scripts/publish_wiki.py --dry-run    only build the converted pages into --out (default: build/wiki), no network
Prerequisite (once): GitHub -> Settings -> Features -> Wikis on, then create any first page in the web UI, otherwise
<repo>.wiki.git does not exist yet.
"""
from __future__ import annotations

import argparse
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIKI_SRC = ROOT / "docs" / "wiki"
BRANCH = "main"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
LINK = re.compile(r"(\]\()([^)\s]+)((?:\s+\"[^\"]*\")?\))")


def git(*args: str, cwd: Path | None = None) -> str:
    res = subprocess.run(["git", *args], cwd=cwd or ROOT, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


def repo_http_url() -> str:
    url = git("remote", "get-url", "origin")
    m = re.match(r"(?:git@github\.com:|https?://(?:[^@/]+@)?github\.com/)(.+?)(?:\.git)?/?$", url)
    if not m:
        raise RuntimeError(f"origin is not a GitHub URL: {url}")
    return f"https://github.com/{m.group(1)}"


def convert_links(text: str, repo_url: str, raw_base: str, page_dir: str = "docs/wiki") -> str:
    def repl(m: re.Match) -> str:
        target = m.group(2)
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|#|mailto:)", target, re.I):
            return m.group(0)
        path, _, anchor = target.partition("#")
        suffix = f"#{anchor}" if anchor else ""
        if not path:
            return m.group(0)
        repo_path = posixpath.normpath(posixpath.join(page_dir, path))
        if posixpath.dirname(repo_path) == "docs/wiki" and repo_path.endswith(".md"):
            return f"{m.group(1)}{posixpath.basename(repo_path)[:-3]}{suffix}{m.group(3)}"
        ext = posixpath.splitext(repo_path)[1].lower()
        if ext in IMAGE_EXT:
            return f"{m.group(1)}{raw_base}/{repo_path}{m.group(3)}"
        kind = "blob" if ext else "tree"
        return f"{m.group(1)}{repo_url}/{kind}/{BRANCH}/{repo_path}{suffix}{m.group(3)}"

    return LINK.sub(repl, text)


def build(out: Path, repo_url: str) -> list[str]:
    raw_base = repo_url.replace("https://github.com/", "https://raw.githubusercontent.com/") + f"/{BRANCH}"
    pages = sorted(p for p in WIKI_SRC.glob("*.md"))
    if not pages:
        raise RuntimeError(f"no pages in {WIKI_SRC}")
    for page in pages:
        (out / page.name).write_text(
            convert_links(page.read_text(encoding="utf-8"), repo_url, raw_base), encoding="utf-8", newline="\n")
    names = [p.stem for p in pages if p.stem != "Home"]
    side = ["**[Home](Home)**", ""] + [f"- [{n.replace('-', ' ')}]({n})" for n in names]
    side += ["", f"[Source code]({repo_url})"]
    (out / "_Sidebar.md").write_text("\n".join(side) + "\n", encoding="utf-8", newline="\n")
    return [p.name for p in pages] + ["_Sidebar.md"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="only build the pages into --out, no clone, no push")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "wiki", help="dry-run output folder")
    args = ap.parse_args()
    repo_url = repo_http_url()

    if args.dry_run:
        if args.out.exists():
            shutil.rmtree(args.out)
        args.out.mkdir(parents=True)
        files = build(args.out, repo_url)
        print(f"built {len(files)} files into {args.out}")
        return 0

    with tempfile.TemporaryDirectory(prefix="evedm-wiki-") as tmp:
        work = Path(tmp) / "wiki"
        try:
            git("clone", "--quiet", f"{repo_url}.wiki.git", str(work), cwd=Path(tmp))
        except RuntimeError as exc:
            print(f"cannot clone the wiki: {exc}\nEnable Wikis in the repository settings and create a first page "
                  f"in the web UI ({repo_url}/wiki), then run this again.", file=sys.stderr)
            return 1
        for old in work.glob("*.md"):
            old.unlink()
        files = build(work, repo_url)
        git("add", "-A", cwd=work)
        if not git("status", "--porcelain", cwd=work):
            print("wiki is already up to date")
            return 0
        git("commit", "--quiet", "-m", "docs(wiki): sync from docs/wiki", cwd=work)
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=work)
        git("push", "--quiet", "origin", branch, cwd=work)
        print(f"published {len(files)} files to {repo_url}/wiki")
    return 0


if __name__ == "__main__":
    sys.exit(main())
