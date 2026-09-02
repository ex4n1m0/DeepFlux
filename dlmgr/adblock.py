"""Ad-block request interceptor for the internal browser.

Uses QWebEngineUrlRequestInterceptor to block requests to known ad,
tracker, and analytics domains before they reach Chromium's network
stack. A curated built-in blocklist is used (no external downloads
required), keeping the binary small and the startup fast.

The interceptor is installed on the QWebEngineProfile and can be
toggled on/off at runtime via set_enabled().
"""
from __future__ import annotations

import logging
import re
from typing import Set

from PySide6.QtCore import Signal
from PySide6.QtWebEngineCore import QWebEngineUrlRequestInterceptor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Built-in ad/tracker/analytics domain blocklist.
#
# This is a curated list of the most common ad networks, tracking pixels,
# and analytics providers. It's intentionally compact (a few hundred domains)
# rather than the full EasyList (~70k rules) to keep memory and startup
# time low. It catches the vast majority of intrusive ads and trackers.
#
# Domains are matched as suffixes: "doubleclick.net" blocks
# "ad.doubleclick.net", "stats.doubleclick.net", etc.
# ---------------------------------------------------------------------------
_BLOCKED_DOMAINS: Set[str] = {
    # --- Major ad networks ---
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "google-analytics.com",
    "googletagmanager.com",
    "googletagservices.com",
    "adservice.google.com",
    "adsense.com",
    "2mdn.net",
    "adnxs.com",
    "adsystem.com",
    "advertising.com",
    "adcolony.com",
    "admob.com",
    "applovin.com",
    "chartboost.com",
    "unityads.unity3d.com",
    "vungle.com",
    "inmobi.com",
    "mopub.com",
    "tapjoy.com",
    "adsrvr.org",
    "adform.net",
    "adtech.de",
    "adtech.com",
    "yieldlab.net",
    "pubmatic.com",
    "rubiconproject.com",
    "openx.net",
    "criteo.com",
    "criteo.net",
    "taboola.com",
    "outbrain.com",
    "mgid.com",
    "revcontent.com",
    "content.ad",
    "adsterra.com",
    "propellerads.com",
    "popads.net",
    "popcash.net",
    "adcash.com",
    "hilltopads.com",
    "adsterra.com",
    "exoclick.com",
    "juicyads.com",
    "trafficjunky.com",
    "trafficjunky.net",
    "etahub.com",
    "ero-advertising.com",
    "exosrv.com",
    "ads.exoclick.com",
    "mainexosrv.com",
    "tsyndicate.com",
    "tsyndicate.com",
    "ads.tsyndicate.com",

    # --- Facebook / Meta ---
    "connect.facebook.net",
    "facebook.net",
    "fbcdn.net",

    # --- Twitter / X ---
    "ads-twitter.com",
    "analytics.twitter.com",

    # --- Amazon ads ---
    "amazon-adsystem.com",

    # --- Microsoft ads ---
    "bat.bing.com",

    # --- Analytics & tracking ---
    "mixpanel.com",
    "segment.io",
    "segment.com",
    "amplitude.com",
    "hotjar.com",
    "mouseflow.com",
    "fullstory.com",
    "logrocket.com",
    "sentry.io",
    "newrelic.com",
    "nr-data.net",
    "quantserve.com",
    "quantcount.com",
    "scorecardresearch.com",
    "comscore.com",
    "clarity.ms",
    "matomo.org",
    "statcounter.com",
    "clicky.com",
    "piwik.org",
    "goatcounter.com",
    "plausible.io",
    "fathom.io",
    "umami.is",

    # --- Tag managers & pixels ---
    "tagmanager.google.com",
    "pixel.facebook.com",
    "pixel.adsafeprotected.com",
    "adsafeprotected.com",
    "ipredictive.com",
    "d1lxhc4jwstoyr.cloudfront.net",

    # --- Common ad-serving CDNs ---
    "adserver.com",
    "adserver.yahoo.com",
    "ads.yahoo.com",
    "ads.yimg.com",
    "cdn.adsafeprotected.com",
    "s.amazon-adsystem.com",
    "aax.amazon-adsystem.com",
    "c.amazon-adsystem.com",

    # --- Popunder / redirect ad networks ---
    "popunder.net",
    "popunder.ru",
    "onclickads.net",
    "onclickperformance.com",
    "onclickprediction.com",
    "propellerads.com",
    "adsprophet.com",
    "adcash.com",
    "ad-maven.com",
    "admaven.com",

    # --- Mobile ad SDKs ---
    "applovin.com",
    "applovinad.com",
    "chartboost.com",
    "fyber.com",
    "ironsrc.com",
    "mintegral.com",
    "startapp.com",
    "supersonic.com",
    "vungle.com",

    # --- Crypto mining / unwanted scripts ---
    "coinhive.com",
    "coin-hive.com",
    "jsecoin.com",
    "cryptoloot.com",
    "coinerra.com",
    "deepmine.io",

    # --- Adult ad networks (common on MissAV-type sites) ---
    "exoclick.com",
    "juicyads.com",
    "trafficjunky.com",
    "ero-advertising.com",
    "adsterra.com",
    "propellerads.com",
    "hilltopads.com",
    "popads.net",
    "popcash.net",
    "tsyndicate.com",
    "etahub.com",
    "ads.tsyndicate.com",
    "engine.addroplet.com",
    "addroplet.com",
    "a.realsrv.com",
    "realsrv.com",
    "syndication.realsrv.com",
    "js.aslsrv.com",
    "aslsrv.com",
    "a.magsrv.com",
    "magsrv.com",
    "engine.phnxtag.com",
    "phnxtag.com",

    # --- Misc trackers ---
    "branch.io",
    "appsflyer.com",
    "kochava.com",
    "adjust.com",
    "adjust.io",
    "appsflyersdk.com",
    "singular.net",
    "tenjin.io",
    "swrve.com",
    "leanplum.com",
    "braze.com",
    "localytics.com",
    "count.ly",
    "countly.com",
    "flurry.com",
    "crashlytics.com",
    "fabric.io",
    "mobileanalytics.us-east-1.amazonaws.com",

    # --- Cookie sync / RTB ---
    "3lift.com",
    "adsystem.com",
    "bidswitch.net",
    "casalemedia.com",
    "demdex.net",
    "dmtracker.com",
    "eyeota.net",
    "id5-sync.com",
    "krxd.net",
    "liadm.com",
    "lotame.com",
    "mathtag.com",
    "mdotlabs.com",
    "media.net",
    "mediavine.com",
    "moatads.com",
    "moatpixel.com",
    "nuggad.net",
    "oneadserver.com",
    "pippio.com",
    "rlcdn.com",
    "rubiconproject.com",
    "sekindo.com",
    "sharethrough.com",
    "simpli.fi",
    "sonobi.com",
    "sync.tidaltv.com",
    "tidaltv.com",
    "tribalfusion.com",
    "turn.com",
    "yieldmo.com",
    "yldbt.com",
    "zedo.com",
    "adinterax.com",
    "admixer.net",
    "admixer.com",
}

# Resource types that should NEVER be blocked even when ad-block is on,
# to avoid breaking page layout/functionality.
_NEVER_BLOCK_EXTENSIONS = {
    ".css", ".woff", ".woff2", ".ttf", ".otf", ".eot",
}

class AdBlockInterceptor(QWebEngineUrlRequestInterceptor):
    """Request interceptor that blocks ad/tracker domains.

    Installed on a QWebEngineProfile via setUrlRequestInterceptor().
    Toggle at runtime with set_enabled().
    """

    blockedRequest = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._enabled: bool = False
        self._blocked_domains: Set[str] = set(_BLOCKED_DOMAINS)
        self._allowed_sites: Set[str] = set()
        # Compile a regex for never-block extensions for fast checking.
        self._never_block_re = re.compile(
            r"\.(?:css|woff2?|ttf|otf|eot)(?:\?|$)",
            re.IGNORECASE,
        )
        logger.info("AdBlock interceptor initialized (%d blocked domains)",
                    len(self._blocked_domains))

    def set_enabled(self, enabled: bool) -> None:
        """Toggle ad-blocking on or off."""
        self._enabled = enabled
        logger.info("AdBlock %s", "enabled" if enabled else "disabled")

    def set_allowed_sites(self, hosts: Set[str]) -> None:
        self._allowed_sites = {host.lower().strip(".") for host in hosts if host}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def interceptRequest(self, info) -> None:  # noqa: N802
        """Called by Chromium for every URL request. Blocks if the
        request host matches a blocked domain and ad-block is on."""
        if not self._enabled:
            return
        try:
            first_party = info.firstPartyUrl().host().lower()
        except Exception:
            first_party = ""
        if any(first_party == host or first_party.endswith("." + host) for host in self._allowed_sites):
            return

        # Never block main-frame navigations — some blocklisted domains are
        # also real content sites (e.g. sentry.io, plausible.io), and the
        # user must always be able to visit a page directly.
        from PySide6.QtWebEngineCore import QWebEngineUrlRequestInfo
        if info.resourceType() == QWebEngineUrlRequestInfo.ResourceType.ResourceTypeMainFrame:
            return

        url = info.requestUrl()
        host = url.host().lower()
        if not host:
            return

        # Never block stylesheets/fonts — breaks page rendering.
        path = url.path().lower()
        if self._never_block_re.search(path):
            return

        # Check if the host (or any parent domain) is in the blocklist.
        if self._host_matches_blocklist(host):
            info.block(True)
            self.blockedRequest.emit(host)

    def _host_matches_blocklist(self, host: str) -> bool:
        """Check if host or any of its parent domains are blocked.

        e.g. "ad.server.doubleclick.net" matches "doubleclick.net".
        """
        h = host
        while True:
            if h in self._blocked_domains:
                return True
            idx = h.find(".")
            if idx == -1:
                break
            h = h[idx + 1:]
        return False

    def add_blocked_domain(self, domain: str) -> None:
        """Add a domain to the blocklist at runtime."""
        self._blocked_domains.add(domain.lower().strip("."))

    def remove_blocked_domain(self, domain: str) -> None:
        """Remove a domain from the blocklist at runtime."""
        self._blocked_domains.discard(domain.lower().strip("."))
