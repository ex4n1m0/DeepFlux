package space.deepflux.android.room

import android.content.Context
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.ConcurrentHashMap

/**
 * The OnlyHumans room controller — the Kotlin port of the desktop's
 * OhRoomController (ircmgr/oh_room.py): join by room word, host-or-seat,
 * the 2–3.5 s drain loop, sealed chat frames, host-side admission with key
 * delivery + member fan-out, rotation, and leave. HUB and desktop clients
 * interoperate byte-exactly through [RoomCrypto].
 *
 * Roles: "left" → "connecting" → "active" (host or member).
 */
class OhRoom(context: Context) {

    enum class Role { LEFT, CONNECTING, ACTIVE }

    data class RoomMessage(val ts: Long, val nick: String, val text: String, val own: Boolean)

    data class UiState(
        val role: Role = Role.LEFT,
        val nick: String = "",
        val word: String = "",
        val hosting: Boolean = false,
        val epoch: Long = 0,
        val sealedRoom: Boolean = false,
        val members: List<String> = emptyList(),
        val messages: List<RoomMessage> = emptyList(),
        val note: String = "",
        val roomHex: String = "",
    )

    private val appContext = context.applicationContext
    private val hub = Hub()
    private val identity: RoomIdentity.Identity = RoomIdentity.loadOrCreate(appContext.filesDir)
    private val history = mutableMapOf<String, RoomHistory>()

    private val lock = Any()
    private var role = Role.LEFT
    private var nick = ""
    private var word = ""
    private var pending = false
    private var room: RoomCrypto.Generation? = null
    private var egk: ByteArray? = null
    private var roomHex = ""
    private var isHost = false
    private var hostId = ""
    private var members = linkedMapOf<String, String>()
    private var mySeq = 0L
    private val seenSeq = ConcurrentHashMap<String, Long>()
    private var lastJoinTry = 0.0
    private var lastReg = 0.0
    private var lastJoinMail = 0.0
    private var lastHostContact = 0.0
    private var lastRefresh = 0.0
    private val egkCache = HashMap<String, ByteArray>()

    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    @Volatile private var stop = false
    private var thread: Thread? = null

    /** Mail pushes must NEVER run on the caller's (UI) thread — the seal is
     * local but the push is HTTP (caught live: NetworkOnMainThreadException
     * silently ate outgoing chat while the local echo rendered). */
    private val io = java.util.concurrent.Executors.newSingleThreadExecutor { r ->
        Thread(r, "oh-room-io").apply { isDaemon = true }
    }

    // ------------------------------------------------------------------ public api

    fun join(nickRaw: String, wordRaw: String): Boolean {
        val n = sanitizeName(nickRaw.ifBlank { "deepfluxuser" + (1000 + (Math.random() * 9000).toInt()) })
        val w = wordRaw.trim().lowercase().take(64)
        if (n.isEmpty() || w.isEmpty()) {
            note("Enter a name and a room word first.")
            return false
        }
        synchronized(lock) {
            if (role != Role.LEFT) return false
            role = Role.CONNECTING
            nick = n
            word = w
            pending = true
        }
        saveConfig()
        publish(note = null)
        startLoop()
        return true
    }

    fun sendMessage(text: String): Boolean {
        val body = text.trim()
        if (body.isEmpty()) return false
        val roomNow: RoomCrypto.Generation
        val membersNow: Map<String, String>
        synchronized(lock) {
            roomNow = room ?: return false
            membersNow = members.toMap()
        }
        val seq = bumpSeq()
        val frame = roomNow.seal(identity.peerId, seq, RoomCrypto.KIND_CHAT_BYTES, body.toByteArray(Charsets.UTF_8))
        recordMessage(nick, body, own = true)
        fanOut(JSONObject().put("Chat", JSONObject().put("frame", frame)), membersNow)
        return true
    }

    fun rename(nameRaw: String): Boolean {
        val name = sanitizeName(nameRaw)
        if (name.isEmpty()) return false
        var host = false
        var seated = false
        synchronized(lock) {
            nick = name
            seated = room != null
            host = isHost
        }
        saveConfig()
        if (!seated) return true
        if (host) {
            synchronized(lock) { members[identity.peerId] = name }
            broadcastMembers()
        } else {
            sendJoinMail()
        }
        publish(note = null)
        return true
    }

    /** Host-only: mint the next key — the room closes to newcomers. */
    fun rotate(): Boolean {
        val roomNow: RoomCrypto.Generation
        val membersNow: Map<String, String>
        synchronized(lock) {
            roomNow = room ?: return false
            if (!isHost) return false
            membersNow = members.toMap()
        }
        val nextKey = RoomCrypto.randomBytes(32)
        val nextEpoch = roomNow.epoch + 1
        val body = JSONObject().put("next_epoch", nextEpoch).put("next_key_b64", RoomCrypto.b64(nextKey))
        val seq = bumpSeq()
        val frame = roomNow.seal(identity.peerId, seq, RoomCrypto.KIND_ROTATE_BYTES, body.toString().toByteArray())
        fanOut(JSONObject().put("Rotate", JSONObject().put("frame", frame)), membersNow)
        roomNow.applyRotation(nextEpoch, nextKey)
        note("The room key was rotated (generation $nextEpoch) — the room is closed to newcomers.")
        publish(note = null)
        return true
    }

    fun leave() = stopRoom("You left the room.")

    fun shutdown() {
        stopRoom(null)
        stop = true
    }

    fun loadConfig(): Pair<String, String> {
        val f = File(File(appContext.filesDir, "rooms"), "config.json")
        if (!f.isFile) return "" to ""
        return runCatching {
            val o = JSONObject(f.readText())
            o.optString("nick", "") to o.optString("word", "")
        }.getOrDefault("" to "")
    }

    private fun saveConfig() {
        runCatching {
            val dir = File(appContext.filesDir, "rooms")
            dir.mkdirs()
            File(dir, "config.json").writeText(JSONObject().put("nick", nick).put("word", word).toString())
        }
    }

    // ------------------------------------------------------------------ loop

    private fun startLoop() {
        if (thread?.isAlive == true) return
        stop = false
        thread = Thread({ run() }, "oh-room-loop").apply { isDaemon = true }.also { it.start() }
    }

    private fun run() {
        var nextDrain = 0.0
        var nextCycle = 0.0
        while (!stop) {
            val now = System.currentTimeMillis() / 1000.0
            val seated: Boolean
            val active: Boolean
            synchronized(lock) {
                seated = room != null
                active = role != Role.LEFT
            }
            if (active) {
                if (now >= nextDrain) {
                    safe { drainPass() }
                    nextDrain = System.currentTimeMillis() / 1000.0 + if (seated) DRAIN_SEATED else DRAIN_UNSEATED
                }
                if (now >= nextCycle) {
                    safe { cyclePass() }
                    nextCycle = System.currentTimeMillis() / 1000.0 + CYCLE
                }
            }
            Thread.sleep(500)
        }
    }

    private fun cyclePass() {
        val roomNow: RoomCrypto.Generation?
        synchronized(lock) { roomNow = room }
        if (roomNow == null) {
            if (pending) joinPass()
            return
        }
        val now = System.currentTimeMillis() / 1000.0
        if (isHost) {
            if (now - lastRefresh > HOST_REFRESH) hostRefresh()
        } else if (now - lastHostContact > HOST_SILENT) {
            rediscover()
        }
        if (now - lastReg > REG_EVERY) {
            lastReg = now
            safe { hub.reg(identity.peerId, identity.pubB64) { identity.sign(it) } }
        }
    }

    // ------------------------------------------------------------------ join sequence

    private fun joinPass() {
        synchronized(lock) {
            if (!pending || role == Role.LEFT) return
            val now = System.currentTimeMillis() / 1000.0
            if (now - lastJoinTry < REJOIN_RETRY && roomHex.isNotEmpty()) return
            lastJoinTry = now
        }
        val gk = hub.fetchGk().second
        val eg = synchronized(lock) {
            egkCache.getOrPut(word) { RoomCrypto.effectiveGk(gk, word) }
        }
        val hex = RoomCrypto.globalRoomHex(eg)
        synchronized(lock) {
            egk = eg
            roomHex = hex
        }
        val rec: JSONObject?
        try {
            // The hub rate-limits reg to ~1/30 s per peer and a 429 must not
            // abort the pass — the registration from moments ago still stands.
            if (System.currentTimeMillis() / 1000.0 - lastReg > 35.0) {
                try {
                    hub.reg(identity.peerId, identity.pubB64) { identity.sign(it) }
                    lastReg = System.currentTimeMillis() / 1000.0
                } catch (e: RoomCrypto.RoomError) {
                    if (!e.message.orEmpty().contains("429")) throw e
                    lastReg = System.currentTimeMillis() / 1000.0 // back off
                }
            }
            rec = hub.lookupRoom(hex)
        } catch (e: Exception) {
            Log.w(TAG, "room first contact failed: ${e.message}")
            return
        }
        if (rec != null && rec.optString("host_peer_id") == identity.peerId) {
            tryFound(forceRegister = true)
            return
        }
        if (rec != null) {
            seekSeat(rec.optString("host_peer_id", ""))
            return
        }
        tryFound()
    }

    private fun tryFound(forceRegister: Boolean = false) {
        val hex: String
        val eg: ByteArray?
        val n: String
        synchronized(lock) {
            hex = roomHex
            eg = egk
            n = nick
        }
        if (hex.isEmpty() || eg == null) return
        val won = safeBool { hub.registerRoom(hex, identity.peerId, identity.pubB64) { identity.sign(it) } } ?: return
        if (!won && !forceRegister) {
            val rec = safeBool { hub.lookupRoom(hex) } as? JSONObject ?: run {
                // null means 404 (no record) or failure — treat 404 as "retry later"
                return
            }
            if (rec.optString("host_peer_id") != identity.peerId) {
                seekSeat(rec.optString("host_peer_id", ""))
                return
            }
        }
        synchronized(lock) {
            if (!pending || role == Role.LEFT) return
            isHost = true
            hostId = identity.peerId
            room = RoomCrypto.Generation(hexToBytes(hex), 1, RoomCrypto.randomBytes(32))
            members = linkedMapOf(identity.peerId to n)
            mySeq = RoomCrypto.nowMs()
            seenSeq.clear()
            role = Role.ACTIVE
            lastRefresh = System.currentTimeMillis() / 1000.0
        }
        note("You opened the room — you are the host.")
        publish(note = null)
    }

    private fun seekSeat(host: String) {
        synchronized(lock) {
            if (!pending || role == Role.LEFT) return
            isHost = false
            hostId = host
            lastJoinMail = System.currentTimeMillis() / 1000.0
        }
        sendJoinMail()
    }

    private fun sendJoinMail() {
        val hex: String
        val eg: ByteArray?
        val host: String
        val n: String
        synchronized(lock) {
            hex = roomHex; eg = egk; host = hostId; n = nick
        }
        if (hex.isEmpty() || eg == null || host.isEmpty()) return
        val nonce = RoomCrypto.randomBytes(16)
        val env = JSONObject().put(
            "Join",
            JSONObject()
                .put("room_id_hex", hex)
                .put("guest_id", identity.peerId)
                .put("guest_nonce_b64", RoomCrypto.b64(nonce))
                .put("guest_proof_b64", RoomCrypto.b64(RoomCrypto.admissionProof(eg, identity.peerId, nonce)))
                .put("name", n),
        )
        safe { hub.mailPushBatch(identity.peerId, identity.pubB64, { identity.sign(it) }, listOf(host to env)) }
    }

    private fun hostRefresh() {
        lastRefresh = System.currentTimeMillis() / 1000.0
        val hex: String
        synchronized(lock) { hex = roomHex }
        val won = safeBool { hub.registerRoom(hex, identity.peerId, identity.pubB64) { identity.sign(it) } } ?: return
        if (won) return
        val rec = safeBool { hub.lookupRoom(hex) } as? JSONObject ?: return
        if (rec.optString("host_peer_id") != identity.peerId) {
            synchronized(lock) { isHost = false }
            note("Another live host holds the room now — staying as a member.")
        }
    }

    private fun rediscover() {
        val hex: String
        val host: String
        val membersNow: Map<String, String>
        synchronized(lock) {
            hex = roomHex; host = hostId; membersNow = members.toMap()
        }
        val rec = safeBool { hub.lookupRoom(hex) } as? JSONObject
        if (rec == null) {
            val now = System.currentTimeMillis() / 1000.0
            if (now - lastJoinTry < REJOIN_RETRY) return
            lastJoinTry = now
            val won = safeBool { hub.registerRoom(hex, identity.peerId, identity.pubB64) { identity.sign(it) } } ?: return
            if (won == true) {
                synchronized(lock) {
                    isHost = true
                    hostId = identity.peerId
                    lastRefresh = System.currentTimeMillis() / 1000.0
                }
                note("The host left — this device keeps the room open.")
            }
            return
        }
        val holder = rec.optString("host_peer_id", "")
        val now = System.currentTimeMillis() / 1000.0
        val healthy = holder == host && membersNow.containsKey(holder) && now - lastHostContact < 300.0
        if (healthy) return
        if (now - lastJoinMail < JOIN_MAIL_EVERY) return
        seekSeat(holder)
    }

    // ------------------------------------------------------------------ inbox

    private fun drainPass() {
        val items = try {
            hub.mailDrain(identity.peerId) { identity.sign(it) }
        } catch (e: Exception) {
            Log.w(TAG, "drain failed: ${e.message}")
            return
        }

        for (item in items) {
            val sender = try {
                verifyMailItem(item)
            } catch (e: Exception) {
                continue
            }
            val env = runCatching { JSONObject(item.optString("env_json", "")) }.getOrNull() ?: continue
            safe { handle(sender, env) }
        }
    }

    private fun handle(sender: String, env: JSONObject) {
        when {
            env.has("KeyDelivery") -> handleKeyDelivery(sender, env.optJSONObject("KeyDelivery") ?: return)
            env.has("Chat") -> handleChat(sender, env.optJSONObject("Chat") ?: return)
            env.has("Members") -> handleMembers(sender, env.optJSONObject("Members") ?: return)
            env.has("Rotate") -> handleRotate(sender, env.optJSONObject("Rotate") ?: return)
            env.has("Join") -> hostHandleJoin(sender, env.optJSONObject("Join") ?: return)
            env.has("Leave") -> handleLeave(sender, env.optJSONObject("Leave") ?: return)
            env.has("Error") -> {
                val message = env.optJSONObject("Error")?.optString("message", "").orEmpty()
                if (message.isNotEmpty()) note(message)
            }
        }
    }

    private fun handleKeyDelivery(sender: String, kd: JSONObject) {
        var eg: ByteArray? = null
        var hex = ""
        var roomEpoch = 0L
        synchronized(lock) {
            if (isHost || sender != hostId) {
                Log.w(TAG, "kd drop: isHost=$isHost sender=${sender.take(10)} hostId=${hostId.take(10)}")
                return
            }
            if (kd.optString("room_id_hex") != roomHex) {
                Log.w(TAG, "kd drop: room ${kd.optString("room_id_hex").take(8)} != ${roomHex.take(8)}")
                return
            }
            val epoch = kd.optLong("epoch", -1)
            if (epoch < 1) return
            room?.let { if (epoch < it.epoch) return }
            eg = egk
            hex = roomHex
            roomEpoch = epoch
        }
        val key = RoomCrypto.openRoomKey(
            eg ?: return,
            hexToBytes(hex),
            roomEpoch,
            identity.peerId,
            RoomCrypto.unb64(kd.optString("key_ct_b64", "")),
        )
        val memberList = linkedMapOf<String, String>()
        val arr = kd.optJSONArray("members") ?: JSONArray()
        for (i in 0 until arr.length()) {
            val m = arr.optJSONObject(i) ?: continue
            memberList[m.optString("peer")] = m.optString("name")
        }
        val firstSeat: Boolean
        synchronized(lock) {
            if (!pending || role == Role.LEFT) return
            firstSeat = role == Role.CONNECTING
            room = RoomCrypto.Generation(hexToBytes(hex), roomEpoch, key)
            members = memberList
            mySeq = RoomCrypto.nowMs()
            seenSeq.clear()
            lastHostContact = System.currentTimeMillis() / 1000.0
            role = Role.ACTIVE
        }
        if (firstSeat) {
            note("You joined the room.")
            loadHistory(hex)
        } else {
            publish(note = null) // silent re-seat: adopt the fresh key/member table
        }
    }

    private fun handleChat(sender: String, chat: JSONObject) {
        val frame = chat.optJSONObject("frame") ?: return
        var roomOpt: RoomCrypto.Generation? = null
        var host = ""
        synchronized(lock) {
            roomOpt = room
            host = hostId
        }
        val roomGen = roomOpt ?: return
        if (frame.optLong("epoch", 0) > roomGen.epoch) {
            sendJoinMail() // missed a rotation: ask for a re-seat
            return
        }
        val body = String(roomGen.open(frame, RoomCrypto.KIND_CHAT_BYTES), Charsets.UTF_8)
        if (sender == host) lastHostContact = System.currentTimeMillis() / 1000.0
        val who = frame.optString("sender", "")
        val seq = frame.optLong("seq", 0)
        if (seq <= (seenSeq[who] ?: 0L)) return
        seenSeq[who] = seq
        val name = synchronized(lock) { members[who] } ?: who.take(10)
        recordMessage(name, body, own = false)
    }

    private fun handleMembers(sender: String, membersEnv: JSONObject) {
        val frame = membersEnv.optJSONObject("frame") ?: return
        var roomOpt: RoomCrypto.Generation? = null
        var host = ""
        synchronized(lock) {
            roomOpt = room
            host = hostId
        }
        val roomGen = roomOpt ?: return
        if (frame.optLong("epoch", 0) > roomGen.epoch) {
            sendJoinMail()
            return
        }
        val body = String(roomGen.open(frame, RoomCrypto.KIND_MEMBERS_BYTES), Charsets.UTF_8)
        if (sender == host) lastHostContact = System.currentTimeMillis() / 1000.0
        val listing = runCatching { JSONObject(body).optJSONArray("members") }.getOrNull() ?: return
        val fresh = linkedMapOf<String, String>()
        for (i in 0 until listing.length()) {
            val m = listing.optJSONObject(i) ?: continue
            fresh[m.optString("peer")] = m.optString("name")
        }
        synchronized(lock) { members = fresh }
        publish(note = null)
    }

    private fun handleRotate(sender: String, rotateEnv: JSONObject) {
        val frame = rotateEnv.optJSONObject("frame") ?: return
        var roomOpt: RoomCrypto.Generation? = null
        synchronized(lock) {
            if (isHost || sender != hostId) return
            roomOpt = room
        }
        val roomGen = roomOpt ?: return
        val body = String(roomGen.open(frame, RoomCrypto.KIND_ROTATE_BYTES), Charsets.UTF_8)
        val secret = JSONObject(body)
        roomGen.applyRotation(secret.optLong("next_epoch"), RoomCrypto.unb64(secret.optString("next_key_b64")))
        lastHostContact = System.currentTimeMillis() / 1000.0
        note("The room key was rotated (generation ${roomGen.epoch}) — the room is closed to newcomers.")
        publish(note = null)
    }

    private fun handleLeave(sender: String, leave: JSONObject) {
        val name: String?
        synchronized(lock) {
            if (!isHost) return
            if (leave.optString("room_id_hex") != roomHex) return
            name = members.remove(sender)
        }
        if (name == null) return
        note("$name left the room.")
        broadcastMembers()
    }

    private fun hostHandleJoin(sender: String, j: JSONObject) {
        var roomOpt: RoomCrypto.Generation? = null
        var egOpt: ByteArray? = null
        var hex = ""
        var host = false
        synchronized(lock) {
            roomOpt = room; egOpt = egk; hex = roomHex; host = isHost
        }
        val roomGen = roomOpt ?: return
        val eg = egOpt ?: return
        if (!host) return
        if (j.optString("room_id_hex") != hex || j.optString("guest_id") != sender) return
        // The first rotation seals the room: a valid GK proof no longer mints
        // a seat from epoch 2 onward.
        if (roomGen.epoch > 1 && !synchronized(lock) { members.containsKey(sender) }) {
            replyError(sender, "the room is sealed (key rotated) — ask a member for a new room word")
            return
        }
        val nonce = RoomCrypto.unb64(j.optString("guest_nonce_b64", ""))
        val expect = RoomCrypto.admissionProof(eg, sender, nonce)
        val got = RoomCrypto.unb64(j.optString("guest_proof_b64", ""))
        if (!MessageDigestIsEqual(expect, got)) return // proof failed
        val name = sanitizeName(j.optString("name", "")).ifBlank { sender.take(10) }
        val isNew: Boolean
        synchronized(lock) {
            isNew = !members.containsKey(sender)
            members[sender] = name
        }
        if (isNew) note("$name joined the room.")
        val kd = JSONObject().put(
            "KeyDelivery",
            JSONObject()
                .put("room_id_hex", hex)
                .put("epoch", roomGen.epoch)
                .put("key_ct_b64", RoomCrypto.b64(RoomCrypto.sealRoomKey(eg, hexToBytes(hex), roomGen.epoch, sender, roomGen.key)))
                .put(
                    "members",
                    JSONArray().apply {
                        synchronized(lock) {
                            members.forEach { (p, n) -> put(JSONObject().put("peer", p).put("name", n)) }
                        }
                    },
                ),
        )
        val batch = mutableListOf(sender to kd)
        sealMembersFrame()?.let { mf ->
            synchronized(lock) {
                members.keys.forEach { peer ->
                    if (peer != identity.peerId) batch.add(peer to JSONObject().put("Members", JSONObject().put("frame", mf)))
                }
            }
        }
        safe { hub.mailPushBatch(identity.peerId, identity.pubB64, { identity.sign(it) }, batch) }
        publish(note = null)
    }

    private fun replyError(to: String, message: String) {
        safe {
            hub.mailPushBatch(
                identity.peerId, identity.pubB64, { identity.sign(it) },
                listOf(to to JSONObject().put("Error", JSONObject().put("message", message))),
            )
        }
    }

    // ------------------------------------------------------------------ host helpers

    private fun sealMembersFrame(): JSONObject? {
        val roomNow: RoomCrypto.Generation?
        synchronized(lock) { roomNow = room }
        roomNow ?: return null
        val seq = bumpSeq()
        val body = synchronized(lock) {
            JSONObject().put(
                "members",
                JSONArray().apply { members.forEach { (p, n) -> put(JSONObject().put("peer", p).put("name", n)) } },
            ).toString().toByteArray(Charsets.UTF_8)
        }
        return roomNow.seal(identity.peerId, seq, RoomCrypto.KIND_MEMBERS_BYTES, body)
    }

    private fun broadcastMembers() {
        val frame = sealMembersFrame() ?: return
        val membersNow: Map<String, String>
        synchronized(lock) { membersNow = members.toMap() }
        fanOut(JSONObject().put("Members", JSONObject().put("frame", frame)), membersNow)
    }

    private fun fanOut(env: JSONObject, membersNow: Map<String, String>) {
        val batch = membersNow.keys.filter { it != identity.peerId }.map { it to env }
        if (batch.isEmpty()) return
        io.execute { safe { hub.mailPushBatch(identity.peerId, identity.pubB64, { identity.sign(it) }, batch) } }
    }

    private fun bumpSeq(): Long = synchronized(lock) {
        mySeq = maxOf(mySeq + 1, RoomCrypto.nowMs())
        mySeq
    }

    // ------------------------------------------------------------------ state

    private fun stopRoom(detail: String?) {
        val membersNow: Map<String, String>
        var seated = false
        var hex = ""
        synchronized(lock) {
            seated = room != null
            membersNow = members.toMap()
            hex = roomHex
            pending = false
            role = Role.LEFT
            room = null
            members = linkedMapOf()
            isHost = false
            hostId = ""
            egk = null
            roomHex = ""
        }
        if (seated) {
            val leaveEnv = JSONObject().put("Leave", JSONObject().put("room_id_hex", hex))
            io.execute {
                safe {
                    hub.mailPushBatch(
                        identity.peerId, identity.pubB64, { identity.sign(it) },
                        membersNow.keys.filter { it != identity.peerId }.map { it to leaveEnv },
                    )
                }
            }
        }
        if (detail != null) note(detail)
        publish(note = null)
    }

    private fun recordMessage(n: String, text: String, own: Boolean) {
        val ts = System.currentTimeMillis()
        val hex = synchronized(lock) { roomHex }
        if (hex.isNotEmpty()) historyFor(hex).append(ts, n, text)
        val cur = _state.value
        _state.value = cur.copy(messages = (cur.messages + RoomMessage(ts, n, text, own)).takeLast(500))
    }

    private fun loadHistory(hex: String) {
        val rows = historyFor(hex).load()
        val msgs = rows.map { RoomMessage(it.first, it.second, it.third, own = false) }
        _state.value = _state.value.copy(messages = msgs)
    }

    private fun historyFor(hex: String): RoomHistory = history.getOrPut(hex) { RoomHistory(appContext.filesDir, hex) }

    private fun note(text: String) {
        Log.i(TAG, text)
        _state.value = _state.value.copy(note = text)
    }

    private fun publish(note: String?) {
        val st = synchronized(lock) {
            UiState(
                role = role,
                nick = nick,
                word = word,
                hosting = isHost,
                epoch = room?.epoch ?: 0,
                sealedRoom = (room?.epoch ?: 0) > 1,
                members = members.values.toList(),
                roomHex = roomHex,
            )
        }
        _state.value = st.copy(messages = _state.value.messages, note = note ?: _state.value.note)
    }

    private fun MessageDigestIsEqual(a: ByteArray, b: ByteArray): Boolean =
        java.security.MessageDigest.isEqual(a, b)

    private fun safe(block: () -> Unit) {
        try {
            block()
        } catch (e: Exception) {
            Log.w(TAG, "room pass failed: ${e.message}", e)
        }
    }

    private inline fun <T> safeBool(block: () -> T): T? = try {
        block()
    } catch (e: Exception) {
        Log.w(TAG, "room op failed: ${e.message}")
        null
    }

    companion object {
        private const val TAG = "OhRoom"
        private const val DRAIN_SEATED = 3.5
        private const val DRAIN_UNSEATED = 2.0
        private const val CYCLE = 5.0
        private const val HOST_REFRESH = 45.0
        private const val HOST_SILENT = 45.0
        private const val REJOIN_RETRY = 10.0
        private const val REG_EVERY = 31.0
        private const val JOIN_MAIL_EVERY = 15.0

        fun sanitizeName(name: String): String =
            name.replace(Regex("\\s+"), " ").trim().take(32)

        fun hexToBytes(hex: String): ByteArray = ByteArray(hex.length / 2) { i ->
            hex.substring(i * 2, i * 2 + 2).toInt(16).toByte()
        }
    }
}

/** Recipient-side mail verification: the included key must derive the
 *  sender's peer id and the signature covers the exact envelope JSON. */
@Throws(RoomCrypto.RoomError::class)
fun verifyMailItem(item: JSONObject): String {
    val pubRaw = RoomCrypto.ed25519RawFromProtobuf(RoomCrypto.unb64(item.optString("public_key_b64", "")))
    val sender = item.optString("from", "")
    if (RoomCrypto.peerIdFromPublic(pubRaw) != sender) throw RoomCrypto.RoomError("mail item: key does not derive sender")
    val ok = RoomIdentity.verify(
        pubRaw,
        RoomCrypto.unb64(item.optString("sig_b64", "")),
        Hub.mailCanonical(sender, item.optString("to", ""), item.optLong("ts_ms", 0), item.optString("env_json", "")),
    )
    if (!ok) throw RoomCrypto.RoomError("mail item: signature failed")
    return sender
}
