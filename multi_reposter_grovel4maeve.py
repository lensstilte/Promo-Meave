import os
import random
import logging
from typing import List, Optional

from atproto import Client

# Basis logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# Accounts / secrets keys (suffix na BSKY_USERNAME_ / BSKY_PASSWORD_)
ACCOUNT_KEYS = [
    "BEAUTYFAN",
    "BEAUTYGROUP",
    "HOTBLEUSKY",
    "BLEUSKYPROMO",
    "NSFWBLEUSKY",
]


def get_target_handle() -> str:
    handle = os.getenv("TARGET_HANDLE")
    if not handle:
        logging.error("TARGET_HANDLE environment variable is not set.")
        raise SystemExit(1)
    return handle


def get_client_for_account(label: str) -> Optional[Client]:
    """
    Haal username/password uit env en log in.
    Als er geen secrets zijn ingevuld voor dit account: skip.
    """
    username = os.getenv(f"BSKY_USERNAME_{label}")
    password = os.getenv(f"BSKY_PASSWORD_{label}")

    if not username or not password:
        logging.warning(
            "Geen credentials gevonden voor %s (username/password), account wordt geskipt.",
            label,
        )
        return None

    client = Client()
    try:
        client.login(username, password)
        logging.info("Ingelogd als %s (label=%s)", username, label)
    except Exception as e:
        logging.error("Login mislukt voor %s: %s", label, e)
        return None

    return client


def post_has_media(post_view) -> bool:
    """
    True als de post foto of video bevat.
    We kijken naar embed op record en eventueel nested media.
    """
    record = getattr(post_view, "record", None)
    embed = getattr(record, "embed", None) if record else None

    # fallback: sommige versies zetten embed op post_view zelf
    if embed is None:
        embed = getattr(post_view, "embed", None)

    if not embed:
        return False

    def _is_media_embed(e) -> bool:
        if e is None:
            return False
        etype = (
            getattr(e, "$type", "")
            or getattr(e, "type", "")
            or getattr(getattr(e, "_type", None), "__str__", lambda: "")()
        )

        # directe media-embeds
        if "app.bsky.embed.images" in str(etype):
            return True
        if "app.bsky.embed.video" in str(etype):
            return True

        # vaak hebben images een 'images'-lijst
        if getattr(e, "images", None):
            return True

        return False

    # directe embed checken
    if _is_media_embed(embed):
        return True

    # recordWithMedia: media hangt onder embed.media
    media = getattr(embed, "media", None)
    if _is_media_embed(media):
        return True

    # als we hier zijn: geen foto/video gevonden
    return False


def fetch_recent_media_posts(client: Client, actor_handle: str, limit: int = 50):
    """
    Haal recente posts van de target op, en filter:
    - alleen eigen posts (geen reposts van anderen)
    - alleen posts met media (foto/video)
    We gebruiken 'posts_no_replies' zodat je geen replies pakt.
    """
    logging.info("Posts ophalen van %s (limit=%d)...", actor_handle, limit)
    feed = client.get_author_feed(
        actor=actor_handle,
        limit=limit,
        filter="posts_no_replies",
    )

    raw_feed = list(feed.feed or [])
    filtered = []

    for feed_post in raw_feed:
        # feed_post is AppBskyFeedDefs.FeedViewPost

        # 1) Geen reposts van anderen: reason moet None zijn
        if getattr(feed_post, "reason", None) is not None:
            # Dit is een repost die het account heeft gedaan → overslaan
            continue

        post_view = feed_post.post

        # 2) Alleen posts met media (foto/video)
        if not post_has_media(post_view):
            continue

        filtered.append(feed_post)

    logging.info(
        "Gefilterde posts voor %s: %d van de %d (eigen + met media).",
        actor_handle,
        len(filtered),
        len(raw_feed),
    )
    return filtered


def choose_posts_for_run(feed_posts, num_random_older: int = 2):
    """
    Kies:
    - altijd de nieuwste post (index 0)
    - plus num_random_older willekeurige oudere posts uit de rest
    """
    if not feed_posts:
        return []

    selected = []

    # Nieuwste post (bovenaan in feed)
    newest = feed_posts[0]
    selected.append(newest)

    # Oudere posts (alles na index 0)
    older = feed_posts[1:]
    if older:
        k = min(num_random_older, len(older))
        random_older = random.sample(older, k=k)
        selected.extend(random_older)

    return selected


def unrepost_if_needed_and_repost(client: Client, feed_post) -> None:
    """
    - Check of deze post al is gerepost door de huidige account (viewer.repost)
    - Zo ja: delete_repost(repost_uri)
    - Daarna: repost(uri, cid)
    """
    post_view = feed_post.post  # AppBskyFeedDefs.PostView

    uri = post_view.uri
    cid = post_view.cid
    viewer = post_view.viewer  # ViewerState of None
    repost_uri = getattr(viewer, "repost", None) if viewer else None

    if repost_uri:
        logging.info("  Post %s is al gerepost. Oude repost wordt verwijderd: %s", uri, repost_uri)
        try:
            client.delete_repost(repost_uri)
            logging.info("  Oude repost verwijderd.")
        except Exception as e:
            logging.warning("  Kon oude repost niet verwijderen (%s): %s", repost_uri, e)

    logging.info("  Nieuwe repost van %s...", uri)
    try:
        client.repost(uri=uri, cid=cid)
        logging.info("  Repost gelukt.")
    except Exception as e:
        logging.error("  Repost mislukt voor %s: %s", uri, e)


def process_account(label: str, target_handle: str) -> None:
    """
    Verwerk één bot-account:
    - login
    - eigen media-posts ophalen (geen reposts, geen tekst-only)
    - nieuwste + 2 random oudere kiezen
    - per gekozen post: unrepost (als nodig) + opnieuw repost
    """
    logging.info("=== Account %s starten ===", label)
    client = get_client_for_account(label)
    if not client:
        logging.warning("Account %s wordt overgeslagen.", label)
        return

    try:
        feed_posts = fetch_recent_media_posts(client, target_handle)
    except Exception as e:
        logging.error(
            "Kon feed voor %s niet ophalen bij account %s: %s",
            target_handle,
            label,
            e,
        )
        return

    if not feed_posts:
        logging.info(
            "Geen geschikte media-posts gevonden voor %s, account %s slaat run over.",
            target_handle,
            label,
        )
        return

    to_repost = choose_posts_for_run(feed_posts, num_random_older=2)

    logging.info(
        "Account %s gaat %d posts (nieuwste + random) (opnieuw) repost-en.",
        label,
        len(to_repost),
    )

    for feed_post in to_repost:
        unrepost_if_needed_and_repost(client, feed_post)


def main():
    target_handle = get_target_handle()
    logging.info("Target handle: %s", target_handle)

    for label in ACCOUNT_KEYS:
        process_account(label, target_handle)

    logging.info("Multi-reposter run voltooid.")


if __name__ == "__main__":
    main()