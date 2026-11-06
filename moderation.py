"""
Content screening hook (STUB, shipped in the open-source edition in place of the real filter).

The screening rules used by the original deployment are intentionally NOT published: anyone who can read
them can write ads that slip past them. This stub keeps the rest of the bot fully working (the owner review
queue, Accept / Decline, user bans, taking down posted copies, the Report button on every posted ad) but it
flags nothing by itself.

To screen ads in your own deployment, implement scan() below: a keyword list, an external moderation API,
whatever suits you. It receives the ad text and returns a list of Flag objects. Any non-empty result HOLDS
that ad: it is not posted, and the owner (OWNER_IDS) gets a DM with Accept / Decline buttons. Returning a
Flag for every ad therefore turns on "the owner approves every ad before it is posted".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

CSAM = "csam"
ILLEGAL = "illegal"

CATEGORY_LABELS = {
    CSAM: "Child sexual exploitation (CSAM-related text)",
    ILLEGAL: "Other illegal activity",
    "report": "Reported by a user",
}


@dataclass(frozen=True)
class Flag:
    category: str  # CSAM | ILLEGAL (or any key you add to CATEGORY_LABELS)
    severity: str  # "high" | "medium"
    match: str  # the text that tripped the filter
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def scan(text: str) -> list[Flag]:
    """Return every red flag found in `text`. Empty list = nothing flagged. STUB: flags nothing."""
    return []
