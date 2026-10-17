#!/usr/bin/env bash
# Build one platform wheel of warrant-buyer.
#
#   scripts/build-buyer-wheel.sh <version> <bin-dir> <platform-tag> [out-dir]
#
# <bin-dir> holds the compiled warrant-policy and warrant-evidence for the target platform.
# <platform-tag> is a wheel platform tag, for example musllinux_1_2_x86_64 or macosx_11_0_arm64.
set -euo pipefail

version=${1:?version}
bindir=${2:?directory with warrant-policy and warrant-evidence}
platform=${3:?wheel platform tag}
out=${4:-dist}

root=$(cd "$(dirname "$0")/.." && pwd)
src=$root/showcases/ionet_arc
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

pkg=$stage/warrant_buyer
mkdir -p "$pkg/bin" "$pkg/config"
for f in __init__ adapter arc escrow buyer_mcp_server buyer_init cli; do
  cp "$src/$f.py" "$pkg/$f.py"
done
cp "$src/config/mainnet-public_keys.yaml" "$src/config/buyer_policy_template.yaml" "$pkg/config/"
for b in warrant-policy warrant-evidence; do
  install -m 0755 "$bindir/$b" "$pkg/bin/$b"
done
sed "s/@VERSION@/$version/" "$root/packaging/warrant-buyer/pyproject.toml" > "$stage/pyproject.toml"
cp "$root/packaging/warrant-buyer/README.md" "$stage/README.md"
cp "$root/LICENSE" "$stage/LICENSE"

mkdir -p "$out"
out=$(cd "$out" && pwd)
python3 -m build --wheel --no-isolation --outdir "$stage/dist" "$stage" >/dev/null
python3 -m wheel tags --platform-tag "$platform" --python-tag py3 --abi-tag none --remove "$stage"/dist/*.whl >/dev/null
cp "$stage"/dist/*.whl "$out/"
ls "$out"/warrant_buyer-"$version"-*"$platform"*.whl
