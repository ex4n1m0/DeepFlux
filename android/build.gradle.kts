// DeepFlux Android — root build file. Versions live in
// gradle/libs.versions.toml. Toolchain facts (pinned 2026-11-12, see
// ANDROID_STUDY.md §11): AGP 8.13.0 (last 8.x line — the AGP 9 built-in
// Kotlin migration is a deliberate LATER upgrade), Gradle 8.14.3,
// Kotlin 2.4.21, compileSdk/targetSdk 36, minSdk 28 (libtorrent4j floor).
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.compose) apply false
}
