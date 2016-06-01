from collections import defaultdict
from io import BytesIO
from textwrap import dedent

import asyncio
from discord import ChannelType, Forbidden, HTTPException
from musicbot.commands import all_commands, command
from musicbot.exceptions import CommandError
from musicbot.structures import Response


@command("id")
async def cmd_id(self, author, user_mentions):
    """
    Usage:
        {command_prefix}id [@user]

    Tells the user their id or the id of another user.
    """
    if not user_mentions:
        return Response('your id is `%s`' % author.id, reply=True, delete_after=35)
    else:
        usr = user_mentions[0]
        return Response("%s's id is `%s`" % (usr.name, usr.id), reply=True, delete_after=35)


@command("help")
async def cmd_help(self, command=None):
    """
    Usage:
        {command_prefix}help [command]

    Prints a help message.
    If a command is specified, it prints a help message for that command.
    Otherwise, it lists the available commands.
    """

    if command:
        cmd = all_commands.get(command, None)

        if not cmd:
            return Response("No such command", delete_after=10)

        return Response(
            "```\n{}```".format(
                dedent(cmd.__doc__),
                command_prefix=self.config.command_prefix
            ),
            delete_after=60
        )

    else:
        commands = []

        for cmd in all_commands:
            if cmd == 'help':
                continue

            commands.append("{}{}".format(self.config.command_prefix, cmd))

        helpmsg = "**Commands**\n```{commands}```".format(
            commands=", ".join(commands)
        )

        return Response(helpmsg, reply=True, delete_after=60)


@command("listids")
async def cmd_listids(self, server, author, leftover_args, cat='all'):
    """
    Usage:
        {command_prefix}listids [categories]

    Lists the ids for various things.  Categories are:
       all, users, roles, channels
    """

    cats = ['channels', 'roles', 'users']

    if cat not in cats and cat != 'all':
        return Response(
            "Valid categories: " + ' '.join(['`%s`' % c for c in cats]),
            reply=True,
            delete_after=25
        )

    if cat == 'all':
        requested_cats = cats
    else:
        requested_cats = [cat] + [c.strip(',') for c in leftover_args]

    data = ['Your ID: %s' % author.id]

    for cur_cat in requested_cats:
        rawudata = None

        if cur_cat == 'users':
            data.append("\nUser IDs:")
            rawudata = ['%s #%s: %s' % (m.name, m.discriminator, m.id) for m in server.members]

        elif cur_cat == 'roles':
            data.append("\nRole IDs:")
            rawudata = ['%s: %s' % (r.name, r.id) for r in server.roles]

        elif cur_cat == 'channels':
            data.append("\nText Channel IDs:")
            tchans = [c for c in server.channels if c.type == ChannelType.text]
            rawudata = ['%s: %s' % (c.name, c.id) for c in tchans]

            rawudata.append("\nVoice Channel IDs:")
            vchans = [c for c in server.channels if c.type == ChannelType.voice]
            rawudata.extend('%s: %s' % (c.name, c.id) for c in vchans)

        if rawudata:
            data.extend(rawudata)

    with BytesIO() as sdata:
        sdata.writelines(d.encode('utf8') + b'\n' for d in data)
        sdata.seek(0)

        # TODO: Fix naming (Discord20API-ids.txt)
        await self.send_file(
            author,
            sdata,
            filename='%s-ids-%s.txt' % (server.name.replace(' ', '_'), cat)
        )

    return Response(":mailbox_with_mail:", delete_after=20)


@command("pldump")
async def cmd_pldump(self, channel, song_url):
    """
    Usage:
        {command_prefix}pldump url

    Dumps the individual urls of a playlist
    """

    try:
        info = await self.downloader.extract_info(self.loop, song_url.strip('<>'), download=False, process=False)
    except Exception as e:
        raise CommandError("Could not extract info from input url\n%s\n" % e, expire_in=25)

    if not info:
        raise CommandError("Could not extract info from input url, no data.", expire_in=25)

    if not info.get('entries', None):
        # TODO: Retarded playlist checking
        # set(url, webpageurl).difference(set(url))

        if info.get('url', None) != info.get('webpage_url', info.get('url', None)):
            raise CommandError("This does not seem to be a playlist.", expire_in=25)
        else:
            return await cmd_pldump(self, channel, info.get(''))

    linegens = defaultdict(lambda: None, **{
        "youtube": lambda d: 'https://www.youtube.com/watch?v=%s' % d['id'],
        "soundcloud": lambda d: d['url'],
        "bandcamp": lambda d: d['url']
    })

    exfunc = linegens[info['extractor'].split(':')[0]]

    if not exfunc:
        raise CommandError("Could not extract info from input url, unsupported playlist type.", expire_in=25)

    with BytesIO() as fcontent:
        for item in info['entries']:
            fcontent.write(exfunc(item).encode('utf8') + b'\n')

        fcontent.seek(0)
        await self.send_file(
            channel,
            fcontent,
            filename='playlist.txt',
            content="Here's the url dump for <%s>" % song_url
        )

    return Response(":mailbox_with_mail:", delete_after=20)

async def cmd_perms(self, author, channel, server, permissions):
    """
    Usage:
        {command_prefix}perms

    Sends the user a list of their permissions.
    """

    lines = ['Command permissions in %s\n' % server.name, '```', '```']

    for perm in permissions.__dict__:
        if perm in ['user_list'] or permissions.__dict__[perm] == set():
            continue

        lines.insert(len(lines) - 1, "%s: %s" % (perm, permissions.__dict__[perm]))

    await self.send_message(author, '\n'.join(lines))
    return Response(":mailbox_with_mail:", delete_after=20)


@command("clean")
async def cmd_clean(self, message, channel, server, author, search_range=50):
    """
    Usage:
        {command_prefix}clean [range]

    Removes up to [range] messages the bot has posted in chat. Default: 50, Max: 1000
    """

    try:
        float(search_range)  # lazy check
        search_range = min(int(search_range), 1000)
    except:
        return Response("enter a number.  NUMBER.  That means digits.  `15`.  Etc.", reply=True, delete_after=8)

    await self.safe_delete_message(message, quiet=True)

    def is_possible_command_invoke(entry):
        valid_call = any(
            entry.content.startswith(prefix) for prefix in [self.config.command_prefix])  # can be expanded
        return valid_call and not entry.content[1:2].isspace()

    delete_invokes = True
    delete_all = channel.permissions_for(author).manage_messages or self.config.owner_id == author.id

    def check(message):
        if is_possible_command_invoke(message) and delete_invokes:
            return delete_all or message.author == author
        return message.author == self.user

    if channel.permissions_for(server.me).manage_messages:
        deleted = await self.purge_from(channel, check=check, limit=search_range, before=message)
        return Response('Cleaned up {} message{}.'.format(len(deleted), 's' * bool(deleted)), delete_after=15)

    deleted = 0
    async for entry in self.logs_from(channel, search_range, before=message):
        if entry == self.server_specific_data[channel.server]['last_np_msg']:
            continue

        if entry.author == self.user:
            await self.safe_delete_message(entry)
            deleted += 1
            await asyncio.sleep(0.21)

        if is_possible_command_invoke(entry) and delete_invokes:
            if delete_all or entry.author == author:
                try:
                    await self.delete_message(entry)
                    await asyncio.sleep(0.21)
                    deleted += 1

                except Forbidden:
                    delete_invokes = False
                except HTTPException:
                    pass

    return Response('Cleaned up {} message{}.'.format(deleted, 's' * bool(deleted)), delete_after=15)
