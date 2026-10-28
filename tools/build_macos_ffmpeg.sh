#!/bin/bash
# Build only the stream-copy formats needed by Pawzz; no third-party libraries.
set -euo pipefail
cd "$(dirname "$0")/.."
archive="${1:?Pass the official ffmpeg-9.0.2.tar.xz source archive}"
archive="$(cd "$(dirname "$archive")" && pwd)/$(basename "$archive")"
expected=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
[[ "$(shasum -a 256 "$archive" | cut -d ' ' -f 1)" == "$expected" ]] || { echo 'Source checksum mismatch'; exit 1; }
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/pawzz-ffmpeg.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT
tar -xf "$archive" -C "$build_dir"
source_dir="$build_dir/ffmpeg-9.0.2"
destination="$PWD/platforms/desktop/ffmpeg"
for arch in arm64 x86_64; do
  mkdir "$build_dir/$arch"
  cd "$build_dir/$arch"
  cross=''
  [[ "$(uname -m)" == "$arch" ]] || cross=--enable-cross-compile
  "$source_dir/configure" --disable-everything --disable-autodetect --disable-network \
    --disable-shared --enable-static --disable-ffplay --disable-ffprobe --enable-ffmpeg \
    --disable-doc --disable-debug --disable-asm --enable-protocol=file,pipe \
    --enable-demuxer=mov --enable-muxer=mp4,streamhash --enable-parser=aac,h264,hevc \
    --enable-bsf=aac_adtstoasc,extract_extradata,h264_mp4toannexb,hevc_mp4toannexb \
    --cc=clang --target-os=darwin --arch="$arch" ${cross:+"$cross"} \
    --extra-cflags="-arch $arch -mmacosx-version-min=11.0" \
    --extra-ldflags="-arch $arch -mmacosx-version-min=11.0"
  make -j6
  mkdir -p "$destination/macos-$arch"
  cp ffmpeg "$destination/macos-$arch/ffmpeg"
  chmod 755 "$destination/macos-$arch/ffmpeg"
  codesign --force --sign - "$destination/macos-$arch/ffmpeg"
done
mkdir -p "$destination/source"
cp "$archive" "$destination/source/ffmpeg-9.0.2.tar.xz"
cp "$source_dir/COPYING.LGPLv2.1" "$destination/COPYING.LGPLv2.1"
