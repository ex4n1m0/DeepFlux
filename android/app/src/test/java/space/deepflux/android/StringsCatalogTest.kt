package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import space.deepflux.android.agent.LlmPresets
import space.deepflux.android.ui.AppStrings
import space.deepflux.android.ui.EnStrings
import space.deepflux.android.ui.Strings
import space.deepflux.android.ui.ZhStrings

/**
 * The string catalog as a whole (1.4.16). A new string used to be checked one by one, in
 * the test that needed it; these hold EVERY entry to the same rules, whatever is added:
 * both languages carry it, neither is blank, and both take the same placeholders — a
 * "%1$s" lost in translation is a crash in the field.
 */
class StringsCatalogTest {

    /** Every string's getter on the catalog interface — plain Java reflection (kotlin-reflect is not on the test classpath). */
    private val getters = AppStrings::class.java.methods.filter { it.name.startsWith("get") && it.parameterCount == 0 }.sortedBy { it.name }

    private fun name(getter: java.lang.reflect.Method) = getter.name.removePrefix("get").replaceFirstChar { it.lowercase() }

    private fun text(getter: java.lang.reflect.Method, catalog: AppStrings): String = getter.invoke(catalog) as String

    private val placeholder = Regex("%(?:\\d+\\$)?[sdb]")

    private fun placeholders(text: String) = placeholder.findAll(text).map { it.value }.sorted().toList()

    @Test
    fun everyEntryIsThereInBothLanguagesAndNotBlank() {
        assertTrue("${getters.size} entries", getters.size > 300)
        val catalogs: List<Pair<String, AppStrings>> = listOf("English" to EnStrings, "Chinese" to ZhStrings)
        for (g in getters) {
            for ((label, catalog) in catalogs) {
                assertTrue("${name(g)} is blank in $label", text(g, catalog).isNotBlank())
            }
        }
    }

    @Test
    fun bothLanguagesTakeTheSamePlaceholders() {
        val en = EnStrings
        val zh = ZhStrings
        for (g in getters) {
            assertEquals(
                "${name(g)}: the two languages format different values",
                placeholders(text(g, en)),
                placeholders(text(g, zh)),
            )
        }
    }

    @Test
    fun everyProviderAndEveryThinkingLevelHasANameInBothLanguages() {
        val ids = LlmPresets.ALL.map { it.id }
        assertEquals(listOf("venice", "deepseek", "openrouter", "custom"), ids)
        for (chinese in listOf(false, true)) {
            Strings.setLanguage(chinese)
            val names = ids.map { Strings.llmProviderLabel(it) }
            assertEquals("a name each", ids.size, names.toSet().size)
            val levels = LlmPresets.EFFORTS.map { Strings.llmEffortLabel(it) }
            assertEquals("a name each", LlmPresets.EFFORTS.size, levels.toSet().size)
            assertTrue("an unknown provider still has a name (it reads as the default)", Strings.llmProviderLabel("no-such").isNotBlank())
            assertTrue(Strings.llmEffortLabel("no-such").isNotBlank())
            if (chinese) {
                // What the chat says when a provider has no key of the user's.
                assertTrue(Strings.t.needsOwnKeyFmt.contains("%1\$s"))
                assertNotEquals(EnStrings.needsOwnKeyFmt, Strings.t.needsOwnKeyFmt)
            }
        }
        Strings.setLanguage(false)
    }
}
