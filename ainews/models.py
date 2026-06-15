"""Core data model shared across the package.

A single ``Article`` dataclass is the lingua franca: feeds produce them,
enrichment annotates them (sentiment / topic), and the view-model + UI render
them. Keeping one typed shape here is what lets every other module be developed
and reasoned about independently.
"""
from dataclasses import dataclass, asdict, field
from typing import List, Optional

# Valid sentiment labels. "neutral" is the safe default before classification.
SENTIMENTS = ("hype", "concern", "neutral")
DEFAULT_TOPIC = "General"


@dataclass
class Article:
    title: str
    published: str
    published_ts: Optional[float]
    feed_name: str
    summary: str            # HTML-stripped, plain text
    link: str
    sentiment: str = "neutral"   # one of SENTIMENTS
    topic: str = DEFAULT_TOPIC   # cluster label assigned during enrichment
    read: bool = False           # UI state; persisted by key via persist.Store
    bookmarked: bool = False     # UI state; persisted by key via persist.Store
    cluster_size: int = 1        # how many near-duplicate copies merged (corroboration)
    concepts: List[str] = field(default_factory=list)  # knowledge-graph concept ids (tagged at load)

    @property
    def key(self) -> str:
        """Stable identity used for de-duplication and cache lookups."""
        return (self.link or self.title).strip().lower()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Article":
        # Tolerate extra/missing keys so cached payloads survive schema tweaks.
        fields = {
            "title", "published", "published_ts", "feed_name",
            "summary", "link", "sentiment", "topic", "read", "bookmarked",
            "cluster_size", "concepts",
        }
        return cls(**{k: v for k, v in d.items() if k in fields})
