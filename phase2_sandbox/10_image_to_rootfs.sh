#!/usr/bin/env bash
# Docker image -> Zeroboot sandbox rootfs (ext4) whose PID 1 is zbagent.
#
#   sudo bash 10_image_to_rootfs.sh <image> <outdir> [setup.sh]
#   sudo bash 10_image_to_rootfs.sh container:<id> <outdir>     # export a container
#                                     that is already set up (20_build_r2e_template.py)
#
# 1. Start the image (WITH network) and optionally run setup.sh inside it
#    (e.g. R2E-Gym's setup_env steps, which pip-install). Sandboxes have no
#    network, so anything that downloads must happen here.
# 2. docker export -> ext4 (mkfs.ext4 -d, no loop mount), sized to the content.
# 3. Add /zb-init (zbagent), /zb-env (the image's Config.Env), /zb-config.
# 4. Write <outdir>/manifest.json (image digest, sizes, agent hash, config).
#
# Optional env: ENV_PATH=<PATH value> replaces PATH in /zb-env (R2E runs every
# command with its DOCKER_PATH), WARMUP=<file> (copied to /zb-warmup.sh, run once before the
# snapshot), THP=madvise, TMPFS_SIZE=90%, WORKDIR_IN_IMAGE (default: the
# image's WorkingDir, else /).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../env.sh
source "$HERE/../env.sh"
IMAGE="${1:?usage: $0 <image> <outdir> [setup.sh]}"
OUT="${2:?usage: $0 <image> <outdir> [setup.sh]}"
SETUP="${3:-}"
: "${THP:=madvise}" "${TMPFS_SIZE:=90%}"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

mkdir -p "$OUT" "$DATA_DIR/work"
AGENT="$OUT/zbagent"
musl-gcc -static -O2 -march=x86-64 -mno-avx -mno-avx2 -Wall -o "$AGENT" "$HERE/agent/zbagent.c"

if [[ "$IMAGE" == container:* ]]; then
    cid="${IMAGE#container:}"
    IMAGE="$(docker inspect -f '{{.Config.Image}}' "$cid")"
    echo "== 1. existing container $cid (image $IMAGE) =="
    own_container=0
else
    echo "== 1. image $IMAGE =="
    docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull -q "$IMAGE"
    cid="$(docker run -d --entrypoint /bin/sh "$IMAGE" -c 'sleep infinity')"
    own_container=1
fi
cleanup() {
    [ "$own_container" = 1 ] && docker rm -f "$cid" >/dev/null 2>&1
    [ -n "${stage:-}" ] && [ -d "$stage" ] && rm -rf -- "$stage"
    return 0
}
trap cleanup EXIT
if [ -n "$SETUP" ]; then
    echo "   running setup $SETUP"
    docker cp "$SETUP" "$cid:/tmp/zb-setup.sh"
    docker exec "$cid" /bin/sh /tmp/zb-setup.sh
    docker exec "$cid" rm -f /tmp/zb-setup.sh
fi

echo "== 2. export -> staging dir =="
stage="$(mktemp -d "$DATA_DIR/work/rootfs.XXXXXX")"
docker export "$cid" | tar -C "$stage" --numeric-owner -xf -

# /zb-env: the image's environment, exactly as docker exec would see it
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" | sed '/^$/d' > "$stage/zb-env"
if [ -n "${ENV_PATH:-}" ]; then
    { grep -v '^PATH=' "$stage/zb-env" || true; echo "PATH=$ENV_PATH"; } > "$stage/zb-env.new"
    mv "$stage/zb-env.new" "$stage/zb-env"
fi
wd="${WORKDIR_IN_IMAGE:-$(docker inspect -f '{{.Config.WorkingDir}}' "$cid")}"
cat > "$stage/zb-config" <<EOF
TMPFS_SIZE=$TMPFS_SIZE
THP=$THP
WARMUP=/zb-warmup.sh
ENV_FILE=/zb-env
WORKDIR=${wd:-/}
EOF
install -m 0755 "$AGENT" "$stage/zb-init"
[ -n "${WARMUP:-}" ] && install -m 0755 "$WARMUP" "$stage/zb-warmup.sh"
# the agent needs these to exist on the template's root
mkdir -p "$stage/proc" "$stage/sys" "$stage/dev" "$stage/tmp"
[ -x "$stage/bin/cp" ] && [ -x "$stage/bin/sh" ] || { echo "FATAL: image lacks /bin/sh or /bin/cp"; exit 1; }

echo "== 3. ext4 =="
content_mb=$(( $(du -sxm "$stage" | cut -f1) ))
fs_mb=$(( content_mb * 115 / 100 + 256 ))
rm -f "$OUT/rootfs.ext4"
truncate -s "${fs_mb}M" "$OUT/rootfs.ext4"
mkfs.ext4 -q -F -d "$stage" -L zbroot "$OUT/rootfs.ext4"

digest="$(docker image inspect -f '{{index .RepoDigests 0}}' "$IMAGE" 2>/dev/null || docker image inspect -f '{{.Id}}' "$IMAGE")"
python3 - "$OUT/manifest.json" <<EOF
import json, sys
json.dump({
  "image": "$IMAGE", "image_digest": "$digest",
  "setup_script": "${SETUP:+$(basename "$SETUP")}" or None,
  "content_mb": $content_mb, "rootfs_mb": $fs_mb,
  "agent_sha256": "$(sha256sum "$AGENT" | cut -d' ' -f1)",
  "config": {"THP": "$THP", "TMPFS_SIZE": "$TMPFS_SIZE", "WORKDIR": "${wd:-/}",
             "warmup": $([ -n "${WARMUP:-}" ] && echo True || echo False)},
}, open(sys.argv[1], "w"), indent=2)
EOF
chown -R "$REAL_USER:$(id -gn "$REAL_USER")" "$OUT"
echo "VERIFY: $OUT/rootfs.ext4 (${fs_mb} MB fs, ${content_mb} MB content) + manifest.json"
