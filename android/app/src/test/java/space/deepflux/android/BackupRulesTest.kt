package space.deepflux.android

import android.app.Application
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.xmlpull.v1.XmlPullParser
import java.io.File

/**
 * Fix batch 2026-11-23 — the OS backup is an ALLOW-LIST. Before, the app
 * asked for backup with no rules at all, so everything in its storage was
 * uploaded: the settings file with both API keys and the chat log, the Room
 * identity, playlist and feed credentials, download cookies. These tests
 * pin what may leave the device, so widening it is always a deliberate edit.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = Application::class)
class BackupRulesTest {

    private val context: Context = ApplicationProvider.getApplicationContext()

    /** Everything that may be backed up — nothing else is, on any Android version. */
    private val allowed = setOf("browser_bookmarks.json", "player_positions.json", "memory")

    /** Every rule element of a rules file as "kind domain:path" (kind = include / exclude). */
    private fun rules(resId: Int): List<Triple<String, String?, String?>> {
        val out = mutableListOf<Triple<String, String?, String?>>()
        val p = context.resources.getXml(resId)
        var ev = p.eventType
        while (ev != XmlPullParser.END_DOCUMENT) {
            if (ev == XmlPullParser.START_TAG && p.name in setOf("include", "exclude")) {
                out.add(Triple(p.name, p.getAttributeValue(null, "domain"), p.getAttributeValue(null, "path")))
            }
            ev = p.next()
        }
        return out
    }

    /**
     * Robolectric's manifest loader does not fill ApplicationInfo's
     * dataExtractionRulesRes (it reads 0 even when wired), so the manifest
     * source itself is checked — it is what the built manifest is made from.
     */
    private fun applicationAttributes(): Map<String, String> {
        val candidates = listOf(
            File(System.getProperty("user.dir"), "src/main/AndroidManifest.xml"),
            File(System.getProperty("user.dir"), "app/src/main/AndroidManifest.xml"),
        )
        val manifest = candidates.firstOrNull { it.isFile }
            ?: throw AssertionError("AndroidManifest.xml not found under ${System.getProperty("user.dir")}")
        val doc = javax.xml.parsers.DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(manifest)
        val app = doc.getElementsByTagName("application").item(0)
        val out = mutableMapOf<String, String>()
        val attrs = app.attributes
        for (i in 0 until attrs.length) {
            out[attrs.item(i).nodeName.substringAfter("android:")] = attrs.item(i).nodeValue
        }
        return out
    }

    @Test
    fun theManifestPointsAtBothRuleFiles() {
        val attrs = applicationAttributes()
        assertEquals("an unwired fullBackupContent means Android ≤ 11 backs up EVERYTHING", "@xml/backup_rules", attrs["fullBackupContent"])
        assertEquals("an unwired dataExtractionRules means Android 12+ backs up EVERYTHING", "@xml/data_extraction_rules", attrs["dataExtractionRules"])
    }

    @Test
    fun legacyBackupIsAnAllowListOfHarmlessStoresOnly() {
        val rules = rules(R.xml.backup_rules)
        assertTrue("rules found", rules.isNotEmpty())
        rules.forEach { (kind, domain, path) ->
            assertEquals("only includes — this is an allow-list, never exclude-the-rest", "include", kind)
            assertEquals("file", domain)
            assertTrue("'$path' is not on the approved list", path in allowed)
        }
        assertEquals(allowed, rules.map { it.third }.toSet())
    }

    @Test
    fun android12BackupAndDeviceTransferCarryTheSameAllowList() {
        val rules = rules(R.xml.data_extraction_rules)
        assertTrue(rules.isNotEmpty())
        rules.forEach { (kind, domain, path) ->
            assertEquals("only includes", "include", kind)
            assertEquals("file", domain)
            assertTrue("'$path' is not on the approved list", path in allowed)
        }
        assertEquals(allowed, rules.map { it.third }.toSet())
    }

    // The two allow-list tests above are exact: "these three files and
    // nothing else" already rules out every secret-bearing store (the
    // settings with both API keys, the chat log, playlists, the .torrent
    // store). A separate "no secrets listed" test over the same rules
    // repeated them and proved nothing further.

    /**
     * Review round 2026-11-24 — the Commander's Share / Open with and the
     * updater go through one FileProvider. It exposed the WHOLE private
     * storage (`files-path path="."`): the settings with both API keys, the
     * chat log, saved .torrent files with tracker passkeys. Only the
     * download folder needs to be shareable.
     */
    @Test
    fun theFileProviderExposesTheDownloadFolderAndNothingElseOfThePrivateStorage() {
        val scopes = mutableListOf<Triple<String, String?, String?>>() // tag, name, path
        val p = context.resources.getXml(R.xml.file_paths)
        var ev = p.eventType
        while (ev != XmlPullParser.END_DOCUMENT) {
            if (ev == XmlPullParser.START_TAG && p.name != "paths") {
                scopes.add(Triple(p.name, p.getAttributeValue(null, "name"), p.getAttributeValue(null, "path")))
            }
            ev = p.next()
        }
        val internal = scopes.filter { it.first == "files-path" }
        assertEquals(
            "exactly one scope into private storage: the download folder",
            listOf(Triple("files-path", "downloads", "DeepFlux/")),
            internal,
        )
        // The updater's verified APK, nothing more from the cache.
        assertEquals(listOf(Triple("cache-path", "updates", "updates/")), scopes.filter { it.first == "cache-path" })
    }
}
