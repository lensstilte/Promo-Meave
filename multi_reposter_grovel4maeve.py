import os
import random
import logging
from typing import List, Optional

from atproto import Client, models

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

ACCOUNT_KEYS = [
    "BEAUTYFAN",
    "BEAUTYGROUP",
    "HOTBLEUSKY",
    "BLEUSKYPROMO",
    "NSFWBLEUSKY",
]

TARGET_HANDLE = "grovel4maeve.bsky.social"


def get_client_for_account(label: str) -> Optional[Client]:
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


def has_media(post_view: models.AppBskyFeedDefs_PostView) -> bool:
    embed = getattr(post_view, "embed", None)
    if not embed:
        return False

    if isinstance(embed, models.AppBskyEmbedImages_View):
        return bool(embed.images)

    if isinstance(embed, models.AppBskyEmbedVideo_View):
        return True

    if isinstance(embed, models.AppBskyEmbedRecordWithMedia_View):
        media = embed.media
        if isinstance(media, models.AppBskyEmbedImages_View):
            return bool(media.images)
        if isinstance(media, models.AppBskyEmbedVideo_View):
            return True

    return False


def filter_original_media_posts(feed_posts: List[models.AppBskyFeedDefs_FeedViewPost]):
    filtered = []
    for fp in feed_posts:
        if fp.reason is not None:
            continue

        post_view = fp.post
        if not has_media(post_view):
            continue

        filtered.append(fp)

    return filtered


def fetch_recent_posts(client: Client, actor_handle: str, limit: int = 50):
    logging.info("Posts ophalen van %s (limit=%d)...", actor_handle, limit)
    feed = client.get_author_feed(
        actor=actor_handle,
        limit=limit,
        filter="posts_no_replies",
    )
    raw_posts = list(feed.feed or [])
    filtered_posts = filter_original_media_posts(raw_posts)

    logging.info(
        "Totaal %d posts in feed, %d over na filter (eigen + media).",
        len(raw_posts),
        len(filtered_posts),
    )
    return filtered_posts


def choose_posts_for_run(feed_posts, num_random_older: int = 2):
    if not feed_posts:
        return []

    selected = []
    newest = feed_posts[0]
    selected.append(newest)

    older = feed_posts[1:]
    if older:
        k = min(num_random_older, len(older))
        random_older = random.sample(older, k=k)
        selected.extend(random_older)

    return selected


def unrepost_if_needed_and_repost(client: Client, feed_post) -> None:
    post_view = feed_post.post

    uri = post_view.uri
    cid = post_view.cid
    viewer = post_view.viewer
    repost_uri = getattr(viewer, "repost", None) if viewer else None

    if repost_uri:
        logging.info(
            "  Post %s is al gerepost. Oude repost wordt verwijderd: %s",
            uri,
            repost_uri,
        )
        try:
            client.delete_repost(repost_uri)
            logging.info("  Oude repost verwijderd.")
        except Exception as e:
            logging.warning(
                "  Kon oude repost niet verwijderen (%s): %s",
                repost_uri,
                e,
            )

    logging.info("  Nieuwe repost van %s...", uri)
    try:
        client.repost(uri=uri, cid=cid)
        logging.info("  Repost gelukt.")
    except Exception as e:
        logging.error("  Repost mislukt voor %s: %s", uri, e)


def process_account(label: str, target_handle: str) -> None:
    logging.info("=== Account %s starten ===", label)
    client = get_client_for_account(label)
    if not client:
        logging.warning("Account %s wordt overgeslagen.", label)
        return

    try:
        feed_posts = fetch_recent_posts(client, target_handle)
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
            "Geen geschikte posts gevonden voor %s, account %s slaat run over.",
            target_handle,
            label,
        )
        return

    to_repost = choose_posts_for_run(feed_posts, num_random_older=2)

    logging.info(
        "Account %s gaat %d posts (nieuwste + random oudere) (opnieuw) repost-en.",
        label,
        len(to_repost),
    )

    for feed_post in to_repost[::-1]:
        unrepost_if_needed_and_repost(client, feed_post)


def main():
    logging.info("Target handle: %s", TARGET_HANDLE)

    for label in ACCOUNT_KEYS:
        process_account(label, TARGET_HANDLE)

    logging.info("Multi-reposter run voltooid voor %s.", TARGET_HANDLE)


if __name__ == "__main__":
    main()