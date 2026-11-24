package space.deepflux.android.filecmd

import java.io.File
import java.nio.file.Files

/**
 * Recursive folder size, meant to run OFF the UI thread (fix 2026-11-23:
 * the Commander walked every folder synchronously while listing, on the main
 * thread — a large library froze the tab, and the storage line re-walked the
 * whole download tree on every recomposition).
 *
 * The desktop Commander's walker rules (gui/commander_tab.py), in miniature:
 *  - iterative — a deep tree never overflows the stack;
 *  - never crosses a symbolic link / junction;
 *  - stops early when the caller cancels ([cancelled]) — a pane that
 *    navigates away must not keep scanning the disk.
 */
object FolderSizer {

    /**
     * The real link test. A parameter so that a test can exercise the skip
     * on a Windows dev shell, which cannot create symbolic links. It comes
     * BEFORE `cancelled` on purpose: callers pass the cancel callback as a
     * trailing lambda, and a parameter added after it would quietly take
     * that lambda instead (caught by folderSizerStopsWhenCancelled).
     */
    private val DEFAULT_LINK_TEST: (File) -> Boolean = { Files.isSymbolicLink(it.toPath()) }

    /** Bytes used by the regular files under [dir] (not counting [dir] itself). */
    fun sizeOf(dir: File, isLink: (File) -> Boolean = DEFAULT_LINK_TEST, cancelled: () -> Boolean = { false }): Long {
        var total = 0L
        forEachFile(dir, isLink, cancelled) { total += it.length() }
        return total
    }

    /** Visits every regular file under [dir]; returns early once [cancelled] turns true. */
    fun forEachFile(dir: File, isLink: (File) -> Boolean = DEFAULT_LINK_TEST, cancelled: () -> Boolean = { false }, visit: (File) -> Unit) {
        if (!dir.isDirectory) return
        val stack = ArrayDeque<File>()
        stack.addLast(dir)
        while (stack.isNotEmpty()) {
            // Polled before EVERY directory: a boolean check next to a
            // listFiles() is free, and the first check must not be deferred
            // (a small tree is walked before a "every Nth step" poll fires).
            if (cancelled()) return
            val children = stack.removeLast().listFiles() ?: continue
            for (f in children) {
                if (f.isDirectory) {
                    if (isLink(f)) continue // never cross a link / junction
                    stack.addLast(f)
                } else if (f.isFile) {
                    visit(f)
                }
            }
        }
    }
}
