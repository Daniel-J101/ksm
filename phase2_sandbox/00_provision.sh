#!/usr/bin/env bash
# Phase 2 node prep, on top of phase1_zeroboot_cow/00_provision.sh:
#   data disk at $DATA_DIR, docker (data-root on the data disk), and a pinned
#   R2E-Gym checkout + venv (used to build templates and, in Phase 3, to record
#   traces with R2E-Gym's own docker runtime).
#
#   sudo bash 00_provision.sh                 # DEV=/dev/sdb by default
#   sudo DEV=/dev/nvme1n1 bash 00_provision.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../env.sh
source "$HERE/../env.sh"
R2E_REPO=https://github.com/R2E-Gym/R2E-Gym.git
R2E_COMMIT=0d94c4eb9431cd195c55a7ea3abd54006c9a1735   # 2025-07-13, has uv.lock
UV_VERSION=0.8.4

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
[ "$REAL_USER" != root ] || { echo "run via sudo from your normal user"; exit 1; }

echo "== 1. data disk =="
bash "$KSM_ROOT/tools/setup_data_disk.sh"

echo "== 2. docker (data-root=$DATA_DIR/docker) =="
export DEBIAN_FRONTEND=noninteractive
apt-get install -y docker.io python3-venv
mkdir -p /etc/docker
want="{\"data-root\": \"$DATA_DIR/docker\"}"
if [ "$(cat /etc/docker/daemon.json 2>/dev/null)" != "$want" ]; then
    echo "$want" > /etc/docker/daemon.json
    systemctl restart docker
fi
systemctl enable --now docker >/dev/null
[ "$(docker info -f '{{.DockerRootDir}}')" = "$DATA_DIR/docker" ] \
    || { echo "FATAL: docker root is $(docker info -f '{{.DockerRootDir}}')"; exit 1; }
# R2E-Gym talks to docker as the normal user. Takes effect on next login
# (or run commands under `sg docker -c '...'`).
usermod -aG docker "$REAL_USER"
echo "VERIFY: docker $(docker version -f '{{.Server.Version}}'), root $DATA_DIR/docker, $REAL_USER in group docker"

echo "== 3. R2E-Gym @ ${R2E_COMMIT:0:7} -> $R2E_DIR (as $REAL_USER) =="
mkdir -p "$(dirname "$R2E_DIR")"
chown "$REAL_USER:$(id -gn "$REAL_USER")" "$(dirname "$R2E_DIR")"
sudo -u "$REAL_USER" -H bash -euo pipefail -c "
    [ -d '$R2E_DIR/.git' ] || git clone -q '$R2E_REPO' '$R2E_DIR'
    cd '$R2E_DIR'
    git cat-file -e '$R2E_COMMIT^{commit}' 2>/dev/null || git fetch -q origin
    git -c advice.detachedHead=false checkout -q '$R2E_COMMIT'
    [ -x .uv/bin/uv ] || { python3 -m venv .uv && .uv/bin/pip install -q 'uv==$UV_VERSION'; }
    .uv/bin/uv sync --frozen --quiet
    # pyproject has no [build-system], so uv sync treats it as virtual; install it (as R2E's README does)
    .uv/bin/uv pip install --quiet --python .venv/bin/python --no-deps -e .
    .venv/bin/python -c 'import r2egym, litellm, docker; print(\"VERIFY: r2egym importable; litellm\", litellm.__version__ if hasattr(litellm, \"__version__\") else \"ok\")'
"

cat <<EOF

== Phase 2 provisioning done ==
  data disk : $DATA_DIR   docker root: $DATA_DIR/docker
  R2E-Gym   : $R2E_DIR  (venv: $R2E_DIR/.venv)
Log out/in (or use 'sg docker') before running docker as $REAL_USER.
EOF
