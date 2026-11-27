#!/bin/sh
# Install the pinned upstream Debian package on Debian/Ubuntu (run as root).
set -eu

version=0.8.0
architecture=$(dpkg --print-architecture)
case "$architecture" in
  amd64) checksum=8d21eebdf827a45e6ca10229c68f241830f37826cbbf9a1ee17cced27d60d1c1 ;;
  arm64) checksum=5cf649d521aadebf7922b503dce3f5ea77334bf89ed4cc53f1031f119cbc57f6 ;;
  *) printf 'Unsupported DNGLab package architecture %s\n' "$architecture" >&2; exit 1 ;;
esac

directory=$(mktemp -d)
trap 'rm -rf "$directory"' EXIT
package="dnglab_${version}-1_${architecture}.deb"
(
  cd "$directory"
  curl --fail --silent --show-error --location --retry 3 \
    "https://github.com/dnglab/dnglab/releases/download/v$version/$package" -o "$package"
  printf '%s  %s\n' "$checksum" "$package" | sha256sum -c -
  dpkg -i "$package"
)
