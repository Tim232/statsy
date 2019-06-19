import asyncio
import datetime
import inspect
import logging
import os
import platform
import random
import requests
import sys
import traceback
from collections import defaultdict

import aiohttp
import brawlstats
import clashroyale
import datadog
import discord
import psutil
from discord.ext import commands
from dotenv import find_dotenv, load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from ext import utils
from ext.context import CustomContext
from ext.view import CustomView
from ext.command import command
from ext.utils import InvalidPlatform, InvalidBSTag, InvalidTag, NoTag, APIError
from ext.log import LoggingHandler
from locales.i18n import Translator


_ = Translator('Core', __file__)


class Statsy(commands.AutoShardedBot):
    """Custom client for statsy made by Kyber"""
    emoji_servers = [
        376364364636094465,
        376368487037140992,
        376364990023729152,
        377742732501843968,
        376365022752014345,
        386872710225068042,
        389369383639842817,
        390674234239746055,
        454111856983015424,
        454113083217149972,
        454117186823258112,
        521586528909000744
    ]

    developers = [
        325012556940836864,
        180314310298304512,
        273381165229146112
    ]

    def __init__(self):
        super().__init__(case_insensitive=True, command_prefix=None)
        self.session = aiohttp.ClientSession(loop=self.loop)
        self.mongo = AsyncIOMotorClient(os.getenv('mongo'))
        self.uptime = datetime.datetime.utcnow()
        self.process = psutil.Process()
        self.remove_command('help')
        self.messages_sent = 0
        self.maintenance_mode = False
        self.psa_message = None
        self.default_game = defaultdict(lambda: 'Clash_Royale')
        try:
            self.dev_mode = platform.system() != 'Linux' and sys.argv[1] != '-d'
        except IndexError:
            self.dev_mode = True

        if not self.dev_mode:
            self.backup_task_loop = self.loop.create_task(self.backup_task())
            self.datadog_loop = self.loop.create_task(self.datadog())

        self.load_extensions()
        self._add_commands()

        self.error_hook = discord.Webhook.from_url(
            os.getenv('error_hook'),
            adapter=discord.AsyncWebhookAdapter(self.session)
        )
        self.log_hook = discord.Webhook.from_url(
            os.getenv('log_hook'),
            adapter=discord.AsyncWebhookAdapter(self.session)
        )
        self.guild_hook = discord.Webhook.from_url(
            os.getenv('guild_hook'),
            adapter=discord.AsyncWebhookAdapter(self.session)
        )
        self.command_logger = logging.getLogger('statsy.commands')
        self.main_logger = logging.getLogger('statsy.main')
        self.main_logger.addHandler(LoggingHandler(logging.INFO))

        try:
            self.loop.run_until_complete(self.start(os.getenv('token')))
        except discord.LoginFailure:
            print('Invalid token')
        except KeyboardInterrupt:
            pass
        except Exception:
            print('Fatal exception')
            traceback.print_exc(file=sys.stderr)
        finally:
            requests.get(f'https://fourjr-herokustartup/logout/{os.getenv("HEROKU_APP_NAME")}')
            if not self.dev_mode:
                self.backup_task_loop.cancel()
                self.clan_update.cancel()
                self.datadog_loop.cancel()
                self.event_notifications_loop.cancel()
            self.loop.run_until_complete(self.logout())
            self.loop.run_until_complete(self.session.close())
            self.loop.close()

    def get_game_emojis(self):
        emojis = []
        for id_ in self.emoji_servers:
            g = self.get_guild(id_)
            for e in g.emojis:
                emojis.append(e)
        return emojis

    def _add_commands(self):
        """Adds commands automatically"""
        for _, attr in inspect.getmembers(self):
            if isinstance(attr, commands.Command):
                self.add_command(attr)

    def load_extensions(self, cogs=None, path='cogs.'):
        """Loads the default set of extensions or a seperate one if given"""
        base_extensions = [x.replace('.py', '') for x in os.listdir('cogs') if x.endswith('.py')]
        for extension in cogs or base_extensions:
            try:
                self.load_extension(f'{path}{extension}')
                print(f'Loaded extension: {extension}')
            except Exception:
                print(f'LoadError: {extension}')
                traceback.print_exc()

    def restart(self):
        """Forcefully restart the bot."""
        os.execv(sys.executable, ['python'] + sys.argv)

    async def get_prefix(self, message):
        """Returns the prefix."""

        if self.dev_mode:
            return ['./', './', './']

        id = getattr(message.guild, 'id', None)

        cfg = await self.mongo.config.guilds.find_one({'guild_id': str(id)}) or {}

        prefixes = [
            f'<@{self.user.id}> ',
            f'<@!{self.user.id}> ',
            cfg.get('prefix', '!')
        ]

        return prefixes

    async def on_connect(self):
        """Called when the bot has established a
        gateway connection with discord
        """
        print('----------------------------')
        print('Statsy connected!')
        print('----------------------------')
        datadog.statsd.increment('statsy.connect')
        self.blacklist = await self.mongo.config.admin.find_one({'_id': 'blacklist'})
        async for g in self.mongo.config.guilds.find({'default_game': {'$exists': True}}):
            self.default_game[int(g['guild_id'])] = g['default_game']
        print('Guild syncing complete')

    async def on_shard_ready(self, shard_id):
        self.main_logger.info(f'Shard {shard_id} ready')
        print(f'Shard {shard_id} ready')

    async def on_ready(self):
        """Called when guild streaming is complete
        and the client's internal cache is ready.
        """
        fmt = 'Statsy is ready!\n' \
              '----------------------------\n' \
              f'Logged in as: {self.user}\n' \
              f'Client ID: {self.user.id}\n' \
              '----------------------------\n' \
              f'Guilds: {len(self.guilds)}\n' \
              f'Shards: {self.shard_count}\n' \
              f'Users: {len(self.users)}\n' \
              '----------------------------'
        self.game_emojis = self.get_game_emojis()
        self.main_logger.info(fmt)
        print(fmt)
        if not self.dev_mode:
            await self.log_hook.send(f'```{fmt}```')

    async def on_shard_connect(self, shard_id):
        """Called when a shard has successfuly
        connected to the gateway.
        """
        print(f'Shard `{shard_id}` ready!')
        print('----------------------------')

    async def on_command(self, ctx):
        """Called when a command is invoked."""
        if not ctx.command.hidden:
            datadog.statsd.increment('statsy.commands', 1, [
                f'command:{ctx.command.name}',
                f'user:{ctx.author.id}',
                f'guild:{getattr(ctx.guild, "id", "DM")}',
                f'prefix:{"|".join([str(ord(i)) for i in ctx.prefix])}',
                f'channel_type:{type(ctx.channel).__name__}'
            ])
        self.command_logger.info(f'{ctx.message.content} - {ctx.author}')

    async def process_commands(self, message):
        """Utilises the CustomContext subclass of discord.Context"""
        await self.wait_until_ready()
        ctx = await self.get_context(message)

        if ctx.prefix is None:
            return

        try:
            blacklist = [
                str(ctx.author.id) in self.blacklist['users'],
                str(ctx.channel.id) in self.blacklist['channels'],
                str(getattr(ctx.guild, 'id', None)) in self.blacklist['guilds']
            ]
            if any(blacklist):
                return
        except AttributeError:
            pass

        if ctx.command:
            if self.maintenance_mode is True:
                if message.author.id not in self.developers:
                    return await ctx.send('The bot is under maintenance at the moment!')
            else:
                await self.invoke(ctx)
        else:
            if isinstance(ctx.channel, discord.TextChannel):
                default_cog = self.get_cog(self.default_game[ctx.guild.id])
            else:
                default_cog = self.get_cog(self.default_game[ctx.channel.id])

            ctx.message.content = ctx.prefix + default_cog.alias + ctx.message.content.replace(ctx.prefix, '', 1).strip()
            ctx = await self.get_context(ctx.message)
            if ctx.command:
                if self.maintenance_mode is True:
                    if message.author.id not in self.developers:
                        return await ctx.send('The bot is under maintenance at the moment!')
                else:
                    await self.invoke(ctx)

    async def get_context(self, message, *, cls=CustomContext):
        """Overwrites the default StringView for space insensitivity
        Original: https://github.com/Rapptz/discord.py/blob/rewrite/discord/ext/commands/bot.py#L810-L879
        """

        view = CustomView(message.content)
        ctx = cls(prefix=None, view=view, bot=self, message=message)

        if self._skip_check(message.author.id, self.user.id):
            return ctx

        prefix = await self.get_prefix(message)
        invoked_prefix = prefix

        if isinstance(prefix, str):
            if not view.skip_string(prefix):
                return ctx
        else:
            try:
                # if the context class' __init__ consumes something from the view this
                # will be wrong.  That seems unreasonable though.
                if message.content.startswith(tuple(prefix)):
                    invoked_prefix = discord.utils.find(view.skip_string, prefix)
                else:
                    return ctx

            except TypeError:
                if not isinstance(prefix, list):
                    raise TypeError("get_prefix must return either a string or a list of string, "
                                    "not {}".format(prefix.__class__.__name__))

                # It's possible a bad command_prefix got us here.
                for value in prefix:
                    if not isinstance(value, str):
                        raise TypeError("Iterable command_prefix or list returned from get_prefix must "
                                        "contain only strings, not {}".format(value.__class__.__name__))

                # Getting here shouldn't happen
                raise

        invoker = view.get_word()
        ctx.invoked_with = invoker
        ctx.prefix = invoked_prefix
        ctx.command = self.all_commands.get(invoker)

        if isinstance(ctx.channel, discord.TextChannel):
            ctx.language = (await self.mongo.config.guilds.find_one({'guild_id': str(ctx.guild.id)}) or {}).get('language', 'messages')
        else:
            ctx.language = 'messages'

        return ctx

    async def on_command_error(self, ctx, error, description=None):
        error = getattr(error, 'original', error)
        ignored = (
            NoTag,
            APIError,
            discord.Forbidden,
            commands.CheckFailure,
            commands.DisabledCommand,
            clashroyale.RequestError,
            brawlstats.RequestError
        )

        if isinstance(error, commands.errors.BotMissingPermissions):
            try:
                await ctx.send(error)
            except discord.Forbidden:
                pass
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send('This command can only be used in servers.')
        elif isinstance(error, ignored):
            pass
        elif isinstance(error, InvalidBSTag):
            em = discord.Embed(title='Invalid Tag', description=error.message, color=utils.random_color())
            em.set_image(url='https://i.imgur.com/Al3Ourz.png')
            await ctx.send(embed=em)
        elif isinstance(error, (InvalidTag, InvalidPlatform)):
            await ctx.send(error.message)
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send(error)
        elif isinstance(error, commands.BadArgument) and ctx.command.name.endswith('save'):
            try:
                await ctx.invoke(ctx.command, tag=ctx.args[2])
            except Exception as e:
                await self.on_command_error(ctx, e)
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            prefix = (await self.get_prefix(ctx.message))[2]
            await ctx.send(
                embed=discord.Embed(
                    color=random.randint(0, 0xffffff),
                    title=f'``Usage: {prefix}{ctx.command.signature}``',
                    description=ctx.command.help
                )
            )
        else:
            if not description:
                await ctx.send(_('Something went wrong and we are investigating the issue now.'))
            error_message = 'Ignoring exception in command {}:\n'.format(ctx.command)
            error_message += ''.join(traceback.format_exception(type(error), error, error.__traceback__))
            error_message = f"```py\n{error_message}\n```"
            if len(error_message) > 2000:
                async with self.session.post('https://www.hastebin.com/documents', data=error_message) as resp:
                    error_message = 'https://www.hastebin.com/' + (await resp.json())['key']

            em = discord.Embed(
                color=discord.Color.orange(),
                description=error_message,
                title=ctx.message.content
            )
            em.set_footer(text=f'G: {getattr(ctx.guild, "id", "DM")} | C: {ctx.channel.id} | U: {ctx.author.id}')
            if not self.dev_mode:
                await self.error_hook.send(content=description, embed=em)
            else:
                traceback.print_exception(type(error), error, error.__traceback__)

    async def on_error(self, event, *args, **kwargs):
        error_message = f"```py\n{traceback.format_exc()}\n```"
        if len(error_message) > 2000:
            async with self.session.post('https://www.hastebin.com/documents', data=error_message) as resp:
                error_message = 'https://www.hastebin.com/' + (await resp.json())['key']

        em = discord.Embed(
            color=discord.Color.red(),
            description=error_message,
            title=event
        )

        if not self.dev_mode:
            await self.error_hook.send(embed=em)
        else:
            traceback.print_exc()

    async def on_message(self, message):
        """Called when a message is sent/recieved."""
        self.messages_sent += 1
        if not message.author.bot:
            await self.process_commands(message)

    async def backup_task(self):
        """Publish to botlists."""
        await self.wait_until_ready()
        while not self.is_closed():
            server_count = {'server_count': len(self.guilds)}
            # DBL
            await self.session.post(
                'https://discordbots.org/api/bots/347006499677143041/stats', json=server_count, headers={
                    'Authorization': os.getenv('dbl')
                }
            )
            # bots.pw
            await self.session.post(
                'https://bots.discord.pw/api/bots/347006499677143041/stats', json=server_count, headers={
                    'Authorization': os.getenv('botspw')
                }
            )
            # Bots for Discord
            await self.session.post(
                'https://botsfordiscord.com/api/v1/bots/347006499677143041', json=server_count, headers={
                    'Authorization': os.getenv('bfd'),
                    'Content-Type': 'application/json'
                }
            )
            await asyncio.sleep(3600)

    async def datadog(self):
        """Push to datadog"""
        await self.wait_until_ready()
        games = await self.mongo.player_tags.list_collection_names()
        while not self.is_closed():
            metrics = [
                ('statsy.latency', self.latency * 1000),
                ('statsy.guilds', len(self.guilds)),
                ('statsy.shards', self.shard_count),
                ('statsy.users', len(self.users)),
                ('statsy.channels', len([i.id for g in self.guilds for i in g.channels])),
                ('statsy.memory', self.process.memory_full_info().uss / 1024**2),
                ('statsy.tags_saved', sum([await self.mongo.player_tags[i].count_documents({}) for i in games])),
                ('statsy.cache', len(self.get_cog('Clash_Royale').cache), ['game:clashroyale']),
                ('statsy.cache', len(self.get_cog('Clash_Of_Clans').cache), ['game:clashofclans']),
                ('statsy.cache', len(self.get_cog('Brawl_Stars').cache), ['game:brawlstars']),
                ('statsy.claninfo', await self.mongo.config.guilds.count_documents({'claninfo': {'$exists': True}})),
                ('statsy.tournament', await self.mongo.config.guilds.count_documents({'tournament': {'$exists': True}}))
            ]
            for i in metrics:
                try:
                    tags = i[2]
                except IndexError:
                    tags = None
                datadog.statsd.gauge(i[0], i[1], tags)

            # Languages
            for i in _.translations.keys():
                if i == 'messages':
                    continue
                num = await self.mongo.config.guilds.count_documents({'language': i})
                datadog.statsd.gauge('statsy.language', num, [f'language: {i}'])

            await asyncio.sleep(60)

    @command()
    async def ping(self, ctx):
        """Pong! Returns average shard latency."""
        em = discord.Embed(
            title=_('Pong! Websocket Latency:'),
            description=f'{self.latency * 1000:.4f} ms',
            color=0xf9c93d
        )
        try:
            await ctx.send(embed=em)
        except discord.Forbidden:
            await ctx.send(em.title + em.description)


if __name__ == '__main__':
    load_dotenv(find_dotenv())
    requests.get(f'https://fourjr-herokustartup/login/{os.getenv("HEROKU_APP_NAME")}')

    logger = logging.getLogger('discord')
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
    handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
    logger.addHandler(handler)

    logger = logging.getLogger('commands')
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(filename='bot.log', encoding='utf-8', mode='w')
    handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
    logger.addHandler(handler)

    datadog.initialize(api_key=os.getenv('api_key'), app_key=os.getenv('app_key'))
    Statsy()
