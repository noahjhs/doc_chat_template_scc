#!/bin/sh
# Project size: lines in git-tracked text files, by kind (no lockfiles or
# binaries). Usage: tools/size.sh [commit]   (default: the working tree at HEAD)
REV=${1:-HEAD}
git ls-tree -r --name-only "$REV" | grep -vE '(\.lock|go\.sum|package-lock\.json|\.png|\.jpg|\.icns|\.ico|\.zip|\.pdf)$' |
while read -r f; do
  n=$(git show "$REV:$f" 2>/dev/null | LC_ALL=C grep -c '' 2>/dev/null) || n=0
  case "$f" in
    *.go) k=Go ;; *.py) k=Python ;; *.html|*.css|*.js) k=Web ;; *.md) k=Docs ;; *.sh|*.toml|*.yml|*.yaml|Dockerfile|*/Dockerfile|*.conf) k=Config ;; *) k=Other ;;
  esac
  echo "$k $n"
done | awk '{t[$1]+=$2; all+=$2} END {for (k in t) printf "%s %d\n", k, t[k]; printf "Total %d\n", all}' | sort
