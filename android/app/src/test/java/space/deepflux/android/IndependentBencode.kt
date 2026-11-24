package space.deepflux.android

import java.io.ByteArrayOutputStream

/**
 * A bencoder and decoder for tests, written from the spec and from nothing
 * in the app: what the app writes is checked against it. (An app-shaped
 * decoder once agreed with an app-shaped writer on something that was no
 * bencode at all.)
 */
object IndependentBencode {

    sealed class Value {
        data class Bytes(val value: ByteArray) : Value()
        data class Number(val value: Long) : Value()
        data class ListOf(val items: List<Value>) : Value()
        data class Dict(val entries: Map<Bytes, Value>) : Value()
    }

    fun encode(value: Value): ByteArray = when (value) {
        is Value.Bytes -> ByteArrayOutputStream().apply {
            write(value.value.size.toString().toByteArray())
            write(':'.code)
            write(value.value)
        }.toByteArray()
        is Value.Number -> "i${value.value}e".toByteArray()
        is Value.ListOf -> ByteArrayOutputStream().apply {
            write('l'.code)
            value.items.forEach { write(encode(it)) }
            write('e'.code)
        }.toByteArray()
        is Value.Dict -> {
            // Keys in plain lexicographic order of the raw bytes.
            val keys = value.entries.keys.sortedBy { it.value.toString(Charsets.ISO_8859_1) }
            ByteArrayOutputStream().apply {
                write('d'.code)
                keys.forEach { k -> write(encode(k)); write(encode(value.entries.getValue(k))) }
                write('e'.code)
            }.toByteArray()
        }
    }

    private class Cursor(val b: ByteArray) {
        var pos = 0
        fun peek(): Char = b[pos].toInt().toChar()
        fun indexOf(c: Char): Int {
            var i = pos
            while (b[i].toInt().toChar() != c) i++
            return i
        }
        fun number(): Long {
            val end = indexOf('e')
            val v = String(b, pos, end - pos).toLong()
            pos = end + 1
            return v
        }
        fun take(count: Int): ByteArray = b.copyOfRange(pos, pos + count).also { pos += count }

        fun decode(): Value = when (peek()) {
            'i' -> { pos++; Value.Number(number()) }
            'l' -> {
                pos++
                val out = ArrayList<Value>()
                while (peek() != 'e') out.add(decode())
                pos++
                Value.ListOf(out)
            }
            'd' -> {
                pos++
                val out = LinkedHashMap<Value.Bytes, Value>()
                while (peek() != 'e') {
                    val key = decode() as Value.Bytes
                    out[key] = decode()
                }
                pos++
                Value.Dict(out)
            }
            else -> {
                val colon = indexOf(':')
                val len = String(b, pos, colon - pos).toInt()
                pos = colon + 1
                Value.Bytes(take(len))
            }
        }
    }

    /** The top-level dictionary of [bytes], and where it ended (the file must be exactly that long). */
    fun decode(bytes: ByteArray): Pair<Map<Value.Bytes, Value>, Int> {
        val cursor = Cursor(bytes)
        val top = cursor.decode() as Value.Dict
        return top.entries to cursor.pos
    }

    fun bytes(s: String): Value.Bytes = Value.Bytes(s.toByteArray(Charsets.UTF_8))
}

fun IndependentBencode.Value.Bytes.text(): String = value.toString(Charsets.UTF_8)

/** A map keyed by byte arrays compares them by identity — look an entry up by content. */
fun Map<IndependentBencode.Value.Bytes, IndependentBencode.Value>.at(key: String): IndependentBencode.Value =
    entries.first { it.key.value.contentEquals(key.toByteArray(Charsets.UTF_8)) }.value

fun Map<IndependentBencode.Value.Bytes, IndependentBencode.Value>.keyTexts(): Set<String> = keys.map { it.text() }.toSet()
