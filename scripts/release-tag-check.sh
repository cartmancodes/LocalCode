#!/bin/sh
# Fails unless tag $1 (vX.Y.Z[-pre]) names the version in Cargo manifest $2.
set -eu
tag=$1
manifest=$2
version=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$manifest" | head -n 1)
if [ "${tag#v}" != "$version" ]; then
    echo "::error::Tag $tag does not match $manifest version $version" >&2
    exit 1
fi
echo "Tag $tag matches version $version"
