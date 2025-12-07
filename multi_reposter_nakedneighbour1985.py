import os
import random
import logging

from atproto import Client

# ===== instellingen =====
TARGET_HANDLE = "nakedneighbour1985.bsky.social""

ACCOUNT_KEYS = [
    "BEAUTYFAN",
    "BEAUTYGROUP",
    "HOTBLEUSKY",
    "BLEUSKYPROMO",
    "NSFWBLEUSKY",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def get_client_for_account(label: str):
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


def is_original_post(post_view) -> bool:
    """Alleen echte eigen posts, geen repost-records."""
    record = getattr(post_view, "record", None)
    rtype = (
        getattr(record, "py_type", None)
        or getattr(record, "_type", None)
        or getattr(record, "$type", None)
        or ""
    )
    return "app.bsky.feed.post" in str(rtype)


def has_media(post_view) -> bool:
    """Alleen posts met foto of video."""
    embed = getattr(post_view, "embed", None)
    if not embed:
        return False

    etype = (
        getattr(embed, "py_type", None)
        or getattr(embed, "_type", None)
        or ""
    )

    # direct images of video
    return ("embed.images" in etype) or ("embed.video" in etype)


def fetch_recent_posts(client: Client, actor_handle: str, limit: int = 50):
    logging.info("Posts ophalen van %s (limit=%d)...", actor_handle, limit)
    feed = client.get_author_feed(
        actor=actor_handle,
        limit=limit,
        filter="posts_no_replies",
    )

    cleaned = []
    for feed_post in list(feed.feed or []):
        post_view = feed_post.post

        if not is_original_post(post_view):
            # sla reposts van andere accounts over
            continue

        if not has_media(post_view):
            # geen tekst-only posts
            continue

        cleaned.append(feed_post)

    return cleaned


def choose_posts_for_run(feed_posts, num_random_older: int = 2):
    """
    Kies:
    - altijd de nieuwste post (index 0)
    - plus num_random_older willekeurige oudere posts uit de rest
    """
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


def sort_posts_old_to_new(feed_posts):
    """Zodat de nieuwste als laatste gerepost wordt (en dus bovenaan komt)."""

    def key(fp):
        pv = fp.post
        return getattr(pv, "indexed_at", "") or ""

    return sorted(feed_posts, key=key)


def ensure_like(client: Client, post_view) -> None:
    """Zorg dat de bot-account de post geliked heeft."""
    viewer = getattr(post_view, "viewer", None)
    like_uri = getattr(viewer, "like", None) if viewer else None

    if like_uri:
        logging.info("  Post %s is al geliked (record %s).", post_view.uri, like_uri)
        return

    try:
        client.like(uri=post_view.uri, cid=post_view.cid)
        logging.info("  Like toegevoegd.")
    except Exception as e:
        logging.error("  Like mislukt voor %s: %s", post_view.uri, e)


def unrepost_if_needed_and_repost_and_like(client: Client, feed_post) -> None:
    post_view = feed_post.post

    uri = post_view.uri
    cid = post_view.cid
    viewer = getattr(post_view, "viewer", None)
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
        return

    # meteen liken
    ensure_like(client, post_view)


def process_account(label: str, target_handle: str) -> None:
    logging.info("=== Account %s starten (target=%s) ===", label, target_handle)
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
    to_repost = sort_posts_old_to_new(to_repost)

    logging.info(
        "Account %s gaat %d posts (nieuwste + random oudere) (opnieuw) repost-en.",
        label,
        len(to_repost),
    )

    for feed_post in to_repost:
        unrepost_if_needed_and_repost_and_like(client, feed_post)


def main():
    logging.info("Target handle: %s", TARGET_HANDLE)

    for label in ACCOUNT_KEYS:
        process_account(label, TARGET_HANDLE)

    logging.info("Multi-reposter run voltooid.")


if __name__ == "__main__":
    main()