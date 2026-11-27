# libtorrent4j is a SWIG JNI wrapper — reflection-free but the swig layer
# references everything from Java; keep it whole. (Rules were pre-staged
# while minification was off; R8 turned ON with 1.4.5 / P9.)
-keep class org.libtorrent4j.** { *; }
-keep class org.libtorrent4j.swig.** { *; }
-dontwarn org.libtorrent4j.**

# BouncyCastle (Room crypto, §13 #17): its lightweight cipher/Mac registries
# resolve implementations by reflective name lookup — a stripped registry
# breaks Ed25519/ChaCha20 at RUNTIME only, long after boot. Keep whole.
-keep class org.bouncycastle.** { *; }
-dontwarn org.bouncycastle.**

# 1.4.15 — two JNI libraries, both looked up BY NAME from native code: the
# compatibility player (VLC) and ExoPlayer's FFmpeg audio renderer. A class or
# method R8 renames here is not a build error but a crash at the first use.
-keep class org.videolan.libvlc.** { *; }
-dontwarn org.videolan.libvlc.**
# The FFmpeg module ships these as consumer rules; repeated because AGP does not
# apply the consumer rules of a local AAR file (libs/decoder-ffmpeg-*.aar).
# ExoPlayer finds the renderer itself by reflection — its name must stay.
-keep,includedescriptorclasses class androidx.media3.decoder.ffmpeg.** {
    native <methods>;
}
-keep, includedescriptorclasses class androidx.media3.decoder.ffmpeg.FfmpegAudioDecoder {
  private java.nio.ByteBuffer growOutputBuffer(androidx.media3.decoder.SimpleDecoderOutputBuffer, int);
}
