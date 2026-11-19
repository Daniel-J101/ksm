# Shared defaults for every ksm script. Source it; don't execute it.
#
#   source "$(dirname "$0")/../env.sh"
#
# All values can be overridden from the environment (ZB_DIR=... bash script.sh).
# Paths are resolved against the *real* user's home even under sudo, so
# `sudo bash X.sh` and `bash X.sh` see the same zeroboot checkout.

KSM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_USER="${SUDO_USER:-$(id -un)}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"

# Zeroboot checkout: pinned upstream + ksm patches, created by
# zeroboot-pin/setup_zeroboot.sh. Keep it on LOCAL disk, outside ksm (build
# artifacts and the guest kernel must not land in the shared dev directory).
: "${ZB_DIR:=$REAL_HOME/zeroboot}"
# Snapshot workdir: LOCAL disk, never NFS (Firecracker writes the mem file slowly).
: "${WORKDIR:=/tmp/zb-workdir-agent}"
ZB="$ZB_DIR/target/release/zeroboot"

# Phase 2+: big local data disk (tools/setup_data_disk.sh) for docker images,
# R2E templates, traces and the R2E-Gym checkout.
: "${DATA_DIR:=/mnt/data}"
: "${R2E_DIR:=$DATA_DIR/r2e/R2E-Gym}"
: "${TEMPLATES_DIR:=$DATA_DIR/templates}"
: "${HF_HOME:=$DATA_DIR/hf}"          # HuggingFace datasets cache (R2E-Gym task metadata)

export KSM_ROOT REAL_USER REAL_HOME ZB_DIR WORKDIR ZB DATA_DIR R2E_DIR TEMPLATES_DIR HF_HOME
