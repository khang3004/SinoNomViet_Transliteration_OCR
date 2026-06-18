"""
data_scraper.py
===============
Robust asynchronous image scraper for the Nom Foundation Digital Library.

Target corpus: "An Nam Nhất Thống Chí" (HVH_004)
Source URL:    https://lib.nomfoundation.org/collection/1/volume/664/

This module discovers all page image URLs from the viewer's pagination
interface and downloads them concurrently with configurable throttling,
retry logic, and clean sequential file naming.

Usage (CLI):
    python data_scraper.py --url "https://lib.nomfoundation.org/collection/1/volume/664/" \
                           --out ./data/raw_images \
                           --workers 8 \
                           --delay 0.5

Author: NLP Pipeline — HCMUS NaturalLanguageProcessing
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import aiofiles
import aiohttp
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("data_scraper")


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------
@dataclass
class ScraperConfig:
    """Runtime configuration for the NomFoundation image scraper.

    Attributes:
        base_url:       The landing page of the target volume.
        output_dir:     Local directory where page images are saved.
        max_workers:    Maximum number of concurrent download coroutines.
        request_delay:  Seconds to sleep between each HTTP request (polite crawling).
        max_retries:    Maximum retry attempts per failed request.
        timeout:        Per-request HTTP timeout in seconds.
        user_agent:     HTTP User-Agent header value.
        file_prefix:    Filename prefix for saved images (e.g., "page").
    """

    base_url: str = "https://lib.nomfoundation.org/collection/1/volume/664/"
    output_dir: Path = Path("./data/raw_images")
    max_workers: int = 8
    request_delay: float = 0.5
    max_retries: int = 3
    timeout: int = 30
    user_agent: str = (
        "Mozilla/5.0 (compatible; NomFoundationScraper/1.0; "
        "HCMUS-NLP-Research/2024)"
    )
    file_prefix: str = "page"
    # Known image API patterns for NomFoundation IIIF viewer
    image_api_patterns: list[str] = field(
        default_factory=lambda: [
            r"/image/\d+/full/full/0/default\.jpg",
            r"/collection/\d+/volume/\d+/image/\d+",
        ]
    )


# ---------------------------------------------------------------------------
# URL discovery helpers
# ---------------------------------------------------------------------------

class NomFoundationURLDiscoverer:
    """Discovers all page image URLs from a Nom Foundation volume viewer page.

    The Nom Foundation uses a IIIF-compatible image viewer. This class
    first attempts to parse page-image links from the HTML, then falls back
    to probing the IIIF manifest endpoint for the canonical image list.

    Args:
        config: Scraper configuration instance.
    """

    def __init__(self, config: ScraperConfig) -> None:
        self._cfg = config
        self._session_headers = {"User-Agent": config.user_agent}

    # ------------------------------------------------------------------
    def discover(self) -> list[tuple[int, str]]:
        """Discover all (page_number, image_url) pairs for the volume.

        Returns:
            A sorted list of (page_number, absolute_image_url) tuples.

        Raises:
            RuntimeError: When no image URLs can be discovered.
        """
        logger.info("Discovering page image URLs from: %s", self._cfg.base_url)
        pairs = self._try_iiif_manifest() or self._try_html_scrape()

        if not pairs:
            raise RuntimeError(
                "Could not discover any image URLs. The website structure may "
                "have changed. Please inspect the page manually."
            )

        # Sort by page number to guarantee ordering
        pairs.sort(key=lambda x: x[0])
        logger.info("Discovered %d page image URLs.", len(pairs))
        return pairs

    # ------------------------------------------------------------------
    def _try_iiif_manifest(self) -> list[tuple[int, str]]:
        """Attempt to fetch the IIIF manifest JSON and extract image URLs.

        NomFoundation often exposes a manifest.json that lists all canvas
        images in order — the most reliable discovery method.

        Returns:
            A list of (page_number, image_url) tuples, or empty list on failure.
        """
        # Common IIIF manifest URL patterns
        parsed = urlparse(self._cfg.base_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]

        # Build candidate manifest URLs
        candidates: list[str] = []
        if len(path_parts) >= 4:
            # e.g. /collection/1/volume/664/ → manifest at collection/1/volume/664/manifest
            vol_root = "/" + "/".join(path_parts[:4])
            candidates = [
                urljoin(base, vol_root + "/manifest"),
                urljoin(base, vol_root + "/manifest.json"),
                urljoin(base, vol_root + "/iiif/manifest.json"),
            ]

        for url in candidates:
            try:
                resp = requests.get(
                    url,
                    headers=self._session_headers,
                    timeout=self._cfg.timeout,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return self._parse_iiif_manifest(data)
            except Exception as exc:
                logger.debug("IIIF manifest probe failed for %s: %s", url, exc)
        return []

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_iiif_manifest(manifest: dict) -> list[tuple[int, str]]:
        """Parse a IIIF Presentation API v2/v3 manifest for image URLs.

        Args:
            manifest: Parsed JSON manifest dictionary.

        Returns:
            A list of (page_number, image_url) tuples.
        """
        pairs: list[tuple[int, str]] = []

        # IIIF v2: sequences → canvases → images
        sequences = manifest.get("sequences", [])
        for seq in sequences:
            canvases = seq.get("canvases", [])
            for idx, canvas in enumerate(canvases, start=1):
                images = canvas.get("images", [])
                for img in images:
                    resource = img.get("resource", {})
                    url = resource.get("@id", "")
                    if url:
                        # Ensure we get the full-resolution image
                        url = re.sub(r"/\d+,\d+/", "/full/", url)
                        pairs.append((idx, url))
                        break  # One image per canvas

        # IIIF v3: items → items → body
        if not pairs:
            items = manifest.get("items", [])
            for idx, canvas in enumerate(items, start=1):
                for annotation_page in canvas.get("items", []):
                    for annotation in annotation_page.get("items", []):
                        body = annotation.get("body", {})
                        url = body.get("id", "")
                        if url:
                            pairs.append((idx, url))
                            break

        return pairs

    # ------------------------------------------------------------------
    def _try_html_scrape(self) -> list[tuple[int, str]]:
        """Fall back to HTML parsing to extract page image URLs.

        Crawls the landing page and any pagination links, collecting
        all ``<img>`` sources and ``<a href>`` image links.

        Returns:
            A list of (page_number, image_url) tuples.
        """
        logger.info("Falling back to HTML scraping strategy.")
        try:
            resp = requests.get(
                self._cfg.base_url,
                headers=self._session_headers,
                timeout=self._cfg.timeout,
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.error("Failed to fetch base URL: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        parsed_base = urlparse(self._cfg.base_url)
        origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

        image_urls: list[str] = []

        # Strategy 1: <img> tags with likely page-image sources
        for img in soup.find_all("img"):
            src = img.get("src", "") or img.get("data-src", "")
            if self._is_page_image_url(src):
                image_urls.append(urljoin(origin, src))

        # Strategy 2: <a> links pointing to full-res images
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if self._is_page_image_url(href):
                image_urls.append(urljoin(origin, href))

        # Strategy 3: JavaScript variables containing image arrays
        for script in soup.find_all("script"):
            text = script.string or ""
            found = re.findall(r'["\']([^"\']*(?:page|image)\d+[^"\']*\.jpe?g)["\']', text, re.I)
            for src in found:
                image_urls.append(urljoin(origin, src))

        # Deduplicate, preserving order, then enumerate
        seen: set[str] = set()
        unique_urls: list[str] = []
        for url in image_urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)

        return [(i + 1, url) for i, url in enumerate(unique_urls)]

    # ------------------------------------------------------------------
    def _is_page_image_url(self, url: str) -> bool:
        """Check whether a URL looks like a scanned page image.

        Args:
            url: The URL string to evaluate.

        Returns:
            True if the URL matches expected page-image patterns.
        """
        if not url:
            return False
        url_lower = url.lower()
        # Must be an image format
        if not any(url_lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff")):
            return False
        # Filter out thumbnails and UI icons
        exclude_keywords = ("thumb", "icon", "logo", "button", "nav", "menu")
        return not any(kw in url_lower for kw in exclude_keywords)


# ---------------------------------------------------------------------------
# Async downloader
# ---------------------------------------------------------------------------

class AsyncImageDownloader:
    """Asynchronously downloads a batch of images with throttling and retries.

    Uses ``aiohttp`` for concurrent HTTP sessions and ``aiofiles`` for
    non-blocking disk writes. A semaphore limits concurrency to
    ``config.max_workers``.

    Args:
        config: Scraper configuration instance.
    """

    def __init__(self, config: ScraperConfig) -> None:
        self._cfg = config
        self._semaphore: Optional[asyncio.Semaphore] = None

    # ------------------------------------------------------------------
    async def download_all(
        self,
        page_pairs: list[tuple[int, str]],
    ) -> dict[int, Path]:
        """Download all pages concurrently.

        Args:
            page_pairs: List of (page_number, image_url) tuples.

        Returns:
            Dictionary mapping page_number → saved local ``Path``.
        """
        self._cfg.output_dir.mkdir(parents=True, exist_ok=True)
        self._semaphore = asyncio.Semaphore(self._cfg.max_workers)

        results: dict[int, Path] = {}
        total = len(page_pairs)

        connector = aiohttp.TCPConnector(limit=self._cfg.max_workers, ssl=False)
        timeout = aiohttp.ClientTimeout(total=self._cfg.timeout)

        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"User-Agent": self._cfg.user_agent},
        ) as session:
            tasks = [
                self._download_one(session, page_num, url)
                for page_num, url in page_pairs
            ]

            with tqdm(total=total, desc="Downloading pages", unit="img") as pbar:
                for coro in asyncio.as_completed(tasks):
                    page_num, saved_path = await coro
                    if saved_path is not None:
                        results[page_num] = saved_path
                    pbar.update(1)

        logger.info(
            "Download complete. %d/%d pages saved to: %s",
            len(results),
            total,
            self._cfg.output_dir,
        )
        return results

    # ------------------------------------------------------------------
    async def _download_one(
        self,
        session: aiohttp.ClientSession,
        page_num: int,
        url: str,
    ) -> tuple[int, Optional[Path]]:
        """Download a single page image with retry logic.

        Args:
            session:   Active ``aiohttp.ClientSession``.
            page_num:  1-based page number used for file naming.
            url:       Absolute image URL.

        Returns:
            ``(page_num, local_path)`` on success; ``(page_num, None)`` on failure.
        """
        assert self._semaphore is not None, "Semaphore not initialized"

        async with self._semaphore:
            # Polite delay to avoid hammering the server
            await asyncio.sleep(self._cfg.request_delay)

            dest_path = self._build_dest_path(page_num, url)
            if dest_path.exists():
                logger.debug("Skipping existing file: %s", dest_path.name)
                return page_num, dest_path

            for attempt in range(1, self._cfg.max_retries + 1):
                try:
                    async with session.get(url) as resp:
                        resp.raise_for_status()
                        content = await resp.read()

                    async with aiofiles.open(dest_path, "wb") as fh:
                        await fh.write(content)

                    logger.debug(
                        "Saved page %03d → %s (%d bytes)",
                        page_num,
                        dest_path.name,
                        len(content),
                    )
                    return page_num, dest_path

                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    wait = 2 ** attempt  # Exponential back-off
                    logger.warning(
                        "Attempt %d/%d failed for page %03d (%s). Retrying in %ds.",
                        attempt,
                        self._cfg.max_retries,
                        page_num,
                        exc,
                        wait,
                    )
                    await asyncio.sleep(wait)

            logger.error("All retries exhausted for page %03d — URL: %s", page_num, url)
            return page_num, None

    # ------------------------------------------------------------------
    def _build_dest_path(self, page_num: int, url: str) -> Path:
        """Build the canonical destination file path for a page image.

        File naming convention: ``<prefix>_<NNN>.<ext>``
        Examples: ``page_001.jpg``, ``page_042.jpg``

        Args:
            page_num: 1-based page number.
            url:      Source image URL (used to infer file extension).

        Returns:
            Absolute ``Path`` to the local destination file.
        """
        # Infer extension from URL path
        url_path = urlparse(url).path
        suffix = Path(url_path).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".tif", ".tiff"):
            suffix = ".jpg"  # Default fallback

        filename = f"{self._cfg.file_prefix}_{page_num:03d}{suffix}"
        return self._cfg.output_dir / filename


# ---------------------------------------------------------------------------
# Public pipeline entry point
# ---------------------------------------------------------------------------

def run_scraper(config: Optional[ScraperConfig] = None) -> dict[int, Path]:
    """Execute the full scrape pipeline: discover → download → return manifest.

    Args:
        config: Optional scraper configuration. Uses defaults if not provided.

    Returns:
        Dictionary mapping page_number (int) → saved file ``Path``.

    Example:
        >>> cfg = ScraperConfig(output_dir=Path("./my_images"), max_workers=4)
        >>> page_map = run_scraper(cfg)
        >>> print(page_map[1])  # Path to page_001.jpg
    """
    if config is None:
        config = ScraperConfig()

    # Step 1: Discover all page image URLs
    discoverer = NomFoundationURLDiscoverer(config)
    page_pairs = discoverer.discover()

    # Step 2: Download concurrently
    downloader = AsyncImageDownloader(config)

    # Use asyncio.run (Python 3.7+)
    start_time = time.perf_counter()
    page_map = asyncio.run(downloader.download_all(page_pairs))
    elapsed = time.perf_counter() - start_time

    logger.info(
        "Pipeline finished in %.2fs — %d pages downloaded.",
        elapsed,
        len(page_map),
    )
    return page_map


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="NomFoundation page image scraper for SinoNom corpus.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--url",
        default="https://lib.nomfoundation.org/collection/1/volume/664/",
        help="Landing page URL of the target volume.",
    )
    parser.add_argument(
        "--out",
        default="./data/raw_images",
        type=Path,
        help="Local directory to save downloaded images.",
    )
    parser.add_argument(
        "--workers",
        default=8,
        type=int,
        help="Maximum concurrent download workers.",
    )
    parser.add_argument(
        "--delay",
        default=0.5,
        type=float,
        help="Polite delay (seconds) between requests.",
    )
    parser.add_argument(
        "--retries",
        default=3,
        type=int,
        help="Maximum retry attempts per failed request.",
    )
    parser.add_argument(
        "--timeout",
        default=30,
        type=int,
        help="Per-request timeout in seconds.",
    )
    parser.add_argument(
        "--prefix",
        default="page",
        help="Filename prefix for saved images.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser


if __name__ == "__main__":
    args = _build_arg_parser().parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    cfg = ScraperConfig(
        base_url=args.url,
        output_dir=args.out,
        max_workers=args.workers,
        request_delay=args.delay,
        max_retries=args.retries,
        timeout=args.timeout,
        file_prefix=args.prefix,
    )

    page_map = run_scraper(cfg)
    print(f"\n✅  Scraping complete. {len(page_map)} images saved to: {cfg.output_dir}")
