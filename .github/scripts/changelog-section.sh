#!/usr/bin/env bash
# Print the CHANGELOG.md body for a release tag; exit non-zero when there is none.
#
# Both jobs in publish.yml run this: `publish` to refuse an upload whose release could
# not be described, `github-release` to build the body it publishes. Keeping it in one
# place is load-bearing rather than tidy — PyPI refuses a second upload of a version it
# already holds, so a check that merely resembled the builder could pass while the
# builder later failed, leaving a published version with no release and no way back.
set -euo pipefail

tag="${1:?usage: changelog-section.sh <tag> [changelog-path]}"
changelog="${2:-CHANGELOG.md}"
version="${tag#v}"

# Print the body of "## [<version>]" up to whatever ends the section, dropping the blank
# lines that bracket it while keeping the ones between subsections. Two things can end
# it: the next release heading, and the link-reference block at the foot of the file,
# which trails the oldest section without a heading of its own.
body=$(
  awk -v ver="$version" '
    $0 == "## [" ver "]" { inside = 1; next }
    inside && /^## \[/ { exit }
    inside && /^\[[^]]+\]: / { exit }
    inside && NF {
      if (started) { for (i = 1; i <= held; i++) print buf[i] }
      held = 0; started = 1; print; next
    }
    inside { if (started) { held++; buf[held] = $0 } }
  ' "$changelog"
)

if [ -z "$body" ]; then
  echo "$changelog has no non-empty '## [$version]' section" >&2
  exit 1
fi

printf '%s\n' "$body"
