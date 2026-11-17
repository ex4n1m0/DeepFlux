package space.deepflux.android

import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.memory.MemoryStore
import java.nio.file.Files

/**
 * P8 (1.4.5): the memory store persists agent knowledge — the dedupe /
 * scope / secret-rejection rules are contract, and it touches android.util
 * Log, hence Robolectric.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = android.app.Application::class)
class MemoryStoreTest {

    private fun store(): MemoryStore = MemoryStore(Files.createTempDirectory("mem").toFile())

    @Test
    fun saveSearchListEditForgetLifecycle() {
        val m = store()
        val saved = m.save("DeepFlux runs on the tablet AVD", "fact")
        assertTrue(saved.startsWith("ok: remembered"))

        val hit = m.search("tablet")
        assertTrue(hit.contains("DeepFlux runs on the tablet AVD"))

        val id = Regex("\\[id:([0-9a-f]{12})\\]").find(saved)!!.groupValues[1]
        assertTrue(m.list().contains(id))

        assertTrue(m.edit(id, "DeepFlux runs on the phone too").startsWith("ok"))
        assertTrue(m.search("phone").contains("DeepFlux runs on the phone too"))

        assertTrue(m.forget(id).startsWith("ok"))
        assertTrue(m.search("DeepFlux runs").contains("no memories match"))
    }

    @Test
    fun dedupesSameContentPerScope() {
        val m = store()
        m.save("favorite codec is hevc", "fact")
        val again = m.save("favorite codec is hevc", "fact")
        assertTrue(again.startsWith("ok: already remembered"))
    }

    @Test
    fun rejectsSecretLikeContent() {
        val m = store()
        assertTrue(m.save("my api key is sk-1234567890abcdef", "fact").startsWith("error"))
        assertTrue(m.save("password: hunter2", "fact").startsWith("error"))
    }

    @Test
    fun emptyStateHasNoPromptSection() {
        val m = store()
        assertTrue(m.promptSection().isEmpty())
        m.save("likes dark UI", "user")
        assertTrue(m.promptSection().contains("USER.md"))
    }

    @Test
    fun noteScopePrefixesDateAndIsSearchable() {
        val m = store()
        m.save("watched a test stream", "note")
        val hit = m.search("test stream", scope = "note")
        assertTrue(hit.contains("watched a test stream"))
    }
}
