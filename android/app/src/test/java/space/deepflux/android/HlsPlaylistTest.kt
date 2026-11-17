package space.deepflux.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import space.deepflux.android.dlmgr.HlsPlaylist

/** P8 (1.4.5): pins the HLS reader's download-safety behaviors (RFC 8216). */
class HlsPlaylistTest {

    private val base = "https://cdn.example.com/v/playlist.m3u8"

    @Test
    fun detectsPlaylistsAndMasters() {
        assertTrue(HlsPlaylist.isPlaylist("#EXTM3U\n#EXTINF:1,\nseg.ts"))
        assertFalse(HlsPlaylist.isPlaylist("<html>no</html>"))
        assertTrue(HlsPlaylist.isMaster("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\na.m3u8"))
        assertFalse(HlsPlaylist.isMaster("#EXTM3U\n#EXTINF:1,\nseg.ts"))
    }

    @Test
    fun picksHighestBandwidthVariantAndResolvesUrl() {
        val master = """
            #EXTM3U
            #EXT-X-STREAM-INF:BANDWIDTH=500000
            low/index.m3u8
            #EXT-X-STREAM-INF:BANDWIDTH=2500000
            hi/index.m3u8
        """.trimIndent()
        assertEquals("https://cdn.example.com/v/hi/index.m3u8", HlsPlaylist.pickVariant(master, base))
    }

    @Test
    fun skipsVariantsWithSeparateAudioTrack() {
        val master = """
            #EXTM3U
            #EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="en",URI="audio.m3u8"
            #EXT-X-STREAM-INF:BANDWIDTH=3000000,AUDIO="aud"
            video_only.m3u8
            #EXT-X-STREAM-INF:BANDWIDTH=1000000
            muxed.m3u8
        """.trimIndent()
        // The 3 Mbps variant needs the separate audio playlist — the 1 Mbps
        // muxed one must win instead of producing a silent-video file.
        assertEquals("https://cdn.example.com/v/muxed.m3u8", HlsPlaylist.pickVariant(master, base))
    }

    @Test
    fun masterWhereEveryVariantNeedsSeparateAudioThrows() {
        val master = """
            #EXTM3U
            #EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="en",URI="audio.m3u8"
            #EXT-X-STREAM-INF:BANDWIDTH=3000000,AUDIO="aud"
            video_only.m3u8
        """.trimIndent()
        val ex = org.junit.Assert.assertThrows(
            HlsPlaylist.Unsupported::class.java,
        ) { HlsPlaylist.pickVariant(master, base) }
        assertTrue(ex.message!!.contains("separate track"))
    }

    @Test
    fun masterListingNoStreamsAtAllThrows() {
        val ex = org.junit.Assert.assertThrows(
            HlsPlaylist.Unsupported::class.java,
        ) { HlsPlaylist.pickVariant("#EXTM3U\n", base) }
        assertTrue(ex.message!!.contains("no streams"))
    }

    @Test
    fun parsesSegmentsSequenceAndEndlist() {
        val media = """
            #EXTM3U
            #EXT-X-MEDIA-SEQUENCE:7
            #EXTINF:4.0,
            seg7.ts
            #EXTINF:4.0,
            seg8.ts
            #EXT-X-ENDLIST
        """.trimIndent()
        val m = HlsPlaylist.parseMedia(media, base)
        assertEquals(2, m.segments.size)
        assertEquals("https://cdn.example.com/v/seg7.ts", m.segments[0].url)
        assertEquals(7L, m.segments[0].sequence)
        assertEquals(8L, m.segments[1].sequence)
        assertTrue(m.ended)
        assertEquals(null, m.initUrl)
        assertEquals(null, m.segments[0].key)
    }

    @Test
    fun parsesAes128KeyWithExplicitIv() {
        val media = """
            #EXTM3U
            #EXT-X-KEY:METHOD=AES-128,URI="https://keys.example.com/k",IV=0x000102030405060708090a0b0c0d0e0f
            #EXTINF:4.0,
            seg.ts
        """.trimIndent()
        val m = HlsPlaylist.parseMedia(media, base)
        val key = m.segments[0].key!!
        assertEquals("https://keys.example.com/k", key.uri)
        assertEquals(0x00.toByte(), key.iv!![0])
        assertEquals(0x0f.toByte(), key.iv!![15])
    }

    @Test
    fun byterangeAndSampleAesFailClosed() {
        org.junit.Assert.assertThrows(HlsPlaylist.Unsupported::class.java) {
            HlsPlaylist.parseMedia("#EXTM3U\n#EXT-X-BYTERANGE:100@0\nseg.ts", base)
        }
        org.junit.Assert.assertThrows(HlsPlaylist.Unsupported::class.java) {
            HlsPlaylist.parseMedia("#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI=\"k\"\nseg.ts", base)
        }
    }

    @Test
    fun sequenceIvIsBigEndianSequenceNumber() {
        val iv = HlsPlaylist.sequenceIv(1L)
        assertEquals(15, iv.indexOfLast { it != 0.toByte() }) // 0x01 lands in the LAST byte
        assertEquals(1.toByte(), iv[15])
        assertEquals(0.toByte(), iv[0])
    }
}
