package space.deepflux.android.agent

import java.net.InetAddress
import java.net.URI

/**
 * Public-internet URL policy for URLs the AGENT supplies (download_url,
 * add_rss_feed) — the Android twin of the desktop's
 * `agent/tools.py::validate_public_http_url`.
 *
 * Why: this app's agent runs always-YOLO (every tool call executes at once,
 * owner decision), so a prompt injected through a web search result or a
 * scanned page must not be able to make it fetch the user's own network
 * (router admin pages, NAS, intranet hostnames) or push LAN URLs into the
 * download list. A URL the USER types into a dialog is their own choice and
 * is not checked.
 *
 * Accepted residual, same as on the desktop: the check resolves the host
 * and the later request resolves it again — a DNS-rebinding race.
 */
object PublicUrls {

    /** Default resolver (blocking DNS lookup — call off the main thread). */
    val SYSTEM_RESOLVER: (String) -> List<InetAddress> = { host -> InetAddress.getAllByName(host).toList() }

    /**
     * Null when the URL is a public http(s) address; otherwise a short
     * reason the model can act on.
     */
    fun rejection(url: String, resolve: (String) -> List<InetAddress> = SYSTEM_RESOLVER): String? {
        val raw = url.trim()
        val parsed = runCatching { URI(raw) }.getOrNull()
            ?: return "URL must be a valid http:// or https:// address"
        if (parsed.scheme?.lowercase() !in setOf("http", "https")) return "URL must be a valid http:// or https:// address"
        val host = parsed.host?.trimEnd('.')?.lowercase()
        if (host.isNullOrEmpty()) return "URL must be a valid http:// or https:// address"
        if (host == "localhost" || host.endsWith(".localhost") || host.endsWith(".local")) {
            return "Local and private network URLs are not allowed"
        }
        val addresses = runCatching {
            parseLiteral(host)?.let { listOf(it) } ?: resolve(host)
        }.getOrElse { return "Could not resolve URL host: $host" }
        if (addresses.isEmpty() || addresses.any { !isPublic(it) }) {
            return "Local and private network URLs are not allowed"
        }
        return null
    }

    private val V4_LITERAL = Regex("^\\d{1,3}(\\.\\d{1,3}){3}$")

    /**
     * An IP literal (dotted quad, or anything with a colon = IPv6) without
     * a name lookup; null for a host NAME, which the caller resolves. Never
     * passes a name to InetAddress — that would resolve only its first
     * address while a name may resolve to several, any of which may be private.
     */
    fun parseLiteral(host: String): InetAddress? =
        if (V4_LITERAL.matches(host) || host.contains(':')) runCatching { InetAddress.getByName(host) }.getOrNull()
        else null

    /**
     * Globally routable address (the desktop uses Python's `ip_address.is_global`).
     * IPv4: loopback, private, link-local, CGNAT 100.64/10, this-network 0/8,
     * IETF protocol/test nets (192.0.0/24, 192.0.2/24, 198.51.100/24,
     * 203.0.113/24), benchmarking 198.18/15, reserved 240/4 and multicast.
     * IPv6: unspecified, loopback, IPv4-mapped/compatible (judged as the IPv4),
     * unique-local fc00::/7, link-local fe80::/10, site-local fec0::/10,
     * documentation 2001:db8::/32, multicast ff00::/8.
     */
    fun isPublic(address: InetAddress): Boolean {
        val b = address.address
        return if (b.size == 4) !isPrivateV4(b) else !isPrivateV6(b)
    }

    private fun u(b: Byte): Int = b.toInt() and 0xff

    private fun isPrivateV4(b: ByteArray): Boolean {
        val o1 = u(b[0])
        val o2 = u(b[1])
        val o3 = u(b[2])
        return when {
            o1 == 0 || o1 == 10 || o1 == 127 -> true
            o1 == 100 && o2 in 64..127 -> true
            o1 == 169 && o2 == 254 -> true
            o1 == 172 && o2 in 16..31 -> true
            o1 == 192 && o2 == 168 -> true
            o1 == 192 && o2 == 0 && (o3 == 0 || o3 == 2) -> true // 192.0.0/24 protocol assignments, 192.0.2/24 TEST-NET-1
            o1 == 198 && (o2 == 18 || o2 == 19) -> true
            o1 == 198 && o2 == 51 && o3 == 100 -> true // TEST-NET-2
            o1 == 203 && o2 == 0 && o3 == 113 -> true // TEST-NET-3
            o1 >= 224 -> true
            else -> false
        }
    }

    private fun isPrivateV6(b: ByteArray): Boolean {
        val leadingZero = (0 until 15).all { u(b[it]) == 0 }
        if (leadingZero) return true // :: (unspecified) and ::1 (loopback)
        val mappedV4 = (0 until 10).all { u(b[it]) == 0 } && (u(b[10]) == 0xff && u(b[11]) == 0xff || u(b[10]) == 0 && u(b[11]) == 0)
        if (mappedV4) return isPrivateV4(b.copyOfRange(12, 16))
        val f = u(b[0])
        val s = u(b[1])
        return when {
            f and 0xfe == 0xfc -> true // fc00::/7 unique-local
            f == 0xfe && s and 0xc0 == 0x80 -> true // fe80::/10 link-local
            f == 0xfe && s and 0xc0 == 0xc0 -> true // fec0::/10 site-local (deprecated)
            f == 0xff -> true // multicast
            f == 0x20 && s == 0x01 && u(b[2]) == 0x0d && u(b[3]) == 0xb8 -> true // 2001:db8::/32 documentation
            else -> false
        }
    }
}
