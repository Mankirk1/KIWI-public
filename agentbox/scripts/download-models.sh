#!/usr/bin/env bash
# Download the GGUF files for a profile into $MODELS_DIR (default ~/agentbox-runtime/models).
# Resumable: re-run if interrupted.
#   scripts/download-models.sh            # the profile chosen with select-profile.sh
#   scripts/download-models.sh 32gb-cpu
# Set HF_TOKEN in .env if Hugging Face rate-limits you.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && . ./.env && set +a

PROFILE="${1:-${AGENTBOX_PROFILE:-}}"
DEST="${MODELS_DIR:-$HOME/agentbox-runtime/models}"
mkdir -p "$DEST"
if ! awk -F'	' -v p="$PROFILE" '$1==p {f=1} END {exit !f}' llama-swap/models.tsv; then
  echo "usage: $0 <32gb-cpu|16gb|32gb|64gb>  (or run select-profile.sh first)" >&2
  exit 1
fi

need=0
while IFS=$'\t' read -r prof repo file gb; do
  [[ "$prof" == "$PROFILE" ]] || continue
  need=$(python3 -c "print(int($need + $gb + 1))")
done < llama-swap/models.tsv
free=$(df -BG --output=avail "$DEST" | tail -1 | tr -dc '0-9')
echo "Need about ${need} GB, ${free} GB free in $DEST"
if (( free < need )); then
  echo "Not enough disk space. Free some space or set MODELS_DIR in .env to another Linux path." >&2
  exit 1
fi

auth=()
[[ -n "${HF_TOKEN:-}" ]] && auth=(-H "Authorization: Bearer $HF_TOKEN")

while IFS=$'\t' read -r prof repo file gb; do
  [[ "$prof" == "$PROFILE" ]] || continue
  url="https://huggingface.co/$repo/resolve/main/$file"
  echo
  echo "==> $file (~$gb GB) from $repo"
  for try in 1 2 3 4 5; do
    if curl -fL --retry 5 --retry-delay 5 -C - "${auth[@]}" -o "$DEST/$file.part" "$url"; then
      mv "$DEST/$file.part" "$DEST/$file"
      break
    fi
    echo "download interrupted (attempt $try); resuming in 10s..."
    sleep 10
  done
  [[ -f "$DEST/$file" ]] || { echo "failed to download $file" >&2; exit 1; }
done < llama-swap/models.tsv

echo
ls -lh "$DEST"/*.gguf
echo "Done. Start or restart the model server: docker compose up -d"
