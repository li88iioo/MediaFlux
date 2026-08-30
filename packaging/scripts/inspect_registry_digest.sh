#!/usr/bin/env bash
set -euo pipefail

reference=${1:?usage: inspect_registry_digest.sh IMAGE_REFERENCE}
attempts=${MEDIAFLUX_REGISTRY_INSPECT_ATTEMPTS:-3}
delay=${MEDIAFLUX_REGISTRY_INSPECT_DELAY_SECONDS:-2}

if [[ ! "$attempts" =~ ^[1-9][0-9]*$ ]]; then
  echo "MEDIAFLUX_REGISTRY_INSPECT_ATTEMPTS must be a positive integer" >&2
  exit 2
fi
if [[ ! "$delay" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "MEDIAFLUX_REGISTRY_INSPECT_DELAY_SECONDS must be non-negative" >&2
  exit 2
fi

error_file=$(mktemp)
trap 'rm -f "$error_file"' EXIT

for ((attempt = 1; attempt <= attempts; attempt++)); do
  if output=$(docker buildx imagetools inspect "$reference" 2>"$error_file"); then
    digest=$(awk '$1 == "Digest:" {print $2; exit}' <<<"$output")
    if [[ -z "$digest" ]]; then
      echo "Registry inspect succeeded without a digest for $reference" >&2
      exit 1
    fi
    printf '%s\n' "$digest"
    exit 0
  fi

  # Only a registry response that unambiguously means “this manifest/tag does
  # not exist” may permit a first promotion. Authentication, rate limiting,
  # transport, daemon and parser failures remain fatal (fail closed).
  if grep -Eqi \
    'manifest unknown|no such manifest|(^|[[:space:]:])manifest[^[:space:]]*[^[:alnum:]]not found([[:space:]]|$)' \
    "$error_file" \
    || grep -Fqi -- "$reference: not found" "$error_file" \
    || grep -Fqi -- "$reference not found" "$error_file"; then
    exit 3
  fi

  if ((attempt < attempts)); then
    sleep "$delay"
  fi
done

cat "$error_file" >&2
exit 1
