#!/bin/bash
# Push NAS Safe to GitHub (SSH, no token needed)
# Run with: bash push.sh
# Prerequisite: add your SSH public key to GitHub once:
#   cat ~/.ssh/id_ed25519.pub
#   paste at https://github.com/settings/ssh/new

set -e

REPO_DIR="/e/我的AI软件/NAS快照AI工具"

cd "$REPO_DIR" || exit 1

echo "===================================="
echo "Push NAS Safe to GitHub (SSH)"
echo "===================================="
echo ""
git status -sb
echo ""

# Refuse to push if there are uncommitted changes
UNCOMMITTED=$(git status --porcelain)
if [ -n "$UNCOMMITTED" ]; then
    echo "Uncommitted changes found. Add and commit first, for example:"
    echo ""
    echo "  git add -A"
    echo "  git commit -m \"your message\""
    echo "  bash push.sh"
    echo ""
    exit 1
fi

read -rp "Press Enter to push 'main' to GitHub (Ctrl+C to cancel)..."
echo "Pushing..."
git push origin main

echo ""
echo "Done. No token needed (SSH key auth)."
read -rp "Press Enter to close..."
