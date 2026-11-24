package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import space.deepflux.android.agent.PublicUrls
import java.net.InetAddress

/**
 * Fix batch 2026-11-23 — the agent may only fetch PUBLIC internet addresses
 * (desktop `validate_public_http_url` twin). The Android agent runs
 * always-YOLO, so a page-injected "download http://192.168.1.1/…" used to go
 * straight into the list. Pure JVM: the resolver is injected, no DNS.
 */
class PublicUrlsTest {

    private class FakeDns(private val map: Map<String, List<String>>) {
        val asked = mutableListOf<String>()
        val resolve: (String) -> List<InetAddress> = { host ->
            asked.add(host)
            map[host]?.map { InetAddress.getByName(it) } ?: throw java.net.UnknownHostException(host)
        }
    }

    private fun ip(text: String): InetAddress = InetAddress.getByName(text)

    // ---------------------------------------------------------------- address classes

    @Test
    fun privateAndSpecialRangesAreNotPublic() {
        val notPublic = listOf(
            "127.0.0.1", "0.0.0.0", "10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.1.1",
            "169.254.169.254", // link-local (cloud metadata service)
            "100.64.0.1", "100.127.255.255", // CGNAT
            "192.0.0.1", "192.0.2.1", "198.51.100.7", "203.0.113.9", // IETF special-purpose / documentation
            "198.18.0.1", "198.19.255.255", // benchmarking
            "224.0.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255", // multicast / reserved / broadcast
            "::", "::1", "::ffff:192.168.1.5", "::ffff:127.0.0.1", // v6 unspecified, loopback, v4-mapped
            "fc00::1", "fd12:3456:789a::1", // unique-local
            "fe80::1", // link-local
            "fec0::1", // site-local
            "2001:db8::1", // documentation
            "ff02::1", // multicast
        )
        for (text in notPublic) assertFalse("must NOT be public: $text", PublicUrls.isPublic(ip(text)))
    }

    @Test
    fun ordinaryAddressesArePublic() {
        val public = listOf("8.8.8.8", "1.1.1.1", "93.184.216.34", "172.32.0.1", "100.63.255.255", "100.128.0.1", "192.169.0.1", "192.0.1.1", "198.20.0.1", "203.0.114.1", "223.255.255.255", "2607:f8b0:4004:c07::6a")
        for (text in public) assertTrue("must be public: $text", PublicUrls.isPublic(ip(text)))
    }

    // ---------------------------------------------------------------- URL policy

    @Test
    fun rejectsNonHttpSchemesAndMalformedUrls() {
        val dns = FakeDns(emptyMap())
        for (bad in listOf("ftp://example.com/x", "file:///etc/passwd", "content://media/1", "javascript:alert(1)", "magnet:?xt=urn:btih:x", "not a url", "http://", "")) {
            assertNotNull("must reject '$bad'", PublicUrls.rejection(bad, dns.resolve))
        }
        assertTrue("no DNS lookup for malformed input", dns.asked.isEmpty())
    }

    @Test
    fun rejectsLocalHostnamesWithoutResolvingThem() {
        val dns = FakeDns(mapOf("localhost" to listOf("127.0.0.1")))
        assertEquals("Local and private network URLs are not allowed", PublicUrls.rejection("http://localhost:8080/admin", dns.resolve))
        assertEquals("Local and private network URLs are not allowed", PublicUrls.rejection("http://api.localhost/", dns.resolve))
        assertEquals("Local and private network URLs are not allowed", PublicUrls.rejection("http://nas.local/", dns.resolve))
        assertEquals("Local and private network URLs are not allowed", PublicUrls.rejection("http://NAS.LOCAL/", dns.resolve))
        assertTrue("name-based rejects never hit DNS", dns.asked.isEmpty())
    }

    @Test
    fun rejectsLiteralPrivateAddressesWithoutDns() {
        val dns = FakeDns(emptyMap())
        for (bad in listOf("http://192.168.1.1/", "http://10.0.0.5/x", "http://[::1]/", "http://[fe80::1]/x", "http://127.0.0.1:9117/api")) {
            assertEquals("'$bad'", "Local and private network URLs are not allowed", PublicUrls.rejection(bad, dns.resolve))
        }
        assertTrue("literals never hit DNS", dns.asked.isEmpty())
    }

    @Test
    fun rejectsANameThatResolvesIntoAPrivateNetwork() {
        val dns = FakeDns(mapOf("rebind.example" to listOf("93.184.216.34", "10.0.0.8")))
        assertEquals("Local and private network URLs are not allowed", PublicUrls.rejection("https://rebind.example/file", dns.resolve))
        assertEquals(listOf("rebind.example"), dns.asked)
    }

    @Test
    fun rejectsIpv4MappedAndAlternateNotations() {
        val dns = FakeDns(emptyMap())
        assertNotNull(PublicUrls.rejection("http://[::ffff:192.168.0.10]/", dns.resolve))
        assertNotNull(PublicUrls.rejection("http://[::ffff:7f00:1]/", dns.resolve))
        // Decimal / hex forms of 127.0.0.1 are host NAMES to java.net; they are
        // resolved, and whatever they resolve to is judged.
        val dns2 = FakeDns(mapOf("2130706433" to listOf("127.0.0.1")))
        assertNotNull(PublicUrls.rejection("http://2130706433/", dns2.resolve))
    }

    @Test
    fun unresolvableHostIsReportedNotAllowed() {
        val dns = FakeDns(emptyMap())
        assertEquals("Could not resolve URL host: nope.invalid", PublicUrls.rejection("https://nope.invalid/a.bin", dns.resolve))
    }

    @Test
    fun publicUrlsPass() {
        val dns = FakeDns(mapOf("example.com" to listOf("93.184.216.34"), "ipv6.example" to listOf("2607:f8b0:4004:c07::6a")))
        assertNull(PublicUrls.rejection("https://example.com/file.iso", dns.resolve))
        assertNull(PublicUrls.rejection("http://ipv6.example:8080/x?q=1", dns.resolve))
        assertNull(PublicUrls.rejection("http://8.8.8.8/", dns.resolve))
        assertNull(PublicUrls.rejection("http://[2607:f8b0:4004:c07::6a]/", dns.resolve))
    }

    @Test
    fun aHostNameStartingWithADigitIsResolvedNotTreatedAsALiteral() {
        // "1337x.to" starts with a digit: all of its addresses must be checked.
        val dns = FakeDns(mapOf("1337x.to" to listOf("1.2.3.4", "192.168.0.9")))
        assertEquals("Local and private network URLs are not allowed", PublicUrls.rejection("https://1337x.to/torrent", dns.resolve))
        assertEquals(listOf("1337x.to"), dns.asked)
    }
}
