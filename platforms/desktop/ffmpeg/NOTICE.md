# FFmpeg in the macOS package

FFmpeg 9.0.2 — https://ffmpeg.org/ — Copyright the FFmpeg developers.
Licensed under LGPL 2.1 or later; see COPYING.LGPLv2.1.
Pawzz's own license does not replace the FFmpeg license.

This is an unmodified-source static build for macOS 11+, arm64 and x86_64.
Only file/pipe input, MP4 stream copying and SHA-256 stream verification are
enabled. No external codec libraries, encoders, GPL components or network
protocols are enabled. This binary is not a general-purpose video converter.
It links only to macOS system libraries, not Homebrew libraries.

Complete corresponding source is included in source/ffmpeg-9.0.2.tar.xz.
Rebuild or replace it using tools/build_macos_ffmpeg.sh (requires Apple's
command line tools). The script records all configuration flags. No source
patches were applied. Users may modify/rebuild these components and reverse
engineer the combined executable to debug their modifications as permitted
by the LGPL. An ad-hoc signature is used; this is not Apple notarization.

Upstream: https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz
SHA-256: 8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
Release signature verified against fingerprint:
FCF986EA15E6E293A5644F10B4322F04D67658D8
