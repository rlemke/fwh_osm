#!/usr/bin/env bash
# Copy files from SRC to DST recursively. Skip a file when the destination
# already has the same size AND modification time. Preserve the source's
# timestamps on copied files.
#
# Usage: copy_skip_existing.sh <source-dir> <dest-dir>

set -euo pipefail

if [ $# -ne 2 ]; then
  echo "Usage: $0 <source-dir> <dest-dir>" >&2
  exit 1
fi

SRC=${1%/}
DST=${2%/}

if [ ! -d "$SRC" ]; then
  echo "Source not a directory: $SRC" >&2
  exit 1
fi

mkdir -p "$DST"

# Pick stat flavor: BSD (macOS) or GNU (Linux)
if stat -f '%z' / >/dev/null 2>&1; then
  stat_size()  { stat -f '%z' "$1"; }
  stat_mtime() { stat -f '%m' "$1"; }
else
  stat_size()  { stat -c '%s' "$1"; }
  stat_mtime() { stat -c '%Y' "$1"; }
fi

copied=0
skipped=0
failed=0

while IFS= read -r -d '' src_file; do
  rel=${src_file#"$SRC"/}
  dst_file=$DST/$rel

  if [ -f "$dst_file" ]; then
    if [ "$(stat_size "$src_file")"  = "$(stat_size "$dst_file")" ] && \
       [ "$(stat_mtime "$src_file")" = "$(stat_mtime "$dst_file")" ]; then
      skipped=$((skipped + 1))
      continue
    fi
  fi

  mkdir -p "$(dirname "$dst_file")"
  if cp -p "$src_file" "$dst_file"; then
    copied=$((copied + 1))
    echo "copied: $rel"
  else
    failed=$((failed + 1))
    echo "FAILED: $rel" >&2
  fi
done < <(find "$SRC" -type f -print0)

echo
echo "Done. copied=$copied  skipped=$skipped  failed=$failed"
