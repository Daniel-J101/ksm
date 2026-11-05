#!/usr/bin/env bash
# Phase 1, step 1 -- bake ONE R2E-Gym repo into a bootable Zeroboot rootfs.
#
# WHY this shape: Zeroboot boots its own rootfs, which must contain Zeroboot's
# guest agent (the thing that speaks the serial exec protocol behind /v1/exec)
# AND a Python. An arbitrary R2E docker image does NOT have that agent, so we do
# NOT convert the docker image wholesale. Instead we start from Zeroboot's
# working python rootfs and OVERLAY the repo's /testbed + site-packages into it,
# preserving the boot/exec contract. The repo is pulled via its R2E docker image.
#
# This step is the one most likely to need on-node adjustment (interpreter
# paths, agent location). It is written to fail loud and tell you what to check.
#
#   sudo bash 10_build_r2e_rootfs.sh \
#        --docker-image <R2E docker_image> --pkg scrapy --repo scrapy
#
# Get <docker_image>/<pkg> from the shared fetcher:
#   python3 ../../numa_ksm_bench/agentzip_ksm_bench/fetch_tasks.py \
#           --repo scrapy --num 1 --out task.json   # reads docker_image, pkg
set -euo pipefail

ZB_DIR="${ZB_DIR:-$HOME/zeroboot}"
STOCK_ROOTFS="${STOCK_ROOTFS:-$ZB_DIR/workdir-python/rootfs.ext4}"  # Zeroboot's working rootfs
OUT_WORKDIR="${OUT_WORKDIR:-$ZB_DIR/workdir-r2e}"
GROW_MB="${GROW_MB:-4096}"          # headroom for the repo + deps
DOCKER_IMAGE=""; PKG=""; REPO=""

while [ $# -gt 0 ]; do
    case "$1" in
        --docker-image) DOCKER_IMAGE="$2"; shift 2;;
        --pkg) PKG="$2"; shift 2;;
        --repo) REPO="$2"; shift 2;;
        *) echo "unknown arg $1"; exit 1;;
    esac
done
[ -n "$DOCKER_IMAGE" ] || { echo "need --docker-image"; exit 1; }
[ -n "$PKG" ] || { echo "need --pkg (top-level import name, e.g. scrapy / PIL)"; exit 1; }

test -f "$STOCK_ROOTFS" || {
    echo "FATAL: $STOCK_ROOTFS not found.";
    echo "Build Zeroboot's stock python rootfs first (see its docs/DEPLOYMENT.md),";
    echo "or point STOCK_ROOTFS at the working rootfs that boots under Zeroboot.";
    exit 1; }

mkdir -p "$OUT_WORKDIR"
ROOTFS="$OUT_WORKDIR/rootfs.ext4"

echo "== 1. fresh copy of the stock rootfs (never snapshot the original) =="
cp --reflink=auto "$STOCK_ROOTFS" "$ROOTFS"
truncate -s +"${GROW_MB}M" "$ROOTFS"
e2fsck -fy "$ROOTFS" || true
resize2fs "$ROOTFS"

echo "== 2. export the repo's /testbed from its R2E docker image =="
docker pull "$DOCKER_IMAGE"
cid="$(docker create "$DOCKER_IMAGE")"
stage="$(mktemp -d)"
# R2E images keep the repo checked out at /testbed; copy that plus its deps.
docker cp "$cid:/testbed" "$stage/testbed"
# Site-packages location varies per image (venv/conda). Try common ones.
for sp in /usr/local/lib/python3*/site-packages \
          /opt/conda/lib/python3*/site-packages \
          /testbed/.venv/lib/python3*/site-packages; do
    docker cp "$cid:$sp" "$stage/site-packages-$(echo "$sp" | tr / _)" 2>/dev/null || true
done
docker rm "$cid" >/dev/null

echo "== 3. mount the rootfs and lay the repo in =="
mnt="$(mktemp -d)"
mount -o loop "$ROOTFS" "$mnt"
trap 'umount "$mnt" 2>/dev/null || true; rmdir "$mnt" 2>/dev/null || true; rm -rf "$stage"' EXIT
mkdir -p "$mnt/testbed"
cp -a "$stage/testbed/." "$mnt/testbed/"
# Overlay any exported site-packages into the guest's python path. VERIFY the
# guest's python version matches; mismatched ABI will fail imports on-node.
for d in "$stage"/site-packages-*; do
    [ -d "$d" ] || continue
    tgt="$(ls -d "$mnt"/usr/local/lib/python3*/site-packages 2>/dev/null | head -1 || true)"
    [ -n "$tgt" ] && cp -a "$d/." "$tgt/" || echo "WARN: no guest site-packages dir; adjust by hand"
done

echo "== 4. inject /init.py that WARMS the repo into snapshot RAM =="
# The paper preloads libraries so their pages are resident at snapshot time and
# thus CoW-shared across every fork (numpy/pandas in the stock template; here
# also the target repo). init runs at template time, then idles until snapshot.
cat > "$mnt/init.py" <<PY
import os, sys, time
os.chdir("/testbed")
sys.path.insert(0, "/testbed")
for mod in ("$PKG", "numpy", "pandas"):
    try:
        __import__(mod)
        print("warmed", mod, flush=True)
    except Exception as e:
        print("skip", mod, repr(e), flush=True)
# idle so Zeroboot's template step can snapshot a steady, warm memory image
while True:
    time.sleep(3600)
PY
sync
umount "$mnt"; trap - EXIT; rmdir "$mnt"; rm -rf "$stage"

cat <<EOF

== rootfs built ==
  rootfs : $ROOTFS   (repo=$REPO pkg=$PKG from $DOCKER_IMAGE)
  workdir: $OUT_WORKDIR

CHECK ON-NODE before trusting the snapshot:
  * Does this rootfs still boot under Zeroboot? (guest agent intact?)
      zeroboot test-exec $OUT_WORKDIR 'python3 -c "import $PKG; print($PKG.__file__)"'
  * If imports fail: guest python ABI != exported site-packages. Rebuild the
    repo's deps against the guest python instead of copying site-packages.

Next: bash 20_make_template.sh   (set WORKDIR=$OUT_WORKDIR)
EOF
