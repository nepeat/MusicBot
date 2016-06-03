import discord.opus
import os
from raven import Client
from musicbot.bot import MusicBot

if not discord.opus.is_loaded():
    discord.opus.load_opus('opus')

if 'SENTRY_DSN' in os.environ:
    client = Client(dsn=os.environ["SENTRY_DSN"])

__all__ = ['MusicBot']
