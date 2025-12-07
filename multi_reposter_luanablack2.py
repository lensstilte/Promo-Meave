import os
import random
import logging
from typing import Optional, List

from atproto import Client

# ==== CONFIG PER SCRIPT ====
# Deze pas je per bestand aan (zie stap 2)
TARGET_HANDLE = "luanablack2.bsky.social"

# Accounts / secrets keys (suffix na BSKY_USERNAME_ / BSKY_PASSWORD_)
ACCOUNT_KEYS: List[str] = [
    "BEAUTYFAN",
    "BEAUTYGROUP",
    "HOTBLEUSKY",
    "BLEUSKYPROMO",
    "NSFWBLEUSKY",
]

# Basis logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


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


def has_media(post_view) -> bool:
    """
    True als de post media heeft (images / media). Geen media = text-only = skip.
    We kijken generiek naar embed, zonder specifieke modeltypes te gebruiken.
    """
    embed = getattr(post_view, "embed", None)
    if embed is None:
        return False

    # Meest voorkomende gevallen: images of record-with-media
    if getattr(embed, "images", None):
        return True

    if getattr(embed, "media", None):
        # embedRecordWithMedia: media kan zelf weer images/video bevatten
        media = embed.media
        if getattr(media, "images", None):
            return True

    # Als er andere media-types bijkomen kun je hier nog extra checks toevoegen
    return False


def is_own_original_post(feed_post, actor_handle: str) -> bool:
    """
    - Alleen echte eigen posts van de actor
    - Geen reposts (reasonRepost)
    """
    post_view = feed_post.post
    author = getattr(post_view, "author", None)
    handle = getattr(author, "handle", None)

    # Veiligheid: check dat de auteur echt de target is
    if handle and handle != actor_handle:
        return False

    # Reposts hebben een 'reason' met type ...#reasonRepost
    reason = getattr(feed_post, "reason", None)
    reason_type = getattr(reason, "$type", "") if reason else ""
    if "reasonRepost" in reason_type:
        return False

    return True


def fetch_recent_posts(client: Client, actor_handle: str, limit: int = 50):
    """
    Haal recente posts van de target op.
    - posts_no_replies: geen replies
    - filter op: eigen originele posts + met media
    """
    logging.info("Posts ophalen van %s (limit=%d)...", actor_handle, limit)
    feed = client.get_author_feed(
        actor=actor_handle,
        limit=limit,
        filter="posts_no_replies",
    )

    feed_posts = list(feed.feed or [])

    filtered = []
    for fp in feed_posts:
        post_view = fp.post
        if not is_own_original_post(fp, actor_handle):
            # eigen repost of andere onzin -> skip
            continue
        if not has_media(post_view):
            # text-only -> skip
            continue
        filtered.append(fp)

    logging.info(
        "Na filtering: %d posts over (eigen + media) voor %s.",
        len(filtered),
        actor_handle,
    )
    return filtered


def choose_posts_for_run(feed_posts, num_random_older: int = 2):
    """
    Kies:
    - altijd de nieuwste post
    - plus num_random_older willekeurige oudere posts
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


def get_post_timestamp(feed_post) -> str:
    """
    Sorteer van oud -> nieuw (zodat nieuwste als laatste wordt gerepost
    en dus bovenaan je timeline komt te staan).
    """
    post_view = feed_post.post
    record = getattr(post_view, "record", None)

    # Probeer createdAt uit de record
    created_at = getattr(record, "created_at", None) or getattr(record, "createdAt", None)
    if created_at:
        return created_at

    # Fallback: indexedAt
    return getattr(post_view, "indexed_at", None) or getattr(post_view, "indexedAt", "") or ""


def unrepost_if_needed_and_repost_with_like(client: Client, feed_post) -> None:
    """
    - Check of deze post al is gerepost en/of geliked door de huidige account
    - Zo ja: delete_repost / delete_like
    - Daarna: repost + like
    """
    post_view = feed_post.post

    uri = post_view.uri
    cid = post_view.cid
    viewer = getattr(post_view, "viewer", None)

    repost_uri = getattr(viewer, "repost", None) if viewer else None
    like_uri = getattr(viewer, "like", None) if viewer else None

    if repost_uri:
        logging.info("  Post %s is al gerepost. Oude repost wordt verwijderd: %s", uri, repost_uri)
        try:
            client.delete_repost(repost_uri)
            logging.info("  Oude repost verwijderd.")
        except Exception as e:
            logging.warning("  Kon oude repost niet verwijderen (%s): %s", repost_uri, e)

    if like_uri:
        logging.info("  Post %s is al geliked. Oude like wordt verwijderd: %s", uri, like_uri)
        try:
            client.delete_like(like_uri)
            logging.info("  Oude like verwijderd.")
        except Exception as e:
            logging.warning("  Kon oude like niet verwijderen (%s): %s", like_uri, e)

    logging.info("  Nieuwe repost van %s...", uri)
    try:
        client.repost(uri=uri, cid=cid)
        logging.info("  Repost gelukt.")
    except Exception as e:
        logging.error("  Repost mislukt voor %s: %s", uri, e)
        return

    logging.info("  Nieuwe like op %s...", uri)
    try:
        client.like(uri=uri, cid=cid)
        logging.info("  Like gelukt.")
    except Exception as e:
        logging.warning("  Like mislukt voor %s: %s", uri, e)


def process_account(label: str, target_handle: str) -> None:
    """
    Verwerk één bot-account:
    - login
    - posts ophalen (eigen + media, geen reposts)
    - nieuwste + 2 random oudere kiezen
    - gesorteerd van oud -> nieuw:
        unrepost/like (indien nodig) + opnieuw repost + like
    """
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
        logging.info("Geen geschikte posts gevonden voor %s, account %s slaat run over.", target_handle, label)
        return

    to_repost = choose_posts_for_run(feed_posts, num_random_older=2)

    # Belangrijk: van oud -> nieuw repost-en,
    # zodat de nieuwste post als laatste komt en dus bovenaan je profiel.
    to_repost_sorted = sorted(to_repost, key=get_post_timestamp)

    logging.info(
        "Account %s gaat %d posts (nieuwste + random oudere, van oud->nieuw) (opnieuw) repost-en.",
        label,
        len(to_repost_sorted),
    )

    for feed_post in to_repost_sorted:
        unrepost_if_needed_and_repost_with_like(client, feed_post)


def main():
    logging.info("Target handle: %s", TARGET_HANDLE)

    for label in ACCOUNT_KEYS:
        process_account(label, TARGET_HANDLE)

    logging.info("Multi-reposter run voltooid voor target %s.", TARGET_HANDLE)


if __name__ == "__main__":
    main()