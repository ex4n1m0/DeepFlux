"""RSS feed fetcher and monitor for Deeptorrent."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

import requests

from config import RSSFeed

logger = logging.getLogger(__name__)


@dataclass
class FeedItem:
    """A single item from an RSS feed."""
    title: str = ""
    link: str = ""
    description: str = ""
    published: str = ""
    guid: str = ""
    # Extracted torrent info (if available in the feed).
    magnet_uri: str = ""
    torrent_url: str = ""
    size: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "link": self.link,
            "description": self.description[:500] if self.description else "",
            "published": self.published,
            "magnet_uri": self.magnet_uri,
            "torrent_url": self.torrent_url,
            "size": self.size,
        }


class RSSFeedClient:
    """Fetches and parses RSS/Atom feeds, extracting torrent links."""

    def __init__(self, feed: RSSFeed) -> None:
        self.feed = feed

    def fetch(self) -> List[FeedItem]:
        """Fetch the feed URL and return parsed items."""
        try:
            resp = requests.get(
                self.feed.url,
                timeout=30,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Deeptorrent/0.1 RSS Reader"},
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("RSS fetch failed for %s: %s", self.feed.url, exc)
            return []

        return self._parse(resp.content)

    def _parse(self, content: bytes) -> List[FeedItem]:
        """Parse RSS 2.0 or Atom XML into FeedItem list."""
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            logger.warning("RSS parse error for %s: %s", self.feed.url, exc)
            return []

        # Detect format: RSS has <rss><channel><item>, Atom has <feed><entry>
        tag = root.tag.lower()
        items: List[FeedItem] = []

        if "rss" in tag or "channel" in tag:
            # RSS 2.0
            channel = root.find("channel") or root
            for item_elem in channel.findall("item"):
                items.append(self._parse_rss_item(item_elem))
        elif "feed" in tag:
            # Atom
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall("atom:entry", ns) or root.findall("entry")
            for entry in entries:
                items.append(self._parse_atom_entry(entry, ns))
        else:
            # Try generic item/entry search
            for item_elem in root.iter("item"):
                items.append(self._parse_rss_item(item_elem))
            if not items:
                for entry in root.iter("entry"):
                    items.append(self._parse_atom_entry(entry, {"atom": ""}))

        return [i for i in items if i.title or i.link]

    def _parse_rss_item(self, elem: ET.Element) -> FeedItem:
        def text(tag: str) -> str:
            child = elem.find(tag)
            return child.text.strip() if child is not None and child.text else ""

        title = text("title")
        link = text("link")
        description = text("description")
        published = text("pubDate") or text("published")
        guid = text("guid") or link or title

        # Look for enclosures (torrent attachments).
        torrent_url = ""
        size = ""
        for enc in elem.findall("enclosure"):
            url = enc.get("url", "")
            if ".torrent" in url.lower():
                torrent_url = url
                size = enc.get("length", "")
                break

        # Extract magnet links from description or link.
        magnet = self._extract_magnet(description) or self._extract_magnet(link)
        if link.startswith("magnet:"):
            magnet = link

        return FeedItem(
            title=title, link=link, description=description,
            published=published, guid=guid, magnet_uri=magnet,
            torrent_url=torrent_url, size=size,
        )

    def _parse_atom_entry(self, elem: ET.Element, ns: Dict[str, str]) -> FeedItem:
        def text(tag: str) -> str:
            if ns.get("atom"):
                child = elem.find(f"atom:{tag}", ns)
            else:
                child = elem.find(tag)
            return child.text.strip() if child is not None and child.text else ""

        title = text("title")
        published = text("published") or text("updated")
        summary = text("summary") or text("content")
        guid = text("id")

        # Atom links: <link href="..." rel="alternate" />
        link = ""
        for link_elem in elem.findall("atom:link", ns) or elem.findall("link"):
            href = link_elem.get("href", "")
            rel = link_elem.get("rel", "")
            if rel == "alternate" or not link:
                link = href or link_elem.text or ""

        magnet = self._extract_magnet(summary) or self._extract_magnet(link)
        torrent_url = ""
        for link_elem in elem.findall("atom:link", ns) or elem.findall("link"):
            href = link_elem.get("href", "")
            if ".torrent" in href.lower():
                torrent_url = href
                break

        return FeedItem(
            title=title, link=link, description=summary,
            published=published, guid=guid or link or title,
            magnet_uri=magnet, torrent_url=torrent_url,
        )

    @staticmethod
    def _extract_magnet(text: str) -> str:
        """Find a magnet: URI in a block of text."""
        if not text:
            return ""
        match = re.search(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+[^\"\s<>]*", text)
        return match.group(0) if match else ""


class RSSMonitor:
    """Monitors RSS feeds, tracks seen items, and auto-downloads torrents."""

    def __init__(self, config: RSSConfig) -> None:
        self.config = config

    def check_feed(self, feed: RSSFeed) -> Dict[str, Any]:
        """Fetch a feed, return new items and optionally mark them as seen.

        Returns dict with: feed_name, mode, total_items, new_items, items.
        """
        client = RSSFeedClient(feed)
        items = client.fetch()

        if not items:
            return {
                "feed_name": feed.name or feed.url,
                "mode": feed.mode,
                "total_items": 0,
                "new_items": 0,
                "items": [],
                "error": "Failed to fetch or parse feed",
            }

        # Filter to only new items (by guid).
        seen = set(feed.seen_items)
        new_items = [item for item in items if item.guid not in seen]

        return {
            "feed_name": feed.name or feed.url,
            "mode": feed.mode,
            "total_items": len(items),
            "new_items": len(new_items),
            "items": [item.to_dict() for item in new_items],
            "all_items": [item.to_dict() for item in items],
        }

    def mark_seen(self, feed: RSSFeed, items: List[Dict[str, Any]]) -> None:
        """Add item guids to the feed's seen list."""
        for item in items:
            guid = item.get("guid") or item.get("link") or item.get("title")
            if guid and guid not in feed.seen_items:
                feed.seen_items.append(guid)
        # Keep the seen list bounded.
        if len(feed.seen_items) > 500:
            feed.seen_items = feed.seen_items[-500:]

    def get_downloadable_items(self, feed: RSSFeed) -> List[Dict[str, Any]]:
        """Return new items from a feed that have magnet or .torrent links."""
        result = self.check_feed(feed)
        new_items = result.get("items", [])
        downloadable = [
            item for item in new_items
            if item.get("magnet_uri") or item.get("torrent_url")
        ]
        return downloadable
