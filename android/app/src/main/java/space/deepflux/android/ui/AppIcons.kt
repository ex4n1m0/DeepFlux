package space.deepflux.android.ui

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.addPathNodes
import androidx.compose.ui.unit.dp

/**
 * Hand-built vector icons — the core material icon set (the app deliberately
 * depends only on material-icons-core) has no folder or globe, and the
 * extended icon pack would add megabytes to an unminified release build.
 * Path data is the standard material-design geometry.
 */
object AppIcons {

    /** Material "folder" glyph — the Commander tab. */
    val Folder: ImageVector by lazy {
        ImageVector.Builder(
            name = "DeepFluxFolder",
            defaultWidth = 24.dp,
            defaultHeight = 24.dp,
            viewportWidth = 24f,
            viewportHeight = 24f,
        ).apply {
            addPath(
                pathData = addPathNodes(
                    "M10 4H4c-1.1 0-1.99 0.9-1.99 2L2 18c0 1.1 0.9 2 2 2h16c1.1 0 2-0.9 2-2V8c0-1.1-0.9-2-2-2h-8l-2-2z"
                ),
                fill = SolidColor(Color.Black),
            )
        }.build()
    }

    /** Material "file download" — the Downloads tab. */
    val Download: ImageVector by lazy {
        ImageVector.Builder(
            name = "DeepFluxDownload",
            defaultWidth = 24.dp,
            defaultHeight = 24.dp,
            viewportWidth = 24f,
            viewportHeight = 24f,
        ).apply {
            addPath(
                pathData = addPathNodes("M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"),
                fill = SolidColor(Color.Black),
            )
        }.build()
    }

    /** Material "chat bubble" — the Agent tab. */
    val Chat: ImageVector by lazy {
        ImageVector.Builder(
            name = "DeepFluxChat",
            defaultWidth = 24.dp,
            defaultHeight = 24.dp,
            viewportWidth = 24f,
            viewportHeight = 24f,
        ).apply {
            addPath(
                pathData = addPathNodes(
                    "M20 2H4c-1.1 0-2 0.9-2 2v18l4-4h14c1.1 0 2-0.9 2-2V4c0-1.1-0.9-2-2-2z"
                ),
                fill = SolidColor(Color.Black),
            )
        }.build()
    }

    /** Material "tv" — the Live TV tab. */
    val Tv: ImageVector by lazy {
        ImageVector.Builder(
            name = "DeepFluxTv",
            defaultWidth = 24.dp,
            defaultHeight = 24.dp,
            viewportWidth = 24f,
            viewportHeight = 24f,
        ).apply {
            addPath(
                pathData = addPathNodes(
                    "M21 3H3c-1.1 0-2 0.9-2 2v12c0 1.1 0.9 2 2 2h5v2h8v-2h5c1.1 0 2-0.9 2-2V5c0-1.1-0.9-2-2-2zm0 14H3V5h18v12z"
                ),
                fill = SolidColor(Color.Black),
            )
        }.build()
    }

    /** Material "forum" — the chat Room tab (plan §13 #17). */
    val Forum: ImageVector by lazy {
        ImageVector.Builder(
            name = "DeepFluxForum",
            defaultWidth = 24.dp,
            defaultHeight = 24.dp,
            viewportWidth = 24f,
            viewportHeight = 24f,
        ).apply {
            addPath(
                pathData = addPathNodes(
                    "M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zM6 9h12v2H6V9zm8 5H6v-2h8v2zm4-6H6V6h12v2z"
                ),
                fill = SolidColor(Color.Black),
            )
        }.build()
    }

    /** Material "open_in_new" — hand over a file to its own app (the Commander's non-media rows). */
    val OpenInNew: ImageVector by lazy {
        ImageVector.Builder(
            name = "DeepFluxOpenInNew",
            defaultWidth = 24.dp,
            defaultHeight = 24.dp,
            viewportWidth = 24f,
            viewportHeight = 24f,
        ).apply {
            addPath(
                pathData = addPathNodes(
                    "M19 19H5V5h7V3H5c-1.11 0-2 0.9-2 2v14c0 1.1 0.89 2 2 2h14c1.1 0 2-0.9 2-2v-7h-2v7z" +
                        "M14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7z"
                ),
                fill = SolidColor(Color.Black),
            )
        }.build()
    }

    /** Material "mic" — voice input in the chat. */
    val Mic: ImageVector by lazy {
        ImageVector.Builder(
            name = "DeepFluxMic",
            defaultWidth = 24.dp,
            defaultHeight = 24.dp,
            viewportWidth = 24f,
            viewportHeight = 24f,
        ).apply {
            addPath(
                pathData = addPathNodes(
                    "M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z" +
                        "m5.91-3c-.49 0-.9.36-.98.85C16.52 14.2 14.47 16 12 16s-4.52-1.8-4.93-4.15" +
                        "c-.08-.49-.49-.85-.98-.85-.61 0-1.09.54-1 1.14.49 3 2.89 5.35 5.91 5.78V20" +
                        "c0 .55.45 1 1 1s1-.45 1-1v-2.08c3.02-.43 5.42-2.78 5.91-5.78.1-.6-.39-1.14-1-1.14z"
                ),
                fill = SolidColor(Color.Black),
            )
        }.build()
    }
}
