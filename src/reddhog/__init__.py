from importlib.metadata import version

from reddhog.scraper import RedditScraper

__version__ = version("reddhog")

__all__ = ["RedditScraper", "__version__"]
