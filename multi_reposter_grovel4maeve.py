import os
import random
import logging
from typing import Optional, List

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
        logging.error("TARGET_HANDLE environment variable is niet gezet.")
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
    We gebruiken 'posts_no_replies' zodat je alleen eigen posts/reposts pakt, geen replies.
    """
    logging.info("Posts ophalen van %s (limit=%d)...", actor_handle, limit)
    feed = client.get_author_feed(
        actor=actor_handle,
        limit=limit,
        filter="posts_no_replies",
    )
    return list(feed.feed or [])


def has_media(post_view) -> bool:
    """
    Alleen posts met media (foto / video) toestaan.
    We checken of er een embed met afbeeldingen of video is.
    """
    embed = getattr(post_view, "embed", None)
    if not embed:
        return False

    def is_media(e) -> bool:
        if not e:
            return False

        # Type check (images / video)
        t = getattr(e, "$type", "") or getattr(e, "_type", "")
        t = (t or "").lower()

        if "images" in t or "video" in t:
            return True

        # Als er een images-lijst op zit, is het sowieso media
        if getattr(e, "images", None):
            return True

        # Soms zit media genest in .media
        inner = getattr(e, "media", None)
        if inner and is_media(inner):
            return True

        return False

    return is_media(embed)


def filter_valid_posts(feed_posts, actor_handle: str):
    """
    Filter:
    - alleen eigen originele posts (geen reposts, geen posts van andere auteur)
    - alleen posts met media (foto / video)
    """
    valid = []
    for item in feed_posts:
        post_view = getattr(item, "post", None)
        if not post_view:
            continue

        author = getattr(post_view, "author", None)
        handle = getattr(author, "handle", None)

        # Veiligheid: alleen posts van de target-handle zelf
        if handle and handle != actor_handle:
            continue

        # Geen reposts van andere accounts: op author feed
        # krijgen reposts van de actor meestal een 'reason' mee.
        reason = getattr(item, "reason", None)
        if reason is not None:
            # Dit is een repost-record, skippen
            continue

        # Alleen met media (foto / video)
        if not has_media(post_view):
            continue

        valid.append(item)

    logging.info(
        "Na filteren: %d geldige posts (eigen + media) gevonden.",
        len(valid),
    )
    return valid


def choose_posts_for_run(feed_posts, num_random_older: int = 2) -> List:
    """
    Kies:
    - altijd de nieuwste post (index 0)
    - plus num_random_older willekeurige oudere posts uit de rest

    feed_posts moet in volgorde 'nieuwste eerst' staan.
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


def order_posts_old_to_new(selected_posts, full_feed_posts) -> List:
    """
    Zorg dat we reposten van oud -> nieuw,
    zodat de nieuwste repost-actie bovenaan de timeline komt.

    full_feed_posts: lijst van feed items in volgorde 'nieuwste eerst'.
    Index 0 = nieuwste, hoogste index = oudste.
    We sorteren selected_posts op hun index, van hoog naar laag
    (oudste eerst, nieuwste laatst).
    """
    index_map = {}
    for i, fp in enumerate(full_feed_posts):
        index_map[id(fp)] = i

    indexed = []
    for fp in selected_posts:
        idx = index_map.get(id(fp), 9999)
        indexed.append((idx, fp))

    # Oudste eerst (hoogste index), nieuwste (laagste index) als laatste
    indexed.sort(reverse=True, key=lambda x: x[0])

    ordered = [fp for _, fp in indexed]
    return ordered


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
    - filteren op: eigen + media + geen repost
    - nieuwste + 2 random oudere kiezen
    - in volgorde oud -> nieuw (nieuwste als laatste) unrepost + repost
    """
    logging.info("=== Account %s starten ===", label)
    client = get_client_for_account(label)
    if not client:
        logging.warning("Account %s wordt overgeslagen.", label)
        return

    try:
        feed_posts_raw = fetch_recent_posts(client, target_handle)
    except Exception as e:
        logging.error(
            "Kon feed voor %s niet ophalen bij account %s: %s",
            target_handle,
            label,
            e,
        )
        return

    feed_posts = filter_valid_posts(feed_posts_raw, target_handle)

    if not feed_posts:
        logging.info(
            "Geen geldige posts (eigen + media) gevonden voor %s, account %s slaat run over.",
            target_handle,
            label,
        )
        return

    selected = choose_posts_for_run(feed_posts, num_random_older=2)
    if not selected:
        logging.info(
            "Na selectie geen posts om te repost-en voor %s (account %s).",
            target_handle,
            label,
        )
        return

    # Cruciaal: eerst de oudste, dan de nieuwste (laatste) repost,
    # zodat de nieuwste repost bovenaan komt te staan.
    to_repost_ordered = order_posts_old_to_new(selected, feed_posts)

    logging.info(
        "Account %s gaat %d posts (nieuwste + random oudere) (opnieuw) repost-en "
        "in volgorde: oud -> nieuw.",
        label,
        len(to_repost_ordered),
    )

    for feed_post in to_repost_ordered:
        unrepost_if_needed_and_repost(client, feed_post)


def main():
    target_handle = get_target_handle()
    logging.info("Target handle: %s", target_handle)

    for label in ACCOUNT_KEYS:
        process_account(label, target_handle)

    logging.info("Multi-reposter run voltooid.")


if __name__ == "__main__":
    main()