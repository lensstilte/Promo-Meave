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


def fetch_recent_posts(client: Client, actor_handle: str, limit: int = 50):
    """
    Haal recente posts van de target op.
    We gebruiken 'posts_no_replies' zodat je alleen eigen posts pakt, geen replies.
    LET OP: hier kunnen nog steeds reposts tussen zitten, die filteren we later eruit.
    """
    logging.info("Posts ophalen van %s (limit=%d)...", actor_handle, limit)
    feed = client.get_author_feed(
        actor=actor_handle,
        limit=limit,
        filter="posts_no_replies",
    )
    # feed.feed is een lijst van FeedViewPost
    return list(feed.feed or [])


# ---------- FILTERS: alleen eigen posts mét media (foto / video) ----------

def has_image_or_video_embed(post_view) -> bool:
    """
    Check of de post een image- of video-embed heeft.
    We kijken naar post_view.embed en desnoods naar record.embed.
    We accepteren alleen embeds die duidelijk images / video zijn
    (geen pure tekst, geen link-only).
    """
    record = getattr(post_view, "record", None)
    embed = getattr(post_view, "embed", None)

    if not embed and record is not None:
        embed = getattr(record, "embed", None)

    if not embed:
        return False

    # Unwrap recordWithMedia (embed.recordWithMedia.media)
    embed_type = getattr(embed, "$type", "") or ""
    if "recordWithMedia" in embed_type:
        media = getattr(embed, "media", None)
        if media:
            embed = media
            embed_type = getattr(embed, "$type", "") or ""

    # Alleen image / video embeds
    media_keywords = ("embed.images", "embed.video")
    return any(k in embed_type for k in media_keywords)


def is_original_post(feed_post) -> bool:
    """
    Sluit reposts van andere accounts uit.
    - Geen feed_post.reason (die duidt vaak op een repost-reden).
    - Record-type moet een 'echte' app.bsky.feed.post zijn, geen feed.repost.
    """
    if getattr(feed_post, "reason", None) is not None:
        # Dit is waarschijnlijk een 'repost item' in de feed.
        return False

    post_view = feed_post.post
    record = getattr(post_view, "record", None)
    record_type = getattr(record, "$type", "") or ""

    # Echte posts zijn app.bsky.feed.post
    if "app.bsky.feed.post" not in record_type:
        return False

    return True


def filter_original_with_media(feed_posts) -> List:
    """
    Filter feed_posts:
    - Alleen echte eigen posts (geen repost items).
    - Alleen posts met foto of video.
    """
    filtered = []
    for fp in feed_posts:
        post_view = fp.post
        if not is_original_post(fp):
            continue
        if not has_image_or_video_embed(post_view):
            continue
        filtered.append(fp)

    logging.info(
        "Gefilterd: %d van %d posts over (alleen eigen posts met media).",
        len(filtered),
        len(feed_posts),
    )
    return filtered


# ---------- SELECTIE & REPOST LOGICA ----------

def choose_posts_for_run(feed_posts, num_random_older: int = 2):
    """
    feed_posts moet al gefilterd zijn (alleen eigen posts met media).
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
    viewer = getattr(post_view, "viewer", None)
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
    - posts ophalen
    - filter: alleen eigen posts met media (foto/video)
    - nieuwste + 2 random oudere kiezen
    - sorteren van oud -> nieuw
    - per gekozen post: unrepost (als nodig) + opnieuw repost
    """
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
        logging.info("Geen posts gevonden voor %s, account %s slaat run over.", target_handle, label)
        return

    # Eerst filteren op eigen posts + media
    filtered_posts = filter_original_with_media(feed_posts)
    if not filtered_posts:
        logging.info(
            "Geen eigen posts met media gevonden voor %s, account %s slaat run over.",
            target_handle,
            label,
        )
        return

    # Selecteer nieuwste + 2 random oudere
    to_repost = choose_posts_for_run(filtered_posts, num_random_older=2)

    # Sorteer van oud -> nieuw zodat de nieuwste als laatste wordt gerepost
    # indexed_at is een ISO-datumstring, sorteren oplopend geeft oud -> nieuw
    def get_indexed_at(fp):
        post_view = fp.post
        return getattr(post_view, "indexed_at", "") or ""

    to_repost_sorted = sorted(to_repost, key=get_indexed_at)

    logging.info(
        "Account %s gaat %d posts (nieuwste + random oudere) (opnieuw) repost-en (van oud naar nieuw).",
        label,
        len(to_repost_sorted),
    )

    for feed_post in to_repost_sorted:
        unrepost_if_needed_and_repost(client, feed_post)


def main():
    target_handle = get_target_handle()
    logging.info("Target handle: %s", target_handle)

    for label in ACCOUNT_KEYS:
        process_account(label, target_handle)

    logging.info("Multi-reposter run voltooid.")


if __name__ == "__main__":
    main()