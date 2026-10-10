import discord


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _asset(value) -> str | None:
    return value.key if value else None


def user_json(user) -> dict:
    user = getattr(user, "_user", user)
    return {
        "id": str(user.id),
        "username": user.name,
        "global_name": user.global_name,
        "discriminator": user.discriminator,
        "avatar": _asset(user.avatar),
        "bot": user.bot,
        "system": user.system,
        "public_flags": user.public_flags.value,
    }


def member_json(member) -> dict:
    return {
        "user": user_json(member),
        "nick": member.nick,
        "avatar": _asset(member.guild_avatar),
        "roles": [str(role.id) for role in member.roles if not role.is_default()],
        "joined_at": _iso(member.joined_at),
        "premium_since": _iso(member.premium_since),
        "pending": member.pending,
        "communication_disabled_until": _iso(member.timed_out_until),
        "guild_id": str(member.guild.id),
        "status": str(member.status),
        "color": member.color.value,
    }


def member_or_user_json(value) -> dict:
    return member_json(value) if isinstance(value, discord.Member) else user_json(value)


def channel_json(channel) -> dict:
    data = {
        "id": str(channel.id),
        "type": channel.type.value,
        "name": getattr(channel, "name", None),
        "guild_id": str(channel.guild.id) if getattr(channel, "guild", None) else None,
        "position": getattr(channel, "position", None),
        "parent_id": str(channel.category_id) if getattr(channel, "category_id", None) else None,
        "topic": getattr(channel, "topic", None),
        "nsfw": channel.is_nsfw() if hasattr(channel, "is_nsfw") else False,
        "rate_limit_per_user": getattr(channel, "slowmode_delay", None),
    }
    return data


def guild_json(guild) -> dict:
    return {
        "id": str(guild.id),
        "name": guild.name,
        "icon": _asset(guild.icon),
        "banner": _asset(guild.banner),
        "description": guild.description,
        "owner_id": str(guild.owner_id),
        "member_count": guild.member_count,
        "premium_tier": guild.premium_tier,
        "premium_subscription_count": guild.premium_subscription_count,
        "preferred_locale": str(guild.preferred_locale),
        "features": list(guild.features),
        "created_at": _iso(guild.created_at),
    }


def message_json(message) -> dict:
    return {
        "id": str(message.id),
        "channel_id": str(message.channel.id),
        "guild_id": str(message.guild.id) if message.guild else None,
        "author": user_json(message.author),
        "content": message.content,
        "timestamp": _iso(message.created_at),
        "edited_timestamp": _iso(message.edited_at),
        "tts": message.tts,
        "mention_everyone": message.mention_everyone,
        "mentions": [user_json(user) for user in message.mentions],
        "attachments": [
            {
                "id": str(attachment.id),
                "filename": attachment.filename,
                "size": attachment.size,
                "url": attachment.url,
                "content_type": attachment.content_type,
            }
            for attachment in message.attachments
        ],
        "embeds": [embed.to_dict() for embed in message.embeds],
        "pinned": message.pinned,
        "type": message.type.value,
        "referenced_message_id": (str(message.reference.message_id) if message.reference else None),
    }
