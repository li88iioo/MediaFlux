#!/usr/bin/env bash
# Build official controllers, then exercise their real DELETE/config HTTP routes in TestServer.
# Does not start Jellyfin or contact a MediaFlux/cloud production instance.
set -euo pipefail
plugin=$(cd "$(dirname "$0")/.." && pwd)
repo=$(cd "$plugin/../.." && pwd)
cache=${JELLYFIN_VERIFY_CACHE:-"$repo/.superpowers/tmp/jellyfin-plugin"}
dotnet_bin=${DOTNET_BIN:-dotnet}
version=${1:-12.2}
if [ "$#" -gt 1 ] || [[ "$version" != 12.2 && "$version" != 12.3 ]]; then
  echo "Usage: $0 [12.2|12.3]" >&2
  exit 2
fi
if [ "$("$dotnet_bin" --version)" != "10.0.402" ]; then
  echo "Verification requires .NET SDK 10.0.402 (set DOTNET_BIN)." >&2
  exit 1
fi
source_dir="$cache/jellyfin-$version"
mkdir -p "$cache"
if [ ! -f "$source_dir/Jellyfin.Api/Jellyfin.Api.csproj" ]; then
  archive="$cache/jellyfin-$version.tar.gz"
  curl --fail --location --retry 2 "https://codeload.github.com/jellyfin/jellyfin/tar.gz/refs/tags/v$version" -o "$archive"
  tar -xzf "$archive" -C "$cache"
fi
"$dotnet_bin" build "$source_dir/Jellyfin.Api/Jellyfin.Api.csproj" -c Release --nologo
"$dotnet_bin" test "$plugin/Tests/MediaFlux.Jellyfin.Tests.csproj" -c Release --nologo \
  "-p:JellyfinVersion=$version.0" \
  "-p:JellyfinApiDir=$source_dir/Jellyfin.Api/bin/Release/net10.0"
