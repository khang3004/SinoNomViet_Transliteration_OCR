"""Facebook story ids: the base64 form, the permalink, and a path-safe slug.

The upstream team names every image ``<base64_post_id>_<idx>.jpg``, where the
base64 decodes to a Facebook story identifier::

    UzpfSTEwMDAwMDU5MzExMzI1ODpWSzoyNzgzNTQ4OTgyNjA5MzEwMA==
      -> S:_I100000593113258:VK:27835489826093100
      -> https://www.facebook.com/permalink.php
             ?story_fbid=27835489826093100&id=100000593113258

Three separate concerns live here because they are easy to conflate:

* ``post_id``  - the raw base64 string, exactly as it appears in their data.
  This is the join key against their files and must never be rewritten.
* ``slug``     - a URL- and filesystem-safe rewrite of that same id. Standard
  base64 contains ``+``, ``/`` and ``=``, none of which survive a URL path or a
  Windows filename intact.
* ``permalink`` - the decoded Facebook URL a reviewer clicks to see the post.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

# Standard base64 with padding, which is what their filenames use.
BASE64_RE = re.compile(r"^[A-Za-z0-9+/=_-]{8,512}$")
_DIGITS = re.compile(r"^\d+$")
_OWNER = re.compile(r"^_?[A-Za-z]*?(\d{5,})$")

FACEBOOK_PERMALINK = "https://www.facebook.com/permalink.php"


@dataclass(frozen=True)
class StoryId:
    """A decoded Facebook story identifier."""

    post_id: str
    decoded: str
    owner_id: str = ""
    story_fbid: str = ""

    @property
    def permalink(self) -> str:
        """The post URL, or '' when the id did not decode to a known shape.

        Returning '' rather than a half-built URL matters: a reviewer clicking
        through to the wrong post would audit the wrong image and never know.
        """
        if not self.owner_id or not self.story_fbid:
            return ""
        return f"{FACEBOOK_PERMALINK}?story_fbid={self.story_fbid}&id={self.owner_id}"


def _b64_decode(post_id: str) -> str | None:
    """Decode, tolerating missing padding and the URL-safe alphabet."""
    if not post_id or not BASE64_RE.match(post_id):
        return None
    candidate = post_id.replace("-", "+").replace("_", "/")
    candidate += "=" * (-len(candidate) % 4)
    try:
        return base64.b64decode(candidate, validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def decode(post_id: str) -> StoryId:
    """Parse a base64 post id into its parts. Never raises.

    Facebook uses several shapes — ``S:_I<owner>:VK:<story>`` for personal
    timelines, ``S:_I<owner>:<story>`` elsewhere — so rather than matching one
    template we take the first id-looking token as the owner and the last
    all-digit token as the story. An id we cannot read still yields a StoryId,
    with an empty permalink; the image is reviewable regardless.
    """
    decoded = _b64_decode(post_id)
    if decoded is None:
        return StoryId(post_id=post_id, decoded="")

    parts = [p for p in decoded.split(":") if p]
    owner = ""
    story = ""
    for part in parts:
        match = _OWNER.match(part)
        if match and not owner:
            owner = match.group(1)
            continue
        if _DIGITS.match(part):
            story = part

    # A single numeric token is the story, not the owner: without a second id
    # there is no permalink to build, and guessing would send reviewers astray.
    if owner and not story:
        owner, story = "", ""

    return StoryId(post_id=post_id, decoded=decoded, owner_id=owner, story_fbid=story)


def permalink_for(post_id: str) -> str:
    return decode(post_id).permalink


# --- path safety -------------------------------------------------------
# Their ids are standard base64, so they can contain '+', '/' and '='. All three
# break as URL path segments, and '/' would silently create directories.

def slug(post_id: str) -> str:
    """Path- and URL-safe rewrite of a post id. Reversible via ``unslug``."""
    return post_id.replace("+", "-").replace("/", "_").replace("=", "")


def unslug(value: str) -> str:
    """Recover the original base64 id from a slug."""
    restored = value.replace("-", "+").replace("_", "/")
    return restored + "=" * (-len(restored) % 4)


IMAGE_NAME_RE = re.compile(r"^(?P<post>.+)_(?P<idx>\d+)\.(?P<ext>[A-Za-z0-9]{2,4})$")


def parse_image_name(filename: str) -> tuple[str, int, str] | None:
    """``<post_id>_0.jpg`` -> (post_id, 0, '.jpg'); None when it does not match.

    Splits on the LAST underscore because standard base64 never contains one,
    while the index always follows one.
    """
    name = filename.strip().replace("\\", "/").rsplit("/", 1)[-1]
    match = IMAGE_NAME_RE.match(name)
    if match is None:
        return None
    return match.group("post"), int(match.group("idx")), f".{match.group('ext').lower()}"
