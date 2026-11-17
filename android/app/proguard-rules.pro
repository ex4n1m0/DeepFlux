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
