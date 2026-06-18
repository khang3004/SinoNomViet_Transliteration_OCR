r"""data_scraper.py
===============
Robust asynchronous image scraper for the Nom Foundation Digital Library.

Target corpus: "An Nam Nhất Thống Chí" (HVH_004)
Source URL:    https://lib.nomfoundation.org/collection/1/volume/664/

Discovery strategy (reverse-engineered from browser analysis):
  The Nom Foundation uses a static image hosting pattern:
    https://lib.nomfoundation.org/site_media/nom/{identifier}/large/{identifier}-{NNN}.jpg

  The volume landing page contains:
    - The volume identifier (e.g., "NLVNPF-0506") in the page title/metadata
    - The total page count (e.g., "107") in the page metadata

  This scraper:
    1. Fetches the landing page and extracts identifier + page count
    2. Generates all image URLs deterministically
    3. Downloads them concurrently with configurable throttling and retries

Usage (CLI):
    python data_scraper.py --url "https://lib.nomfoundation.org/collection/1/volume/664/" \\
                           --out ./data/raw_images \\
                           --workers 8 \\
                           --delay 0.3

Author: NLP Pipeline — HCMUS NaturalLanguageProcessing
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

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
    request_delay: float = 0.3
    max_retries: int = 3
    timeout: int = 60
    user_agent: str = "Mozilla/5.0 (compatible; NomFoundationScraper/1.0; HCMUS-NLP-Research/2024)"
    file_prefix: str = "page"


# ---------------------------------------------------------------------------
# URL discovery — NomFoundation static image pattern
# ---------------------------------------------------------------------------


class NomFoundationURLDiscoverer:
    """Discovers all page image URLs from a Nom Foundation volume landing page.

    The Nom Foundation hosts scanned manuscript images at a predictable
    static URL pattern::

        https://lib.nomfoundation.org/site_media/nom/{id}/large/{id}-{NNN}.jpg

    where ``{id}`` is the volume identifier (e.g., ``nlvnpf-0506``) and
    ``{NNN}`` is the zero-padded page number.

    This class fetches the landing page HTML, extracts the identifier and
    total page count, then generates all image URLs deterministically.

    Args:
        config: Scraper configuration instance.
    """

    # Regex patterns for extracting volume metadata from the HTML
    # Pattern 1: identifier in the page title "NLVNPF-0506"
    _ID_PATTERNS: list[str] = [
        r"\b([A-Z]+-\d{4})\b",  # e.g., NLVNPF-0506
        r'identifier["\s:]+([A-Za-z0-9\-]+)',  # JSON-like fields
        r"/nom/([a-z0-9\-]+)/jpeg/",  # from existing img src
    ]
    # Pattern for total page count
    _PAGE_COUNT_PATTERNS: list[str] = [
        r"(\d+)\s+pages?",  # "107 pages"
        r"Page\s+\d+\s+of\s+(\d+)",  # "Page 1 of 107"
        r"<b>Pages</b>[^<]*<[^>]+>(\d+)",  # table cell pattern
        r"Pages.*?(\d{2,4})",  # generic fallback
    ]

    def __init__(self, config: ScraperConfig) -> None:
        self._cfg = config
        self._headers = {
            "User-Agent": config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    # ------------------------------------------------------------------
    def discover(self) -> list[tuple[int, str]]:
        """Discover all (page_number, image_url) pairs for the volume.

        Returns:
            A sorted list of (page_number, absolute_image_url) tuples.

        Raises:
            RuntimeError: When the volume identifier or page count cannot
                          be determined from the landing page HTML.
        """
        logger.info("Fetching volume landing page: %s", self._cfg.base_url)

        try:
            resp = requests.get(
                self._cfg.base_url,
                headers=self._headers,
                timeout=self._cfg.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"Failed to fetch landing page: {exc}") from exc

        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        # Step 1: Extract volume identifier
        volume_id = self._extract_volume_id(html, soup)
        logger.info("Volume identifier detected: %s", volume_id)

        # Step 2: Extract total page count
        total_pages = self._extract_page_count(html, soup)
        logger.info("Total pages detected: %d", total_pages)

        # Step 3: Generate all image URLs using the static pattern
        pairs = self._generate_image_urls(volume_id, total_pages)
        logger.info(
            "Generated %d image URLs using pattern: %s",
            len(pairs),
            f"site_media/nom/{volume_id}/large/{volume_id}-NNN.jpg",
        )
        return pairs

    # ------------------------------------------------------------------
    def _extract_volume_id(self, html: str, soup: BeautifulSoup) -> str:
        """Extract the volume identifier from the landing page.

        Tries multiple strategies in order:
        1. Look for an existing image src with the /jpeg/ URL pattern
        2. Look for the identifier in metadata tags
        3. Search the full HTML text with regex patterns

        Args:
            html: Raw HTML string.
            soup: Parsed BeautifulSoup tree.

        Returns:
            Lowercased volume identifier string (e.g., "nlvnpf-0506").

        Raises:
            RuntimeError: If no identifier can be found.
        """
        # Strategy 1: Look for existing image URLs in the HTML with the /jpeg/ pattern
        # The page thumbnail uses: /site_media/nom/{id}/jpeg/{id}-001.jpg
        img_pattern = re.search(r"/site_media/nom/([a-z0-9\-]+)/jpeg/", html, re.I)
        if img_pattern:
            return img_pattern.group(1).lower()

        # Strategy 2: Find the identifier code in <b> tags or metadata tables
        # The Nom Foundation typically shows it as "NLVNPF-0506" on the page
        for tag in soup.find_all(["b", "strong", "td", "span", "h2", "h3"]):
            text = tag.get_text(strip=True)
            m = re.search(r"\b([A-Z]{2,10}-\d{3,6})\b", text)
            if m:
                return m.group(1).lower()

        # Strategy 3: Full-page regex scan
        m = re.search(r"\b(NLVNPF-\d{4}|R\.\d{4}|[A-Z]{2,}-\d{3,})\b", html)
        if m:
            return m.group(1).lower()

        raise RuntimeError(
            "Could not detect volume identifier from the landing page HTML. "
            "Please check the URL or specify the identifier manually."
        )

    # ------------------------------------------------------------------
    def _extract_page_count(self, html: str, soup: BeautifulSoup) -> int:
        """Extract the total page count from the landing page.

        The Nom Foundation metadata list uses this HTML pattern::

            <dt>Pages</dt>
            <dd>107</dd>

        Args:
            html: Raw HTML string.
            soup: Parsed BeautifulSoup tree.

        Returns:
            Total number of pages as an integer.

        Raises:
            RuntimeError: If page count cannot be determined.
        """
        # Strategy 1: Parse the <dt>Pages</dt><dd>NNN</dd> metadata list
        # This is the definitive pattern used by the Nom Foundation website.
        for dt in soup.find_all("dt"):
            if dt.get_text(strip=True).lower() == "pages":
                dd = dt.find_next_sibling("dd")
                if dd:
                    m = re.search(r"(\d+)", dd.get_text())
                    if m:
                        count = int(m.group(1))
                        if 1 < count < 10000:
                            logger.debug("Page count from <dt>Pages</dt>: %d", count)
                            return count

        # Strategy 2: Look in <b>/<strong> tags followed by a value
        for tag in soup.find_all(["b", "strong"]):
            if "pages" in tag.get_text(strip=True).lower():
                parent = tag.parent
                if parent:
                    sibling = parent.find_next_sibling()
                    if sibling:
                        m = re.search(r"(\d+)", sibling.get_text())
                        if m:
                            count = int(m.group(1))
                            if 1 < count < 10000:
                                return count

        # Strategy 3: Regex on full HTML (fallback)
        for pattern in self._PAGE_COUNT_PATTERNS:
            matches = re.findall(pattern, html, re.I)
            for match in matches:
                count = int(match)
                if 1 < count < 10000:
                    logger.debug("Page count via regex %r: %d", pattern, count)
                    return count

        # Strategy 4: Count <a href="page/N"> links in the HTML (last resort)
        page_links = re.findall(r'href=["\']page/(\d+)["\']', html, re.I)
        if page_links:
            max_page = max(int(p) for p in page_links)
            if max_page > 1:
                logger.debug("Page count from href links: %d", max_page)
                return max_page

        raise RuntimeError(
            "Could not determine total page count from the landing page HTML. "
            "Check that the URL is a valid Nom Foundation volume page."
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _generate_image_urls(
        volume_id: str,
        total_pages: int,
    ) -> list[tuple[int, str]]:
        """Generate all image URLs using the NomFoundation static pattern.

        URL pattern (confirmed via browser DOM analysis)::

            https://lib.nomfoundation.org/site_media/nom/{id}/jpeg/{id}-{NNN}.jpg

        The ``/jpeg/`` subdirectory contains the full-resolution scans.
        Page numbers are zero-padded to 3 digits.

        Args:
            volume_id:   Lowercased volume identifier (e.g., "nlvnpf-0506").
            total_pages: Total number of pages in the volume.

        Returns:
            List of (page_number, image_url) tuples, sorted ascending.
        """
        base = "https://lib.nomfoundation.org"
        pairs: list[tuple[int, str]] = []
        for page_num in range(1, total_pages + 1):
            url = f"{base}/site_media/nom/{volume_id}/jpeg/{volume_id}-{page_num:03d}.jpg"
            pairs.append((page_num, url))
        return pairs


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
        self._semaphore: asyncio.Semaphore | None = None

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
            tasks = [self._download_one(session, page_num, url) for page_num, url in page_pairs]

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
    ) -> tuple[int, Path | None]:
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
            await asyncio.sleep(self._cfg.request_delay)

            dest_path = self._build_dest_path(page_num)
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
                    wait = 2**attempt
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
    def _build_dest_path(self, page_num: int) -> Path:
        """Build the canonical destination file path for a page image.

        Naming convention: ``page_NNN.jpg``

        Args:
            page_num: 1-based page number.

        Returns:
            Absolute ``Path`` to the local destination file.
        """
        return self._cfg.output_dir / f"{self._cfg.file_prefix}_{page_num:03d}.jpg"


# ---------------------------------------------------------------------------
# Public pipeline entry point
# ---------------------------------------------------------------------------


def run_scraper(config: ScraperConfig | None = None) -> dict[int, Path]:
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

    discoverer = NomFoundationURLDiscoverer(config)
    page_pairs = discoverer.discover()

    downloader = AsyncImageDownloader(config)

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
        default=0.3,
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
        default=60,
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


def main() -> None:
    """CLI entry point."""
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


if __name__ == "__main__":
    main()
