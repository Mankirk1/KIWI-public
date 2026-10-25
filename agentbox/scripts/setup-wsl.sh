#!/usr/bin/env bash
# One-time setup inside WSL (Ubuntu 24.04), run from the KIWI checkout. Safe to re-run.
#   bash ~/KIWI/agentbox/scripts/setup-wsl.sh
#
# Layout it creates:
#   ~/KIWI/agentbox            code + your config.yaml and .env (git-ignored)
#   ~/agentbox-runtime/        models, project state, clones, logs, reports (never in git)
#   ~/.local/bin/agentctl      the command
set -euo pipefail

HOME_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"
RUNTIME="${AGENTBOX_RUNTIME:-$HOME/agentbox-runtime}"
cd "$HOME_DIR"

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

if [[ "$HOME_DIR" == /mnt/* ]]; then
  echo "agentbox must live in the Linux filesystem (clone KIWI to ~/KIWI), not under /mnt/c." >&2
  exit 1
fi

say "Checking system packages"
need=()
for pkg in python3 python3-venv python3-pip git curl jq ca-certificates; do
  dpkg -s "$pkg" >/dev/null 2>&1 || need+=("$pkg")
done
if ((${#need[@]})); then
  if sudo -n true 2>/dev/null; then
    sudo apt-get update -qq && sudo apt-get install -y -qq "${need[@]}" >/dev/null
  else
    echo "Missing packages: ${need[*]}"
    echo "Run this once yourself (it asks for your Ubuntu password), then re-run this script:"
    echo "  sudo apt-get update && sudo apt-get install -y ${need[*]}"
    exit 2
  fi
fi
echo "ok"

say "Python virtual environment ($HOME_DIR/.venv)"
python3 -m venv "$HOME_DIR/.venv"
"$HOME_DIR/.venv/bin/pip" install -q --upgrade pip
"$HOME_DIR/.venv/bin/pip" install -q -e "$HOME_DIR/orchestrator"

say "agentctl command"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/agentctl" <<EOF
#!/usr/bin/env bash
export AGENTBOX_HOME="$HOME_DIR"
exec "$HOME_DIR/.venv/bin/agentctl" "\$@"
EOF
chmod +x "$HOME/.local/bin/agentctl"
if ! grep -q 'HOME/.local/bin' "$HOME/.bashrc" 2>/dev/null; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
fi

say "Runtime folder ($RUNTIME)"
mkdir -p "$RUNTIME/models"

say "Config files (existing files are kept)"
[[ -f config.yaml ]] || cp config.example.yaml config.yaml
[[ -f .env ]] || cp .env.example .env
chmod 600 .env
set_env() { if grep -q "^$1=" .env; then sed -i "s#^$1=.*#$1=$2#" .env; else echo "$1=$2" >> .env; fi; }
set_env MODELS_DIR "$RUNTIME/models"           # docker compose needs an absolute path
[[ -f llama-swap/config.yaml ]] || bash scripts/select-profile.sh 32gb-cpu >/dev/null
chmod +x scripts/*.sh
echo "config.yaml, .env and llama-swap/config.yaml are in place"

say "Docker"
if docker info >/dev/null 2>&1; then
  echo "Docker is reachable from WSL."
else
  echo "Docker is NOT reachable. Start Docker Desktop and enable Settings > Resources > WSL integration for this distro."
fi

say "Done. Remaining before the first run:"
grep -q 'github_pat_xxx' .env && echo "  - put your two GitHub tokens in $HOME_DIR/.env   (nano $HOME_DIR/.env)"
grep -q 'CHANGE-ME' config.yaml && echo "  - set github.owner in $HOME_DIR/config.yaml"
ls "$RUNTIME"/models/*.gguf >/dev/null 2>&1 || echo "  - download the models: scripts/download-models.sh"
docker image inspect agentbox-worker:latest >/dev/null 2>&1 || echo "  - build the sandbox image: scripts/build-worker.sh"
echo "  - start services: docker compose up -d     then check: agentctl doctor"
echo "Open a new terminal (or: source ~/.bashrc) so agentctl is on your PATH."
