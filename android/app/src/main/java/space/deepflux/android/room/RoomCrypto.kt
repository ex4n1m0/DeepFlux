package space.deepflux.android.room

import org.bouncycastle.crypto.digests.SHA256Digest
import org.bouncycastle.crypto.generators.Argon2BytesGenerator
import org.bouncycastle.crypto.generators.HKDFBytesGenerator
import org.bouncycastle.crypto.modes.ChaCha20Poly1305
import org.bouncycastle.crypto.params.AEADParameters
import org.bouncycastle.crypto.params.Argon2Parameters
import org.bouncycastle.crypto.params.HKDFParameters
import org.bouncycastle.crypto.params.Ed25519PrivateKeyParameters
import org.bouncycastle.crypto.params.Ed25519PublicKeyParameters
import org.bouncycastle.crypto.signers.Ed25519Signer
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.security.MessageDigest
import java.security.SecureRandom

/**
 * The OnlyHumans room protocol's crypto core — the Kotlin port of the
 * desktop's ircmgr/oh_room.py primitives (which mirror portal.ts / the
 * Rust core). Everything here must stay BYTE-EXACT with the desktop: the
 * same b64url alphabet, the same HKDF labels, the same AAD construction,
 * the same XChaCha20-Poly1305 (composed as HChaCha20 subkey + the IETF
 * AEAD — verified against a PyNaCl reference vector).
 */
object RoomCrypto {

    class RoomError(message: String) : Exception(message)

    private val random = SecureRandom()

    // ---------------------------------------------------------------- encoding

    private const val B64_ALPHABET =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    private const val B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

    fun b64(data: ByteArray): String {
        val out = StringBuilder()
        var i = 0
        while (i < data.size) {
            val b0 = data[i].toInt() and 0xFF
            val b1 = if (i + 1 < data.size) data[i + 1].toInt() and 0xFF else 0
            val b2 = if (i + 2 < data.size) data[i + 2].toInt() and 0xFF else 0
            val n = (b0 shl 16) or (b1 shl 8) or b2
            out.append(B64_ALPHABET[(n shr 18) and 63])
            out.append(B64_ALPHABET[(n shr 12) and 63])
            if (i + 1 < data.size) out.append(B64_ALPHABET[(n shr 6) and 63])
            if (i + 2 < data.size) out.append(B64_ALPHABET[n and 63])
            i += 3
        }
        return out.toString()
    }

    fun unb64(text: String): ByteArray {
        val out = ByteArrayOutputStream()
        var i = 0
        while (i < text.length) {
            var n = 0
            var count = 0
            while (count < 4 && i + count < text.length) {
                val idx = B64_ALPHABET.indexOf(text[i + count])
                if (idx < 0) throw RoomError("bad b64 character")
                n = n or (idx shl (18 - 6 * count))
                count++
            }
            out.write((n shr 16) and 0xFF)
            if (count > 2) out.write((n shr 8) and 0xFF)
            if (count > 3) out.write(n and 0xFF)
            i += 4
        }
        return out.toByteArray()
    }

    fun u64le(value: Long): ByteArray = ByteArray(8) { i -> (value ushr (8 * i)).toByte() }

    fun sha256(vararg parts: ByteArray): ByteArray {
        val md = MessageDigest.getInstance("SHA-256")
        parts.forEach { md.update(it) }
        return md.digest()
    }

    fun hkdf32(ikm: ByteArray, salt: ByteArray, info: ByteArray): ByteArray {
        val gen = HKDFBytesGenerator(SHA256Digest())
        gen.init(HKDFParameters(ikm, salt, info))
        val out = ByteArray(32)
        gen.generateBytes(out, 0, 32)
        return out
    }

    // ---------------------------------------------------------------- libp2p id

    fun publicKeyProtobuf(pubRaw: ByteArray): ByteArray = byteArrayOf(0x08, 0x01, 0x12, 0x20) + pubRaw

    fun ed25519RawFromProtobuf(protobuf: ByteArray): ByteArray {
        if (protobuf.size != 36 || protobuf[0] != 0x08.toByte() || protobuf[1] != 0x01.toByte() ||
            protobuf[2] != 0x12.toByte() || protobuf[3] != 0x20.toByte()
        ) throw RoomError("bad libp2p public key protobuf")
        return protobuf.copyOfRange(4, 36)
    }

    fun peerIdFromPublic(pubRaw: ByteArray): String {
        val protobuf = publicKeyProtobuf(pubRaw)
        val mh = byteArrayOf(0x00, protobuf.size.toByte()) + protobuf
        // base58btc of the identity multihash.
        var n = java.math.BigInteger(1, mh)
        val sb = StringBuilder()
        while (n.signum() > 0) {
            val rem = n.mod(java.math.BigInteger.valueOf(58)).toInt()
            n = n.divide(java.math.BigInteger.valueOf(58))
            sb.insert(0, B58_ALPHABET[rem])
        }
        for (byte in mh) {
            if (byte != 0.toByte()) break
            sb.insert(0, '1')
        }
        return if (sb.isEmpty()) "1" else sb.toString()
    }

    // ------------------------------------------------------------------ xchacha

    /**
     * HChaCha20 (RFC 7539's block function, no counter add, 4+4 output
     * words) — derives the XChaCha20 subkey from key + nonce[0..16].
     */
    private fun hchacha20(key: ByteArray, nonce16: ByteArray): ByteArray {
        fun le32(b: ByteArray, off: Int) =
            ((b[off].toInt() and 0xFF) or ((b[off + 1].toInt() and 0xFF) shl 8) or
                ((b[off + 2].toInt() and 0xFF) shl 16) or ((b[off + 3].toInt() and 0xFF) shl 24))

        val state = IntArray(16)
        val constants = intArrayOf(0x61707865, 0x3320646e, 0x79622d32, 0x6b206574)
        for (i in 0..3) state[i] = constants[i]
        for (i in 0..7) state[4 + i] = le32(key, i * 4)
        for (i in 0..3) state[12 + i] = le32(nonce16, i * 4)

        var working = state
        for (round in 0 until 10) {
            working = chachaRounds(working, column = true)
            working = chachaRounds(working, column = false)
        }
        val out = ByteArray(32)
        fun write32(v: Int, off: Int) {
            for (i in 0 until 4) out[off + i] = (v ushr (8 * i)).toByte()
        }
        for (i in 0..3) write32(working[i], i * 4)
        for (i in 0..3) write32(working[12 + i], 16 + i * 4)
        return out
    }

    private fun chachaRounds(s: IntArray, column: Boolean): IntArray {
        val x = s.copyOf()
        // ROTATE, never shift — the original code used shl and produced a
        // wrong subkey (caught live: gk-delivery frames failed auth while
        // every pre-XChaCha step verified).
        fun rotl(v: Int, n: Int) = (v shl n) or (v ushr (32 - n))
        fun qr(a: Int, b: Int, c: Int, d: Int) {
            x[a] += x[b]; x[d] = rotl(x[d] xor x[a], 16)
            x[c] += x[d]; x[b] = rotl(x[b] xor x[c], 12)
            x[a] += x[b]; x[d] = rotl(x[d] xor x[a], 8)
            x[c] += x[d]; x[b] = rotl(x[b] xor x[c], 7)
        }
        if (column) {
            qr(0, 4, 8, 12); qr(1, 5, 9, 13); qr(2, 6, 10, 14); qr(3, 7, 11, 15)
        } else {
            qr(0, 5, 10, 15); qr(1, 6, 11, 12); qr(2, 7, 8, 13); qr(3, 4, 9, 14)
        }
        return x
    }

    /** XChaCha20-Poly1305 (IETF) seal: ct = ciphertext || 16-byte tag.
     *  Composition per draft-irtf-cfrg-xchacha: HChaCha20(key, nonce[0..16])
     *  subkey, then the IETF AEAD with nonce12 = 0^4 || nonce[16..24]. */
    private fun ietfNonce(nonce: ByteArray): ByteArray {
        val n12 = ByteArray(12)
        System.arraycopy(nonce, 16, n12, 4, 8)
        return n12
    }

    fun xchachaSeal(key: ByteArray, nonce: ByteArray, aad: ByteArray, plaintext: ByteArray): ByteArray {
        require(nonce.size == 24) { "xchacha nonce must be 24 bytes" }
        val subkey = hchacha20(key, nonce.copyOfRange(0, 16))
        val engine = ChaCha20Poly1305()
        engine.init(true, AEADParameters(org.bouncycastle.crypto.params.KeyParameter(subkey), 128, ietfNonce(nonce), aad))
        return oneShot(engine, plaintext)
    }

    fun xchachaOpen(key: ByteArray, nonce: ByteArray, aad: ByteArray, ciphertext: ByteArray): ByteArray {
        require(nonce.size == 24) { "xchacha nonce must be 24 bytes" }
        val subkey = hchacha20(key, nonce.copyOfRange(0, 16))
        val engine = ChaCha20Poly1305()
        engine.init(false, AEADParameters(org.bouncycastle.crypto.params.KeyParameter(subkey), 128, ietfNonce(nonce), aad))
        return try {
            oneShot(engine, ciphertext)
        } catch (e: Exception) {
            throw RoomError("sealed frame failed authentication")
        }
    }

    /** BC AEAD one-shot helper: processBytes + doFinal (auth-checked). */
    private fun oneShot(engine: ChaCha20Poly1305, input: ByteArray): ByteArray {
        val out = ByteArray(input.size + 16)
        var len = engine.processBytes(input, 0, input.size, out, 0)
        len += engine.doFinal(out, len)
        return out.copyOf(len)
    }

    // ------------------------------------------------------------------- frame crypto

    const val ARGON2_M_KIB = 65536
    const val ARGON2_T = 3
    const val ARGON2_P = 1

    fun effectiveGk(gk: ByteArray, word: String?): ByteArray {
        val w = (word ?: "").trim().lowercase()
        if (w.isEmpty()) return gk
        val salt = sha256("OH1-pass-v2|".toByteArray(), gk, w.toByteArray(Charsets.UTF_8))
        val gen = Argon2BytesGenerator()
        gen.init(
            Argon2Parameters.Builder(Argon2Parameters.ARGON2_id)
                .withVersion(Argon2Parameters.ARGON2_VERSION_13)
                .withIterations(ARGON2_T)
                .withMemoryAsKB(ARGON2_M_KIB)
                .withParallelism(ARGON2_P)
                .withSalt(salt)
                .build(),
        )
        val out = ByteArray(32)
        gen.generateBytes(w.toByteArray(Charsets.UTF_8), out)
        return out
    }

    fun globalRoomHex(gk: ByteArray): String =
        sha256("OH1-room-v1|".toByteArray(), gk).joinToString("") { "%02x".format(it) }.take(32)

    fun admissionProof(gk: ByteArray, proverId: String, nonce: ByteArray): ByteArray =
        hkdf32(gk, ByteArray(0), "adm".toByteArray() + proverId.toByteArray(Charsets.UTF_8) + nonce)

    private val KIND_CHAT = "chat\u0000\u0000\u0000\u0000".toByteArray(Charsets.ISO_8859_1)
    private val KIND_ROTATE = "rotate\u0000\u0000".toByteArray(Charsets.ISO_8859_1)
    private val KIND_MEMBERS = "members\u0000".toByteArray(Charsets.ISO_8859_1)
    private val KIND_GK_DELIV = "gk-deliv".toByteArray(Charsets.ISO_8859_1)
    val KIND_CHAT_BYTES get() = KIND_CHAT
    val KIND_ROTATE_BYTES get() = KIND_ROTATE
    val KIND_MEMBERS_BYTES get() = KIND_MEMBERS

    fun aad(kind: ByteArray, room: ByteArray, epoch: Long, sender: String, seq: Long): ByteArray =
        "OH1".toByteArray() + kind + room + u64le(epoch) + sender.toByteArray(Charsets.UTF_8) + u64le(seq)

    fun sealRoomKey(gk: ByteArray, roomId: ByteArray, epoch: Long, recipient: String, key: ByteArray): ByteArray {
        val info = "gk-deliv-v2|".toByteArray() + u64le(epoch) + recipient.toByteArray(Charsets.UTF_8)
        val nonce = hkdf32(gk, roomId, info + "|n".toByteArray()).copyOfRange(0, 24)
        val keyEnc = hkdf32(gk, roomId, info + "|k".toByteArray())
        return xchachaSeal(keyEnc, nonce, aad(KIND_GK_DELIV, roomId, epoch, recipient, 0), key)
    }

    fun openRoomKey(gk: ByteArray, roomId: ByteArray, epoch: Long, recipient: String, ciphertext: ByteArray): ByteArray {
        val info = "gk-deliv-v2|".toByteArray() + u64le(epoch) + recipient.toByteArray(Charsets.UTF_8)
        val nonce = hkdf32(gk, roomId, info + "|n".toByteArray()).copyOfRange(0, 24)
        val keyEnc = hkdf32(gk, roomId, info + "|k".toByteArray())
        return xchachaOpen(keyEnc, nonce, aad(KIND_GK_DELIV, roomId, epoch, recipient, 0), ciphertext)
    }

    private val PAD_BUCKETS = intArrayOf(128, 256, 512, 1024, 2048, 4096, 8192, 16384)

    fun pad(body: ByteArray): ByteArray {
        val real = 4 + body.size
        var target = PAD_BUCKETS.last() * ((real + PAD_BUCKETS.last() - 1) / PAD_BUCKETS.last())
        for (b in PAD_BUCKETS) {
            if (b >= real) { target = b; break }
        }
        val out = ByteArray(target)
        out[0] = (body.size and 0xFF).toByte()
        out[1] = ((body.size shr 8) and 0xFF).toByte()
        out[2] = ((body.size shr 16) and 0xFF).toByte()
        out[3] = ((body.size shr 24) and 0xFF).toByte()
        System.arraycopy(body, 0, out, 4, body.size)
        return out
    }

    fun unpad(padded: ByteArray): ByteArray {
        val length = (padded[0].toInt() and 0xFF) or
            ((padded[1].toInt() and 0xFF) shl 8) or
            ((padded[2].toInt() and 0xFF) shl 16) or
            ((padded[3].toInt() and 0xFF) shl 24)
        if (length < 0 || 4 + length > padded.size) throw RoomError("bad padding length")
        return padded.copyOfRange(4, 4 + length)
    }

    fun randomBytes(n: Int): ByteArray = ByteArray(n).also { random.nextBytes(it) }

    fun nowMs(): Long = System.currentTimeMillis()

    /** One (room key, epoch) generation — seals and opens chat frames. */
    class Generation(val roomId: ByteArray, var epoch: Long, var key: ByteArray) {

        val roomHex: String get() = roomId.joinToString("") { "%02x".format(it) }

        private fun messageKey(sender: String, seq: Long, kind: ByteArray): ByteArray =
            hkdf32(key, roomId + u64le(epoch), kind + sender.toByteArray(Charsets.UTF_8) + u64le(seq))

        fun seal(sender: String, seq: Long, kind: ByteArray, plaintext: ByteArray): JSONObject {
            val mk = messageKey(sender, seq, kind)
            val nonce = randomBytes(24)
            val ct = xchachaSeal(mk, nonce, aad(kind, roomId, epoch, sender, seq), pad(plaintext))
            return JSONObject()
                .put("room_id_hex", roomHex)
                .put("epoch", epoch)
                .put("sender", sender)
                .put("seq", seq)
                .put("nonce_b64", b64(nonce))
                .put("ct_b64", b64(ct))
        }

        fun open(frame: JSONObject, kind: ByteArray): ByteArray {
            if (frame.optString("room_id_hex") != roomHex) throw RoomError("frame belongs to a different room")
            val frameEpoch = frame.optLong("epoch", 0)
            if (frameEpoch != epoch) throw RoomError("frame epoch $frameEpoch != current $epoch")
            val sender = frame.optString("sender", "")
            val seq = frame.optLong("seq", 0)
            val mk = messageKey(sender, seq, kind)
            val padded = xchachaOpen(
                mk,
                unb64(frame.optString("nonce_b64", "")),
                aad(kind, roomId, epoch, sender, seq),
                unb64(frame.optString("ct_b64", "")),
            )
            return unpad(padded)
        }

        fun applyRotation(nextEpoch: Long, nextKey: ByteArray) {
            if (nextEpoch != epoch + 1) throw RoomError("rotation epoch does not follow")
            epoch = nextEpoch
            key = nextKey
        }
    }
}

// ------------------------------------------------------------------- ed25519 identity

/**
 * The per-device Ed25519 identity (seed file in filesDir — deliberately
 * NOT inside the exported settings, mirroring the desktop's stance).
 */
object RoomIdentity {
    data class Identity(val seed: ByteArray, val pubRaw: ByteArray, val peerId: String, val pubB64: String) {
        fun sign(message: ByteArray): ByteArray {
            val signer = Ed25519Signer()
            signer.init(true, Ed25519PrivateKeyParameters(seed, 0)) // true = SIGN
            signer.update(message, 0, message.size)
            return signer.generateSignature()
        }
    }

    fun loadOrCreate(directory: java.io.File): Identity {
        val path = java.io.File(directory, "room_identity.seed")
        var seedHex = ""
        if (path.isFile) seedHex = runCatching { path.readText().trim() }.getOrDefault("")
        if (seedHex.length != 64) {
            seedHex = RoomCrypto.randomBytes(32).joinToString("") { "%02x".format(it) }
            runCatching { path.writeText(seedHex + "\n") }
        }
        val seed = ByteArray(32)
        for (i in 0 until 32) seed[i] = seedHex.substring(i * 2, i * 2 + 2).toInt(16).toByte()
        val priv = Ed25519PrivateKeyParameters(seed, 0)
        val pubRaw = priv.generatePublicKey().encoded
        return Identity(seed, pubRaw, RoomCrypto.peerIdFromPublic(pubRaw), RoomCrypto.b64(RoomCrypto.publicKeyProtobuf(pubRaw)))
    }

    fun verify(pubRaw: ByteArray, sig: ByteArray, message: ByteArray): Boolean = try {
        val verifier = Ed25519Signer()
        verifier.init(false, Ed25519PublicKeyParameters(pubRaw, 0)) // false = VERIFY
        verifier.update(message, 0, message.size)
        verifier.verifySignature(sig)
    } catch (e: Exception) {
        false
    }
}
