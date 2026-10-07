#!/bin/sh
# Publishes the files in directory $2 as GitHub release $1 with gh. A tag
# with a "-" is a pre-release. If the release exists (a re-run), its files
# are replaced instead.
set -eu
tag=$1
dist=$2
repo=${GITHUB_REPOSITORY:?}
if gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
    gh release upload "$tag" "$dist"/* --repo "$repo" --clobber
    exit 0
fi
case $tag in
*-*) set -- --prerelease ;;
*) set -- ;;
esac
gh release create "$tag" "$dist"/* --repo "$repo" --title "Octet $tag" --generate-notes "$@"
