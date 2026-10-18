#!/usr/bin/env bash
# Inventory only: findings do not block the lab; scanner failures do.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

scanner='aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa'
mkdir -p .scan-reports/cache
reports=$(mktemp -d "$PWD/.scan-reports/run-XXXXXXXX")
# No real .env or LLM key is needed. Use a separate Compose project for builds.
export OPENROUTER_API_KEY=scan-placeholder
compose=(docker compose --env-file /dev/null -p agent-identity-scan -f compose.yaml)
"${compose[@]}" config --images | sort -u > "$reports/images.txt"
cp requirements.txt "$reports/requirements.txt"
printf '%s\n' "$scanner" > "$reports/scanner.txt"
date -u +'%Y-%m-%dT%H:%M:%SZ' > "$reports/scanned-at.txt"
printf 'Reports: %s\n' "$reports"
"${compose[@]}" build

trivy() {
  docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$reports:/reports" \
    -v "$PWD/.scan-reports/cache:/cache" \
    -e TRIVY_CACHE_DIR=/cache \
    "$scanner" "$@"
}

status=0
if ! trivy fs --scanners vuln --exit-code 0 --format json \
    --output /reports/requirements.json /reports/requirements.txt; then
  status=1
fi
index=0
while IFS= read -r image; do
  index=$((index + 1))
  printf '%s\n' "$image" > "$reports/image-$index.txt"
  if ! trivy image --scanners vuln --exit-code 0 --timeout 15m --format json \
      --output "/reports/image-$index.json" "$image"; then
    status=1
  fi
done < "$reports/images.txt"

for report in "$reports/requirements.json" "$reports"/image-*.json; do
  [[ -f "$report" ]] || continue
  if ! trivy convert --format table --output "/reports/$(basename "${report%.json}").table.txt" \
      "/reports/$(basename "$report")"; then
    status=1
  fi
done
printf 'Reports: %s\n' "$reports"
exit "$status"
