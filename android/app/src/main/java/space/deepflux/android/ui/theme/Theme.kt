package space.deepflux.android.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

// Colors FIRST — top-level property initializers run in file order, and
// the scheme below reads these vals.

val WindowBase = Color(0xFF05070C)
val Panel = Color(0xFF080B12)
val Card = Color(0xFF0B1120)
val Border = Color(0xFF1A2A4A)
val Accent = Color(0xFF2A7ABF)
val AccentData = Color(0xFFA8EDFF)
val DimText = Color(0xFFA8B3C7)
val ErrorRed = Color(0xFFE5534B)
val OkGreen = Color(0xFF3FB950)
val WarnYellow = Color(0xFFD29922)
/**
 * The agent banner's own background (banner_agent.webp edges sample as
 * rgb(0,0,2)). The Agent empty state is painted with it so the banner's
 * edges dissolve — a #05070C pane behind it would show a "square".
 */
val BannerBackground = Color(0xFF000002)

/**
 * DeepFlux brand palette — deliberately the desktop's background ladder
 * (AGENTS.md "Background ladder DARKENED") so the app reads as the same
 * product across platforms:
 *
 *   #05070C window base · #080B12 panels · #0B1120 cards/hover ·
 *   #1A2A4A borders/selection · #2A7ABF accent · #A8EDFF accent data text.
 *
 * Always-dark by design (the desktop is dark-only); dynamic color is off
 * so brand identity wins.
 */
private val DeepFluxColors = darkColorScheme(
    primary = Accent,
    onPrimary = Color.White,
    primaryContainer = Card,
    onPrimaryContainer = Color.White,
    secondary = AccentData,
    onSecondary = Color(0xFF05070C),
    background = WindowBase,
    onBackground = Color.White,
    surface = Panel,
    onSurface = Color.White,
    surfaceVariant = Card,
    onSurfaceVariant = DimText,
    surfaceContainer = Panel,
    surfaceContainerHigh = Card,
    outline = Border,
    error = ErrorRed,
    onError = Color.White,
)

@Composable
fun DeepFluxTheme(content: @Composable () -> Unit) {
    // Always dark — the desktop app is dark-only; system setting ignored
    // on purpose (brand consistency across platforms).
    MaterialTheme(
        colorScheme = DeepFluxColors,
        typography = Typography(),
        content = content,
    )
}
