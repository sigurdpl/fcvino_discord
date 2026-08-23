"""Messages the bot keeps up to date in place.

A Discord channel cannot invoke a slash command, so a channel dedicated to one
thing — the league table, the trips archive — is the bot posting a single message
and editing it from then on. That gives a channel holding exactly one
always-current message rather than a growing pile of stale ones.

Two properties make it behave itself across restarts:

* the message id is stored in `bot_messages`, so a restart carries on editing the
  same message instead of posting a second one;
* a content fingerprint is stored beside it, so a message whose content has not
  changed is left completely alone — no "(edited)" marks for nothing.
"""

from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)


async def publish(
    bot,
    kind: str,
    *,
    embed: discord.Embed,
    fingerprint: str,
    pin_reason: str,
) -> None:
    """Bring the `kind` message up to date in every guild configured for it.

    Does nothing where no channel is set, where the channel has gone, or where
    the fingerprint matches what is already displayed.
    """
    for guild_id, channel_id in bot.db.configured_guilds(kind):
        guild = bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if not isinstance(channel, discord.TextChannel):
            continue
        await _publish_one(
            bot,
            kind,
            guild_id,
            channel,
            embed=embed,
            fingerprint=fingerprint,
            pin_reason=pin_reason,
        )


async def _publish_one(
    bot,
    kind: str,
    guild_id: int,
    channel: discord.TextChannel,
    *,
    embed: discord.Embed,
    fingerprint: str,
    pin_reason: str,
) -> None:
    """Edit, or post and pin, the `kind` message in one channel.

    `guild_id` is passed rather than read off `channel.guild` so the caller's
    view of which guild this is stays authoritative.
    """
    db = bot.db
    existing = db.get_bot_message(guild_id, kind)

    if (
        existing
        and existing["content_hash"] == fingerprint
        and existing["channel_id"] == channel.id
    ):
        return

    if existing and existing["channel_id"] == channel.id:
        try:
            message = await channel.fetch_message(existing["message_id"])
            await message.edit(embed=embed)
            db.set_bot_message(
                guild_id,
                kind,
                channel_id=channel.id,
                message_id=message.id,
                content_hash=fingerprint,
            )
            return
        except discord.NotFound:
            # Someone deleted it; fall through and post a fresh one.
            log.info("%s message %s is gone, reposting", kind, existing["message_id"])
            db.clear_bot_message(guild_id, kind)
        except discord.HTTPException:
            log.exception("could not edit the %s message", kind)
            return

    try:
        message = await channel.send(embed=embed)
    except discord.HTTPException:
        log.exception("could not post the %s message", kind)
        return
    db.set_bot_message(
        guild_id,
        kind,
        channel_id=channel.id,
        message_id=message.id,
        content_hash=fingerprint,
    )
    try:
        await message.pin(reason=pin_reason)
    except discord.HTTPException:
        # Pinning needs Manage Messages; the message works fine unpinned.
        log.info("could not pin the %s message in #%s", kind, channel.name)
