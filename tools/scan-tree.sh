#!/usr/bin/env bash
#
# Scan every tracked file for identifiers that must not be published, and prove
# the scanner works before trusting its verdict.
#
#   bash tools/scan-tree.sh
#
# Exit 0 = clean, 1 = something found (or the scanner is broken).
#
# This runs in CI, but it is a plain script on purpose: a check that can only be
# exercised by pushing is a check nobody runs before pushing.
#
# Two layers, because they fail differently:
#
#   1. Shapes (below). Universal, disclose nothing, safe to commit. They catch
#      credentials and private hosts, including ones nobody thought to list.
#   2. Instance-specific strings — a real person's name, an internal codename.
#      These CANNOT live here: a denylist of the exact strings you are hiding
#      publishes every one of them to anyone who opens the file. They belong in
#      `tools/banned.local.json` (gitignored, optional, an array of strings),
#      which this script reads when present. CI runs layer 1 only.
#
# That split is not hypothetical. This repository shipped a contributor's real
# name and an internal project codename on its default branch for months, in a
# test fixture and a docstring, where neither CodeQL nor dependency review looks.

set -uo pipefail

# Boundaries are spelled out rather than using \b. Neither POSIX ERE nor awk
# implements \b, so a pattern using it matches nothing — silently, which is
# indistinguishable from a clean tree.
B='(^|[^A-Za-z0-9_])'
E='([^A-Za-z0-9_]|$)'

# Placeholders that are *supposed* to appear. Scrubbed from a line before the
# rule is re-applied, so a line carrying both a placeholder and a real address
# is still reported — which a line-level `grep -v` exemption would have hidden.
EXEMPT='[A-Za-z0-9._%+-]+@(example\.(com|org|net)|users\.noreply\.github\.com)'

# `named-secret-constant` — /[A-Z]+_(API_)?(KEY|TOKEN|SECRET)/ — is deliberately
# NOT a rule here. This repository redacts secrets for a living: it ships a
# "[REDACTED_SECRET]" placeholder, names the env vars it sets (LLM_API_KEY), and
# tests both. Every hit is a false positive, and a rule that only ever cries
# wolf gets muted, taking the real rules with it.
rules() {
  cat <<RULES
corporate-hostname|${B}([a-z0-9-]+\.)+(internal|corp|intra|lan)${E}
credential-blob|(-----BEGIN [A-Z ]*PRIVATE KEY-----|${B}(sk|ghp|gho|glpat)-[A-Za-z0-9_-]{16,})
credential-in-url|${B}[a-z][a-z0-9+.-]*://[^/[:space:]:@]+:[^/[:space:]@]+@
non-placeholder-email|[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}
RULES
}

# scan <file-of-NUL-separated-paths> -> lines of "rule<TAB>path:line"
# Prints where, never what.
scan() {
  local list="$1" label re
  while IFS='|' read -r label re; do
    [ -n "$label" ] || continue
    # `-H` because grep omits the filename when handed exactly one file, and the
    # self-test below hands it exactly one. Without it the offsets that split
    # location from content are computed against a line that carries no
    # location — or worse, a colon in the content shifts the fields into an
    # accidental match and a rule passes for the wrong reason. The real scan
    # passes many files at once and so never exposed this.
    #
    # `xargs -a FILE` is GNU-only; the redirect form is portable.
    # `|| true` because grep exits 1 on no-match, the normal case, which would
    # otherwise abort the loop under `bash -e`.
    xargs -0 grep -IHnE "$re" < "$list" 2>/dev/null \
      | awk -v re="$re" -v ex="$EXEMPT" -v label="$label" '
          {
            # Split "path:line:content" by position, not by field, so that
            # colons inside the content stay inside the content.
            if (match($0, /^[^:]+:[0-9]+:/) == 0) next
            location = substr($0, 1, RLENGTH - 1)
            content  = substr($0, RLENGTH + 1)
            gsub(ex, "", content)
            if (content ~ re) print label "\t" location
          }' || true
  done < <(rules)
}

canary="$(mktemp -d)"
trap 'rm -rf "$canary"' EXIT

# --- self-test: positive ------------------------------------------------------
# A scanner that matches nothing produces output identical to a clean tree.
# Plant one sample per rule and require every rule to fire.
{
  printf 'host build-07.corp\n'
  printf 'token glpat-ABCDEFGHIJKLMNOPQRSTUV\n'
  printf 'clone https://user:pw@git.somewhere.org/r.git\n'
  printf 'mail someone@somecorp.not-a-placeholder.net\n'
} > "$canary/planted.txt"
printf '%s\0' "$canary/planted.txt" > "$canary/list"

planted="$(scan "$canary/list")"
broken=0
while IFS='|' read -r label _; do
  [ -n "$label" ] || continue
  grep -q "^${label}" <<<"$planted" || {
    echo "::error::rule '${label}' failed to match its own planted sample"
    broken=1
  }
done < <(rules)

# --- self-test: negative ------------------------------------------------------
# The exemption is the half that fails silently in the safe direction's opposite:
# if it stops working, CI fails on legitimate placeholders until someone deletes
# the rule. An earlier version applied it after the path:line summary had already
# replaced the matched text, so it could never match anything at all.
printf 'contact test@example.com or ci@users.noreply.github.com\n' > "$canary/ok.txt"
printf '%s\0' "$canary/ok.txt" > "$canary/oklist"
if [ -n "$(scan "$canary/oklist")" ]; then
  echo "::error::placeholder addresses are being reported; the exemption is not working"
  broken=1
fi

[ "$broken" -eq 0 ] || {
  echo "the scanner is broken; its verdict on the real tree means nothing"
  exit 1
}
echo "scanner self-test: every rule fires, placeholders stay exempt"

# --- the real scan ------------------------------------------------------------
# Excluded: this script (it necessarily contains the patterns) and vendor/, a
# verbatim third-party snapshot whose contents are already public upstream and
# are not ours to change. Nothing else is excluded — in particular there is no
# carve-out for tests/, which is exactly where this repository's leak lived.
tracked="$canary/tracked"
git ls-files -z \
  ':!tools/scan-tree.sh' \
  ':!skills/repository-memory/vendor/**' > "$tracked"

hits="$(scan "$tracked")"

# Layer 2: instance-specific strings, if the operator has a local list.
local_list="$(dirname "$0")/banned.local.json"
if [ -f "$local_list" ]; then
  # Report the index, never the string, so a CI log or a shared terminal from a
  # machine that *does* have the file still gives nothing away.
  index=0
  while IFS= read -r needle; do
    [ -n "$needle" ] || continue
    if xargs -0 grep -Ilr -- "$needle" < "$tracked" >/dev/null 2>&1; then
      hits="${hits}"$'\n'"banned.local.json[${index}]"$'\t'"$(xargs -0 grep -Iln -- "$needle" < "$tracked" 2>/dev/null | head -20 | tr '\n' ' ')"
    fi
    index=$((index + 1))
  done < <(sed -n 's/.*"\(.*\)".*/\1/p' "$local_list")
fi

hits="$(printf '%s' "$hits" | grep -v '^$' || true)"

if [ -n "$hits" ]; then
  echo "::error::identifiers found in tracked files"
  printf '%s\n' "$hits"
  exit 1
fi
echo "tree is clean"
