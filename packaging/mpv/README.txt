Bundled libmpv DLL for the IPTV player
======================================

Place the libmpv DLL in THIS directory before building the installer
(`pyinstaller packaging/app.spec --clean`). The PyInstaller spec collects
every *.dll found here into the bundle's `mpv/` subfolder, and
`iptv.player.ensure_mpv_dll_on_path()` prepends that folder to PATH at
runtime so the `python-mpv` binding finds libmpv on a clean Windows machine
with no external installs.

Required file
-------------
  libmpv-2.dll   (or mpv-2.dll / mpv-1.dll)  — libmpv, the playback engine

This build has FFmpeg STATICALLY LINKED into libmpv-2.dll (hence the ~120 MB
size), so NO separate avcodec/avformat/avutil/swscale/swresample/avfilter
DLLs are required. It also uses the system OpenGL (OPENGL32.dll) rather than
ANGLE, so no libGLESV2/libEGL DLLs are needed either. The only dependencies
are Windows system DLLs present on any Windows 10/11 install.

Verified with python-mpv: the binding imports and creates a working mpv
instance (mpv v0.41.x) from this DLL on a clean PATH.

The include/ headers and libmpv.dll.a in this folder are compile/link-time
artifacts and are NOT bundled (the spec only collects *.dll) — safe to leave
or remove.

Where to obtain a suitable build
--------------------------------
  * libmpv (gpac/zhongfly build, LGPL, static FFmpeg):
    https://sourceforge.net/projects/mpv-player-windows/files/libmpv/
    Look for an "mpv-dev-x86_64" archive; it contains libmpv-2.dll.
  * Any libmpv Windows build whose import table references only system DLLs
    will work. If your build dynamically links FFmpeg instead, also place the
    av*.dll / sw*.dll files here — the spec will bundle whatever *.dll is
    present.

Licensing (LGPL)
----------------
libmpv and the statically-linked FFmpeg are distributed under the GNU LGPL
(v2.1+). The corresponding source code and full license texts are available
from:
  - https://github.com/mpv-player/mpv
  - https://ffmpeg.org/download.html
License notice files live in ../licenses/ and are bundled into the installer.
Note: statically linking LGPL libraries into a larger work requires allowing
the end user to re-link against a modified version of the LGPL components.
Because libmpv-2.dll is a standalone, unmodified DLL loaded at runtime (not
linked into the DeepFlux executable), re-linking is possible by replacing
the DLL — which satisfies the LGPL obligation. If you switch to a build that
links FFmpeg *statically into a closed-source executable*, consult the LGPL
terms and ship the required object files / source offer instead.

python-vlc (libVLC) is an OPTIONAL fallback backend. If you want it bundled,
install VLC or place libvlc.dll / libvlccore.dll + the plugins/ folder where
`collect_dynamic_libs("vlc")` can find them; otherwise the mpv backend is
used and VLC is simply unavailable (no crash).
