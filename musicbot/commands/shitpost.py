from musicbot.commands import command
from musicbot.commands.music import cmd_play
from musicbot.structures import Response


@command("surprise")
async def cmd_surprise(self, player, channel, author, permissions, redis):
    url = redis.srandmember("musicbot:played")

    if url:
        return await cmd_play(self, player, channel, author, permissions, None, url)
    else:
        return Response(
            "There are no songs that can be replayed. Play a few songs and try this command again.",
            delete_after=25
        )
