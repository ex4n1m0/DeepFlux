package space.deepflux.android

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import space.deepflux.android.room.RoomCrypto

/**
 * P8 (1.4.5): the Room's crypto primitives were vector-tested against
 * PyNaCl during the §13 #17 port but nothing pinned them in-repo. These
 * roundtrips + tamper cases are the regression net for exactly the bug
 * class that shipped live back then (Ed25519 verify silently rejecting
 * everything; HChaCha shl-vs-rotate wrong subkey). Robolectric supplies
 * the real org.json for Generation frames.
 */
@RunWith(RobolectricTestRunner::class)
@org.robolectric.annotation.Config(sdk = [34], application = android.app.Application::class)
class RoomCryptoTest {

    @Test
    fun b64RoundtripsWithAndWithoutPadding() {
        val data = byteArrayOf(0x00, 0x01, 0x02, (0xff).toByte())
        assertEquals(data.toList(), RoomCrypto.unb64(RoomCrypto.b64(data)).toList())
        // unpadded tokens must decode too (desktop python accepts them; the
        // .dfc import gotcha pinned padding on the OTHER lane)
        val padded = RoomCrypto.b64(data)
        val unpadded = padded.trimEnd('=')
        assertEquals(data.toList(), RoomCrypto.unb64(unpadded).toList())
    }

    @Test
    fun xchachaSealOpenRoundtrip() {
        val key = RoomCrypto.randomBytes(32)
        val nonce = RoomCrypto.randomBytes(24) // XChaCha nonce = 24 bytes
        val aad = RoomCrypto.u64le(42L)
        val plaintext = "room chat message".toByteArray()
        val ct = RoomCrypto.xchachaSeal(key, nonce, aad, plaintext)
        assertNotEquals(plaintext.toList(), ct.toList())
        assertArrayEquals(plaintext, RoomCrypto.xchachaOpen(key, nonce, aad, ct))
    }

    @Test
    fun xchachaTamperDetection() {
        val key = RoomCrypto.randomBytes(32)
        val nonce = RoomCrypto.randomBytes(24)
        val aad = RoomCrypto.u64le(1L)
        val ct = RoomCrypto.xchachaSeal(key, nonce, aad, "hello".toByteArray())

        val flippedAad = RoomCrypto.u64le(2L)
        org.junit.Assert.assertThrows(RoomCrypto.RoomError::class.java) {
            RoomCrypto.xchachaOpen(key, nonce, flippedAad, ct)
        }

        val flippedCt = ct.copyOf().also { it[it.size - 1] = (it[it.size - 1].toInt() xor 1).toByte() }
        org.junit.Assert.assertThrows(RoomCrypto.RoomError::class.java) {
            RoomCrypto.xchachaOpen(key, nonce, aad, flippedCt)
        }
    }

    @Test
    fun ed25519ProtobufConversionsRoundtrip() {
        val pub = RoomCrypto.randomBytes(32)
        val protobuf = RoomCrypto.publicKeyProtobuf(pub)
        assertArrayEquals(pub, RoomCrypto.ed25519RawFromProtobuf(protobuf))
    }

    @Test
    fun identitySignVerifyAndWrongKeyRejection() {
        val dir = java.nio.file.Files.createTempDirectory("room-identity").toFile()
        val id = space.deepflux.android.room.RoomIdentity.loadOrCreate(dir)
        val message = "sealed mail body".toByteArray()
        val sig = id.sign(message)
        assertTrue(space.deepflux.android.room.RoomIdentity.verify(id.pubRaw, sig, message))

        val other = space.deepflux.android.room.RoomIdentity.loadOrCreate(
            java.nio.file.Files.createTempDirectory("room-identity-2").toFile(),
        )
        assertFalse(space.deepflux.android.room.RoomIdentity.verify(other.pubRaw, sig, message))
        assertFalse(space.deepflux.android.room.RoomIdentity.verify(id.pubRaw, sig, "tampered".toByteArray()))

        // Same dir reloads the SAME identity (persisted), not a new one.
        val reloaded = space.deepflux.android.room.RoomIdentity.loadOrCreate(dir)
        assertArrayEquals(id.pubRaw, reloaded.pubRaw)
    }

    @Test
    fun generationSealOpenRoundtripAndRotation() {
        val roomId = RoomCrypto.randomBytes(16)
        val gen = RoomCrypto.Generation(roomId, epoch = 3L, key = RoomCrypto.randomBytes(32))
        val kind = "msg".toByteArray()
        val frame = gen.seal("alice", seq = 1L, kind = kind, plaintext = "first message".toByteArray())

        assertEquals(3L, frame.optLong("epoch"))
        assertArrayEquals("first message".toByteArray(), gen.open(frame, kind))

        // Wrong kind (AAD mismatch) must throw, not decode garbage.
        org.junit.Assert.assertThrows(RoomCrypto.RoomError::class.java) {
            gen.open(frame, "cmd".toByteArray())
        }

        // After rotation the OLD epoch's frames are rejected — the forward-
        // secrecy property the hub's rotation messages rely on.
        gen.applyRotation(4L, RoomCrypto.randomBytes(32))
        org.junit.Assert.assertThrows(RoomCrypto.RoomError::class.java) {
            gen.open(frame, kind)
        }
        org.junit.Assert.assertThrows(RoomCrypto.RoomError::class.java) {
            gen.applyRotation(6L, RoomCrypto.randomBytes(32)) // must be +1 exactly
        }
    }

    @Test
    fun paddingRoundtrips() {
        val body = "x".toByteArray()
        val padded = RoomCrypto.pad(body)
        assertTrue(padded.size > body.size)
        assertArrayEquals(body, RoomCrypto.unpad(padded))
        // unpad of garbage must fail, not loop or return nonsense
        org.junit.Assert.assertThrows(Exception::class.java) {
            RoomCrypto.unpad(byteArrayOf(0x00))
        }
    }

    @Test
    fun effectiveGkUsesRoomWord() {
        val gk = RoomCrypto.randomBytes(32)
        val global = RoomCrypto.effectiveGk(gk, null)
        val worded = RoomCrypto.effectiveGk(gk, "room-42")
        assertNotEquals(global.toList(), worded.toList())
        assertArrayEquals(global, RoomCrypto.effectiveGk(gk, null)) // deterministic
        assertArrayEquals(worded, RoomCrypto.effectiveGk(gk, "room-42"))
    }
}
