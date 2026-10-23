#!/usr/bin/env bash
set -euo pipefail
plugin=$(cd "$(dirname "$0")/.." && pwd)
dotnet_bin=${DOTNET_BIN:-dotnet}
if [ "$#" -ne 0 ]; then
  echo "Usage: $0" >&2
  exit 2
fi
if [ "$("$dotnet_bin" --version)" != "10.0.402" ]; then
  echo "Packaging requires .NET SDK 10.0.402 (set DOTNET_BIN)." >&2
  exit 1
fi
"$dotnet_bin" build "$plugin/MediaFlux.Jellyfin.csproj" -c Release --nologo
python3 "$plugin/scripts/manifest.py" package
