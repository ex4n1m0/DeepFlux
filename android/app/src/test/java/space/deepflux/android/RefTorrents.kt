package space.deepflux.android

import java.util.Base64

/**
 * Reference torrents for the identity tests (1.4.16): a V1-ONLY, a HYBRID and a V2-ONLY
 * torrent of the same two files, made by libtorrent 2.1.1 itself — the core the app's engine
 * is built from — so the hashes the app's own reader computes are checked against the
 * library's, never against the reader's own idea of them.
 *
 * Multi-file on purpose: a single-file torrent has no `piece layers`, and the v2 info-hash
 * is the one thing that is computed WITHOUT them.
 *
 * Made by the scratch script `_gate98/android/gen_ref_torrents.py` (not part of the build):
 * two files of 40960 and 9600 bytes, 16 KiB pieces, creation date 1700000000.
 */
object RefTorrents {

    class Ref(
        val base64: String,
        val v1: String?,
        val v2: String?,
        /** What libtorrent's own `info_hash()` answers for the torrent — the name every client knows it by. */
        val libtorrentName: String,
    ) {
        val bytes: ByteArray by lazy { Base64.getDecoder().decode(base64) }

        /** The name the ENGINE reports for the torrent: the first 20 bytes of its v2 hash, else its v1 hash. */
        val engineHash: String get() = v2?.take(40) ?: v1!!

        /** The magnet a site links a torrent of this kind by: its v1 hash, or its whole v2 one. */
        val siteMagnet: String get() = "magnet:?xt=urn:btih:" + (v1 ?: v2)
    }

    val all: Map<String, Ref> = mapOf(
    "v1" to Ref(
        base64 = "ZDEzOmNyZWF0aW9uIGRhdGVpMTcwMDAwMDAwMGU0OmluZm9kNTpmaWxlc2xkNjpsZW5ndGhpNDA5NjBlNDpwYXRobDU6YS5iaW5lZWQ2Omxlbmd0aGk5NjAwZTQ6cGF0aGw1OmIuYmluZWVlNDpuYW1lODpSZWYgUGFjazEyOnBpZWNlIGxlbmd0aGkxNjM4NGU2OnBpZWNlczgwOoDLnEMNgMMIRkn2XgyiXau/+xtigMucQw2AwwhGSfZeDKJdq7/7G2LzKotvuXo5/3X7QJEzKdWaQVPkqtfIBm6mtFAqcBswvvp3xJZ4sEuCZWU=",
        v1 = "d875ac0b4ef5925d1668ea8c863bc136683f0128",
        v2 = null,
        libtorrentName = "d875ac0b4ef5925d1668ea8c863bc136683f0128",
    ),
    "hybrid" to Ref(
        base64 = "ZDEzOmNyZWF0aW9uIGRhdGVpMTcwMDAwMDAwMGU0OmluZm9kOTpmaWxlIHRyZWVkNTphLmJpbmQwOmQ2Omxlbmd0aGk0MDk2MGUxMTpwaWVjZXMgcm9vdDMyOqpd6j3ZE2Omi9VU+zROUIGesftqc+Ngd9btGeTN3+maZWU1OmIuYmluZDA6ZDY6bGVuZ3RoaTk2MDBlMTE6cGllY2VzIHJvb3QzMjph2ncvL2cvMLx7LRQxCBSnCAURueBJP6BOV/PvbJwKS2VlZTU6ZmlsZXNsZDY6bGVuZ3RoaTQwOTYwZTQ6cGF0aGw1OmEuYmluZWVkNDphdHRyMTpwNjpsZW5ndGhpODE5MmU0OnBhdGhsNDoucGFkNDo4MTkyZWVkNjpsZW5ndGhpOTYwMGU0OnBhdGhsNTpiLmJpbmVlZDQ6YXR0cjE6cDY6bGVuZ3RoaTY3ODRlNDpwYXRobDQ6LnBhZDQ6Njc4NGVlZTEyOm1ldGEgdmVyc2lvbmkyZTQ6bmFtZTg6UmVmIFBhY2sxMjpwaWVjZSBsZW5ndGhpMTYzODRlNjpwaWVjZXM4MDqAy5xDDYDDCEZJ9l4Mol2rv/sbYoDLnEMNgMMIRkn2XgyiXau/+xtiQAfNxtZ9Xv6uA7QCUnDur29p11P4lqiQFLMpZiEaCCPhSpcOaD82MGUxMjpwaWVjZSBsYXllcnNkMzI6ql3qPdkTY6aL1VT7NE5QgZ6x+2pz42B31u0Z5M3f6Zo5Njqh8lnUNl7UMgw3fOJvXIxW3Nyaiee2Qb/Y6r+76shmVKHyWdQ2XtQyDDd84m9cjFbc3JqJ57ZBv9jqv7vqyGZU3EBKYT/trrVANFFLxlBfVrkzyqUlApm6fQlDd6UcqkZlZQ==",
        v1 = "6ff89e2dd38d8bf06a61b7648416557dc546f7cf",
        v2 = "e66dff9e47970de747707a1de9d007e4c177753014c0601e7c9bdd459c8aaa52",
        libtorrentName = "e66dff9e47970de747707a1de9d007e4c1777530",
    ),
    "v2" to Ref(
        base64 = "ZDEzOmNyZWF0aW9uIGRhdGVpMTcwMDAwMDAwMGU0OmluZm9kOTpmaWxlIHRyZWVkNTphLmJpbmQwOmQ2Omxlbmd0aGk0MDk2MGUxMTpwaWVjZXMgcm9vdDMyOqpd6j3ZE2Omi9VU+zROUIGesftqc+Ngd9btGeTN3+maZWU1OmIuYmluZDA6ZDY6bGVuZ3RoaTk2MDBlMTE6cGllY2VzIHJvb3QzMjph2ncvL2cvMLx7LRQxCBSnCAURueBJP6BOV/PvbJwKS2VlZTEyOm1ldGEgdmVyc2lvbmkyZTQ6bmFtZTg6UmVmIFBhY2sxMjpwaWVjZSBsZW5ndGhpMTYzODRlZTEyOnBpZWNlIGxheWVyc2QzMjqqXeo92RNjpovVVPs0TlCBnrH7anPjYHfW7Rnkzd/pmjk2OqHyWdQ2XtQyDDd84m9cjFbc3JqJ57ZBv9jqv7vqyGZUofJZ1DZe1DIMN3zib1yMVtzcmonntkG/2Oq/u+rIZlTcQEphP+2utUA0UUvGUF9WuTPKpSUCmbp9CUN3pRyqRmVl",
        v1 = null,
        v2 = "7074be6edcbbe5b870e4619bccc1d0a30def9e0a2ebd49dfd59ab05640c6b17b",
        libtorrentName = "7074be6edcbbe5b870e4619bccc1d0a30def9e0a",
    )
    )
    val v1: Ref = all.getValue("v1")
    val hybrid: Ref = all.getValue("hybrid")
    val v2: Ref = all.getValue("v2")
}
