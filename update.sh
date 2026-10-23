#!/bin/bash
# ai-visualizer — update to the newest version, showing what changed first.
# Copyright (C) 2026 Jared Rhodenizer
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Your ai-visualizer.json is yours: nothing in this script can touch or overwrite it.
# Safe to run any time; when nothing is new it just says so.
#
# THIS IS A FORK. The port lives at https://github.com/Lex553/ai-visualizer as
# an ordinary checkout: one branch (main), one remote (origin). Updating is a
# plain fast-forward pull, so it cannot leave the tree half-updated.
#
# Upstream (jaredrhod/ai-visualizer) is deliberately not followed
# automatically. To take Jared's newest code, ask for it:
#
#   ./update.sh --upstream
#
# which fetches upstream, shows you what it brings, and merges it into main.
# A conflict stops with nothing changed, because a half-merged face is worse
# than an old one. Expect conflicts in server.py and index.html: this fork
# changed both, and they are the files Jared keeps working on.

FORK_URL="https://github.com/Lex553/ai-visualizer.git"
UPSTREAM_URL="https://github.com/jaredrhod/ai-visualizer.git"

main() {
  cd "$(dirname "$0")" || exit 1
  CFG="ai-visualizer.json"
  WANT_UPSTREAM=0
  [ "$1" = "--upstream" ] && WANT_UPSTREAM=1

  if [ ! -d .git ]; then
    # this folder arrived as a zip: wire it to updates, once, keeping the config
    [ -f "$CFG" ] && cp "$CFG" "$CFG.mine"
    git init -q -b main
    # The FORK, not upstream. Wiring a zip straight to Jared's repo would
    # make every later update quietly replace the port with the original.
    git remote add origin "$FORK_URL"
    git fetch -q origin
    git reset -q --hard origin/main
    git branch -q --set-upstream-to=origin/main main
    [ -f "$CFG.mine" ] && mv "$CFG.mine" "$CFG"
    echo "wired this folder to updates."
  fi

  if ! git fetch -q origin 2>/dev/null; then
    echo "  could not reach the fork (offline, or the repo is gone)."
    echo "  nothing was changed."
    echo "update complete."
    return
  fi
  git log --oneline "..origin/main" 2>/dev/null | sed "s/^/  new: /"

  # one-time migration: the config moved out of git tracking. If git here
  # still tracks the old copy, lift yours aside, let the pull retire the
  # tracked one, then put yours back exactly as it was.
  MIGRATE=0
  if git ls-files --error-unmatch "$CFG" >/dev/null 2>&1 && [ -f "$CFG" ]; then
    cp "$CFG" "$CFG.mine" && git checkout -q -- "$CFG" && MIGRATE=1
  fi

  if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
    if git checkout -q main 2>/dev/null; then
      echo "  switched to main."
    else
      echo "  could not switch to main; this folder has changes in the way."
      echo "  nothing was pulled. commit or stash them, then run this again."
      [ "$MIGRATE" = 1 ] && [ -f "$CFG.mine" ] && mv "$CFG.mine" "$CFG"
      echo "update complete."
      return
    fi
  fi

  # The update itself. Fast-forward only: a fork that has been edited by hand
  # gets told so rather than silently merged into a shape nobody chose.
  if git merge --ff-only -q origin/main >/dev/null 2>&1; then
    AHEAD=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
    if [ "$AHEAD" -gt 0 ] 2>/dev/null; then
      echo "  up to date with the fork; this checkout is $AHEAD commit(s) ahead."
      echo "  those are local commits, and this updater never pushes."
    else
      echo "  the face is up to date with the fork."
    fi
  else
    echo "  this checkout has its own commits that origin/main does not."
    echo "  nothing was changed. to see where they diverge:"
    echo "    git log --oneline origin/main..main"
    echo "  to keep them on top of ours:  git rebase origin/main"
  fi

  if [ "$WANT_UPSTREAM" = 1 ]; then
    git remote get-url upstream >/dev/null 2>&1 \
      || git remote add upstream "$UPSTREAM_URL"
    echo
    echo "  asking upstream (jaredrhod/ai-visualizer) for its newest code..."
    if git fetch -q upstream 2>/dev/null; then
      NEW=$(git log --oneline "..upstream/main" 2>/dev/null)
      if [ -z "$NEW" ]; then
        echo "  upstream has nothing this fork does not already have."
      else
        echo "$NEW" | sed "s/^/    upstream: /"
        if git merge --no-edit -q upstream/main >/dev/null 2>&1; then
          echo "  merged upstream into main."
          echo "  run this again with no flag to check the fork is still current."
        else
          git merge --abort >/dev/null 2>&1
          echo "  upstream CONFLICTS with this fork. nothing was changed."
          echo "  expected in server.py and index.html, which this fork rewrote."
          echo "  to resolve by hand:  git merge upstream/main"
        fi
      fi
    else
      echo "  could not reach upstream (offline, or the repo is gone)."
      echo "  nothing was changed."
    fi
  fi

  if [ "$MIGRATE" = 1 ] && [ -f "$CFG.mine" ]; then
    mv "$CFG.mine" "$CFG"
  fi

  echo "update complete."
}
main "$@"
