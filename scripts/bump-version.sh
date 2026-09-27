#!/usr/bin/env bash
# Set the release-train version for dpdpkit-core and the dpdpkit meta-package.
# Usage: scripts/bump-version.sh 0.1.0
set -euo pipefail
v="${1:?usage: bump-version.sh X.Y.Z}"
root="$(cd "$(dirname "$0")/.." && pwd)"
sed -i -E "s/^__version__ = \".*\"/__version__ = \"$v\"/" "$root/src/dpdpkit/__init__.py"
sed -i -E "s/^version = \".*\"/version = \"$v\"/" "$root/meta/pyproject.toml"
sed -i -E "s/\"dpdpkit-core==[^\"]*\"/\"dpdpkit-core==$v\"/" "$root/meta/pyproject.toml"
echo "dpdpkit-core and dpdpkit set to $v — update CHANGELOG.md, commit, then: git tag -s v$v"
