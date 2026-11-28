import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

// Obfuscated shared-key pair (salt/blob, hex) written by
// packaging/gen_android_key.py — gitignored, exactly like the desktop's
// _embedded_keys.py ritual. Absent file → keyless dev build; the app
// degrades to a "paste your own key" Settings flow. The plaintext key is
// NEVER a constant here: BuildConfig carries only the obfuscated pair and
// EmbeddedKey.decode mirrors config._decode_shared_value's
// XOR-sha256(salt+counter) scheme.
val embeddedKeyProps = Properties()
val embeddedKeyFile = rootProject.file("app/embedded_key.properties")
if (embeddedKeyFile.exists()) {
    embeddedKeyFile.inputStream().use { embeddedKeyProps.load(it) }
}

// Upload keystore (Play App Signing: Google holds the app signing key; this
// key only authenticates uploads, and CAN be reset via Play Console if ever
// lost — still back it up). keystore.properties is gitignored; missing file
// → unsigned release (local/dev builds keep working).
val keystoreProps = Properties()
val keystoreFile = rootProject.file("keystore.properties")
if (keystoreFile.exists()) {
    keystoreFile.inputStream().use { keystoreProps.load(it) }
}

android {
    namespace = "space.deepflux.android"
    // BOM is pinned to 2026.06.01 deliberately: the 2026.08+/2026.09 BOMs map
    // to Compose 1.12.x whose AARs demand compileSdk 37 (AGP 9.1+ territory).
    // 2026.06.01 = Compose 1.11.4 / material3 1.4.0, minCompileSdk 35 — the
    // newest BOM on the fully-supported AGP 8.13 + compileSdk 36 pairing.
    // Revisit together with the AGP 9 upgrade.
    compileSdk = 36

    defaultConfig {
        applicationId = "space.deepflux.android"
        minSdk = 28
        targetSdk = 36
        versionCode = 32
        versionName = "1.4.16"

        ndk {
            // libtorrent4j ships one .so per ABI. x86 (32-bit) dropped —
            // modern emulators are x86_64.
            abiFilters += listOf("arm64-v8a", "armeabi-v7a", "x86_64")
        }

        // Shared VENICE key pair — the built-in uncensored-DeepSeek agent
        // (1.4.12, owner 2026-11-17), written by gen_android_key.py from
        // SHARED_VENICE_API_KEY.
        buildConfigField(
            "String",
            "EMBEDDED_VENICE_SALT",
            "\"${embeddedKeyProps.getProperty("venice_salt_hex", "")}\"",
        )
        buildConfigField(
            "String",
            "EMBEDDED_VENICE_BLOB",
            "\"${embeddedKeyProps.getProperty("venice_blob_hex", "")}\"",
        )
        // Shared TMDb key pair (movies/series covers) — same obfuscated
        // scheme, written by gen_android_key.py from SHARED_TMDB_API_KEY.
        buildConfigField(
            "String",
            "EMBEDDED_TMDB_SALT",
            "\"${embeddedKeyProps.getProperty("tmdb_salt_hex", "")}\"",
        )
        buildConfigField(
            "String",
            "EMBEDDED_TMDB_BLOB",
            "\"${embeddedKeyProps.getProperty("tmdb_blob_hex", "")}\"",
        )
        // Adult-metadata providers (1.4.0): TPDB + StashDB.
        buildConfigField(
            "String",
            "EMBEDDED_TPDB_SALT",
            "\"${embeddedKeyProps.getProperty("tpdb_salt_hex", "")}\"",
        )
        buildConfigField(
            "String",
            "EMBEDDED_TPDB_BLOB",
            "\"${embeddedKeyProps.getProperty("tpdb_blob_hex", "")}\"",
        )
        buildConfigField(
            "String",
            "EMBEDDED_STASHDB_SALT",
            "\"${embeddedKeyProps.getProperty("stashdb_salt_hex", "")}\"",
        )
        buildConfigField(
            "String",
            "EMBEDDED_STASHDB_BLOB",
            "\"${embeddedKeyProps.getProperty("stashdb_blob_hex", "")}\"",
        )
    }

    signingConfigs {
        create("release") {
            if (keystoreFile.exists()) {
                storeFile = rootProject.file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            // R8 ON since 1.4.5 (P9): the libtorrent4j + BouncyCastle keeps
            // are in proguard-rules.pro; everything else ships consumer
            // rules. Full release smoke (tabs, torrent lane, updater) runs
            // on the emulator before every deploy.
            isMinifyEnabled = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
            if (keystoreFile.exists()) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    testOptions {
        unitTests {
            // Robolectric resource access + required for the Compose Preview
            // task to exist at all once unit-test sources are present
            // ("Compose Preview failed: Android resources are excluded from
            // unit tests", Gradle 9.6 / AGP — hit 2026-11-16).
            isIncludeAndroidResources = true
            // The test worker's heap. Gradle's default is 512 MB: enough on a
            // fast machine, GC-bound on a shared CI runner, where the tests'
            // 15 s waits then expire — one different test per run (CI runs
            // 3 and 4, 2026-11-26).
            all { it.maxHeapSize = "2g" }
        }
    }

    lint {
        // lintVital runs on assembleRelease and can fail CI on stylistic
        // fatals; run lint explicitly instead.
        checkReleaseBuilds = false
        abortOnError = false
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.material3.window.size)
    implementation(libs.androidx.compose.material.icons.core)
    implementation(libs.androidx.datastore.preferences)
    implementation(libs.kotlinx.coroutines.android)

    implementation(libs.libtorrent4j)
    implementation(libs.libtorrent4j.android.arm64)
    implementation(libs.libtorrent4j.android.arm)
    implementation(libs.libtorrent4j.android.x64)

    // Stream-while-downloading playback (v0.2): ExoPlayer pointed at a
    // loopback NanoHTTPD server that serves the growing torrent file.
    implementation(libs.androidx.media3.exoplayer)
    implementation(libs.androidx.media3.exoplayer.hls) // HLS module — REQUIRED for IPTV streams
    implementation(libs.androidx.media3.ui)
    // 1.4.15: ExoPlayer's FFmpeg AUDIO renderer, for audio the phone's own decoders lack —
    // Dolby (AC-3 / E-AC-3), DTS, TrueHD, MPEG audio (37 % of the films in the owner's TV
    // catalogue carry Dolby; on a phone without a decoder they played with no sound).
    // Used ONLY as a fallback (EXTENSION_RENDERER_MODE_ON in PlayerActivity). Google does
    // not publish this module: .github/workflows/android-ffmpeg-audio.yml builds it from
    // androidx/media at the version below, with those decoders; FFmpeg is built without GPL
    // parts (LGPL-2.1), as the FFmpeg the desktop app ships. 3.3 MB for four ABIs.
    // Rebuild = run the workflow, replace the file, record its sha256 in the commit.
    implementation(files("libs/decoder-ffmpeg-1.11.1.aar"))
    // Compatibility player (1.4.15): VLC's engine, for films whose VIDEO this phone cannot
    // decode (10-bit H.265 without the hardware for it). ExoPlayer has no software video
    // decoder (its FFmpeg video renderer is "not yet functional", androidx/media 1.11.1), so
    // those films used to stop with "Playback failed". LGPL-2.1, like the FFmpeg the desktop
    // app ships; the native libraries are loaded, never modified.
    implementation("org.videolan.android:libvlc-all:3.7.8")
    implementation(libs.nanohttpd)
    // Room (§13 #17): Ed25519 + Argon2id + ChaCha20-Poly1305 + HKDF.
    // XChaCha20-Poly1305 is composed in RoomCrypto (HChaCha + IETF AEAD).
    implementation("org.bouncycastle:bcprov-jdk18on:1.78.1")

    // HTTP/2 transport for the download engine (1.3.4): Cloudflare-walled
    // media CDNs (surrit.com) 403 a Chrome-UA request that arrives over
    // HttpURLConnection's HTTP/1.1+Conscrypt shape — OkHttp negotiates h2
    // (what a Chrome UA always speaks) and passes the same header set.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // Channel logos + movie/series covers in the Live TV list (1.3.5):
    // async image loading for the playlist rows.
    implementation("io.coil-kt:coil-compose:2.6.0")

    // Markdown in agent replies (2026-11-24): replies are markdown (the
    // desktop renders it); they used to show raw "**", "#" and table pipes.
    implementation(libs.commonmark)
    implementation(libs.commonmark.tables)
    implementation(libs.commonmark.autolink)

    debugImplementation(libs.androidx.compose.ui.tooling)

    // Unit tests (P8, 1.4.5): plain JUnit for the pure-JVM cores; Robolectric
    // only where Android types are entangled (MemoryStore's Context/Log).
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.robolectric:robolectric:4.13")
    testImplementation("androidx.test:core-ktx:1.6.1")
}
