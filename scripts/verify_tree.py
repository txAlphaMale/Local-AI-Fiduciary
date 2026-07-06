#!/usr/bin/env python3
"""
verify_tree.py -- repo integrity scanner for the Fiduciary repo.

Why this exists: the Cowork/FUSE bridge that edits the Windows working copy has
truncated files mid-write (app.py once lost ~290 lines, ending in an unterminated
docstring). This script scans the *working tree* for that class of damage so a bad
write is caught immediately instead of being discovered later at runtime.

Run anytime, from WSL or the sandbox, at the repo root or with --root:
    python3 scripts/verify_tree.py
    python3 scripts/verify_tree.py --root /path/to/repo

Exit code 0 = clean, 1 = at least one ERROR (something is corrupt/uncompilable),
2 = not a git repo.

Checks (tracked files only, via `git ls-files`):
  ERROR  a .py file fails to parse (ast.parse) -- catches truncation, unterminated
         strings, unclosed brackets.
  ERROR  a .json file fails to parse.
  ERROR  a tracked file is unexpectedly empty (0 bytes).
  WARN   a tracked file is >SHRINK smaller on disk than its committed (HEAD) blob --
         possible truncation; review (could be a large intentional deletion).

Stdlib + git only. No network, no third-party deps.
"""
import argparse
import ast
import json
import os
import subprocess
import sys


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args],
                          capture_output=True, text=True)


def head_size(root, path):
    r = git(root, "cat-file", "-s", "HEAD:" + path)
    if r.returncode == 0:
        try:
            return int(r.stdout.strip())
        except ValueError:
            return None
    return None  # not in HEAD (new/untracked-at-HEAD file)


def main():
    ap = argparse.ArgumentParser(description="Scan the working tree for corrupt/truncated tracked files.")
    ap.add_argument("--root", default=".", help="repo path (default: cwd)")
    ap.add_argument("--shrink", type=float, default=0.25,
                    help="warn if a file is this fraction smaller than HEAD (default 0.25)")
    args = ap.parse_args()

    top = git(os.path.abspath(args.root), "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        print("ERROR not a git repo: " + os.path.abspath(args.root))
        return 2
    root = top.stdout.strip()

    files = git(root, "ls-files").stdout.splitlines()
    errors = 0
    warns = 0

    for rel in files:
        p = os.path.join(root, rel)
        if not os.path.exists(p):
            continue  # tracked but deleted; `git status` will surface it
        try:
            size = os.path.getsize(p)
        except OSError as e:
            print("ERROR cannot stat {}: {}".format(rel, e))
            errors += 1
            continue

        if size == 0:
            print("ERROR empty file: " + rel)
            errors += 1
            continue

        if rel.endswith(".py"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    ast.parse(f.read(), filename=rel)
            except (SyntaxError, ValueError, UnicodeDecodeError) as e:
                print("ERROR {} does not parse: {}".format(rel, e))
                errors += 1
                continue

        if rel.endswith(".json"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                print("ERROR {} bad JSON: {}".format(rel, e))
                errors += 1
                continue

        hs = head_size(root, rel)
        if hs and hs > 0 and size < hs * (1 - args.shrink):
            pct = 100 * (1 - size / hs)
            print("WARN  {} is {:.0f}% smaller than HEAD ({}b vs {}b) -- possible truncation"
                  .format(rel, pct, size, hs))
            warns += 1

    print("\nscanned {} tracked files: {} error(s), {} warning(s)"
          .format(len(files), errors, warns))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
