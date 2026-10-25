#!/usr/bin/env bash
# Choose the model-server profile for your machine.
#   scripts/select-profile.sh 32gb-cpu      # this XPS 15 9530: 32 GB RAM, Intel Arc (CPU only)  <- default
#   scripts/select-profile.sh 32gb          # NVIDIA GPU (RTX 4050/4060/4070) + 32 GB
#   scripts/select-profile.sh 64gb | 16gb   # other RAM sizes, NVIDIA GPU
#   scripts/select-profile.sh 16gb cpu      # any profile on CPU only
set -euo pipefail
cd "$(dirname "$0")/.."

PROFILE="${1:-32gb-cpu}"
BACKEND="${2:-}"
if [[ "$PROFILE" == *-cpu ]]; then BACKEND=cpu; fi
BACKEND="${BACKEND:-gpu}"
if [[ "$BACKEND" == "cpu" && "$PROFILE" != *-cpu && -f "llama-swap/profiles/$PROFILE-cpu.yaml" ]]; then
  PROFILE="$PROFILE-cpu"
fi
if [[ ! -f "llama-swap/profiles/$PROFILE.yaml" ]]; then
  echo "usage: $0 <32gb-cpu|16gb|32gb|64gb> [gpu|cpu]" >&2
  echo "available: $(cd llama-swap/profiles && ls *.yaml | sed 's/\.yaml//' | tr '\n' ' ')" >&2
  exit 1
fi

cp "llama-swap/profiles/$PROFILE.yaml" llama-swap/config.yaml
touch .env

set_env() {  # set_env KEY VALUE  (updates or appends a line in .env)
  if grep -q "^$1=" .env; then sed -i "s#^$1=.*#$1=$2#" .env; else echo "$1=$2" >> .env; fi
}

if [[ "$BACKEND" == "cpu" ]]; then
  set_env COMPOSE_FILE "docker-compose.yml:docker-compose.cpu.yml"
  echo "CPU-only mode (llama-swap:cpu image)."
else
  sed -i '/^COMPOSE_FILE=/d' .env
fi
# The download list is keyed by profile name; a GPU profile used on CPU keeps its files.
LIST_PROFILE="$PROFILE"
if ! awk -F'\t' -v p="$PROFILE" '$1==p {f=1} END {exit !f}' llama-swap/models.tsv; then
  LIST_PROFILE="${PROFILE%-cpu}"
fi
set_env AGENTBOX_PROFILE "$LIST_PROFILE"

echo "Profile $PROFILE ($BACKEND) written to llama-swap/config.yaml."
echo "Models it needs:"
awk -F'\t' -v p="$LIST_PROFILE" '$1==p {printf "  %-45s %5s GB\n", $3, $4}' llama-swap/models.tsv
echo "Next: scripts/download-models.sh   then   docker compose up -d   (or docker compose restart llama-swap)"
