import hashlib
import json
import math
import os
import re
import requests
import traceback

from typing import Optional

import asyncio
from concurrent.futures import ThreadPoolExecutor

from aiogram import Bot, Dispatcher, executor, types
from aiogram.bot.api import TelegramAPIServer
from aiogram.utils.markdown import escape_md

from os import mkdir, remove, walk, PathLike
from os.path import exists
from pathlib import Path

from rich import inspect, print
from rich.pretty import pprint

from spotipy import Spotify, SpotifyClientCredentials

from tiddl.core.api import TidalAPI, TidalClient, models
from tiddl.core.utils import get_track_stream_data
from tiddl.core.metadata import add_track_metadata
from tiddl.cli.utils.auth import load_auth_data

from yaml import load, dump, Loader

from logger import Logger
from tidal_auth import start_pkce_auth, finish_pkce_auth, refresh_pkce_token, check_pkce_token

url_regex = r"^(https?:\/\/)?([\da-z\.-]+\.[a-z\.]{2,6})(.*)\/?#?$"
domains = {
    # "youtube": ("m.youtube.com", "youtube.com", "www.youtube.com", "youtu.be", "music.youtube.com"),
    "spotify": ("open.spotify.com",),
    "tidal": ("www.tidal.com", "tidal.com",),
}
patterns = {
    # "youtube": dict(),
    "spotify": {
        "track": r"(?:https?:\/\/open\.spotify\.com\/track\/|spotify:track:)([a-zA-Z0-9]+)",
        "playlist": r"(?:https:\/\/open\.spotify\.com\/playlist\/|spotify:playlist:)([a-zA-Z0-9]+)",
        "album": r"(?:https:\/\/open\.spotify\.com\/album\/|spotify:album:)([a-zA-Z0-9]+)"
    },
    "tidal": {
        "track": r"(?:https?:\/\/(?:www\.)?tidal\.com\/(?:browse\/)?track\/|tidal:track:)(\d+)",
        "album": r"(?:https?:\/\/(?:www\.)?tidal\.com\/(?:browse\/)?album\/|tidal:album:)(\d+)",
        "playlist": r"(?:https?:\/\/(?:www\.)?tidal\.com\/(?:browse\/)?playlist\/|tidal:playlist:)([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
    }
}

quality_suffixes = {
    "LOW": "", "HIGH": "", "LOSSLESS": " 🅻", "HI_RES_LOSSLESS": " 🅷",
}

log = Logger()
config = load(open("config.yml"), Loader=Loader)
# bot = Bot(config["bot_token"], server=TelegramAPIServer.from_base("http://localhost:8081"))
bot = Bot(config["bot_token"])
dp = Dispatcher(bot)
search_cache: dict[str, str] = {}

auth_data = load_auth_data()
if not auth_data.token:
    login_url = start_pkce_auth()
    print("Login URL:", login_url)
    resp = input("Code or URL > ")
    result = finish_pkce_auth(resp)
    pprint(result)
    auth_data = load_auth_data()

api_executor = ThreadPoolExecutor(max_workers=16)
download_executor = ThreadPoolExecutor(max_workers=16)

tidal = TidalAPI(
    TidalClient(
        token=auth_data.token,
        cache_name="./tidal_cache",
    ),
    country_code=auth_data.country_code,
    user_id=auth_data.user_id,
)
check_pkce_token(tidal)

spotify = Spotify(auth_manager=SpotifyClientCredentials(
    client_id=config["spotify_id"],
    client_secret=config["spotify_secret"]
))


if not exists("cache"):
    mkdir("cache")
else:
    if w := walk("cache"):
        log.info("Clearing cache...")
        for f in w:
            for file in f[2]:
                log.info(f"Removing [yellow]{f[0]}/{file}[/]")
                remove(f[0] + "/" + file)


def save_url(url: str, path: str):
    r = requests.get(url)
    with open(path, "wb") as out:
        out.write(r.content)
    return path

async def tidal_search(query: str, limit: int = 10, offset: int = 0) -> models.Search.Tracks:
    # TODO: migrate to asyncio / write own parts of TidalAPI
    def blocking_search() -> models.Search:
        check_pkce_token(tidal)
        return tidal.client.fetch(
           models.Search,
           "search",
           {"countryCode": tidal.country_code, "query": query, "offset": offset, "limit": limit},
           expire_after=0x0D0E0200020704
       )

    loop = asyncio.get_running_loop()
    search = await loop.run_in_executor(api_executor, blocking_search)
    return search.tracks

async def tidal_download(track_id: str) -> tuple[models.Track, models.TrackStream, Path]:
    def blocking_download():
        check_pkce_token(tidal)

        track_stream = tidal.get_track_stream(track_id, "HI_RES_LOSSLESS")
        stream_data, file_extension = get_track_stream_data(track_stream)

        filename = f"cache/{track_id}_{track_stream.audioQuality}"
        track_path = Path(filename).with_suffix(file_extension)

        track_path.write_bytes(stream_data)
        track = tidal.get_track(track_id)
        # add_track_metadata(track_path, track)
        return track, track_stream, track_path

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(download_executor, blocking_download)
    return res

def tidal_artists(track: models.Track, detailed: bool = False) -> str:
    return ", ".join([artist.name if not detailed
                      else f"[{escape_md(artist.name)}](https://tidal.com/artist/{artist.id})"
                      for artist in track.artists])

def file_size(path: PathLike) -> str:
    size = os.path.getsize(path)
    if size <= 0:
        return "0B"

    prefixes = ["", "Ki", "Mi", "Gi", "Ti"]
    power = math.floor(math.log(size, 1024))
    prefix = prefixes[power]
    new_size = round(size / 1024 ** power, 2)
    return f"{new_size}{prefix}B"

def cache_query(query: str) -> str:
    key = hashlib.md5(query.encode()).hexdigest()[:10]
    search_cache[key] = query
    return key

async def process_search(message: types.Message, query: str, limit: int = 10, offset: int = 0):
    results = await tidal_search(query, limit, offset)
    log.info(f"Got {len(results.items)} results")
    if len(results.items) == 0:
        await message.edit_text("🔎 No results found")
        return

    buttons = []
    loop = asyncio.get_running_loop()
    for track in results.items:
        stream: models.TrackStream = await loop.run_in_executor(api_executor, tidal.get_track_stream, track.id, "HI_RES_LOSSLESS")
        suffix = quality_suffixes[stream.audioQuality]
        callback = {
            "a": "tidal",
            "t": str(track.id),
        }
        buttons.append([
            types.InlineKeyboardButton(
                text=f"{track.title} - {tidal_artists(track)}{suffix}",
                callback_data=json.dumps(callback)
            ),
        ])

    has_prev = offset > 0
    has_next = (offset + len(results.items)) < results.totalNumberOfItems

    qkey = cache_query(query)

    prev_callback = json.dumps({
        "a": "search",
        "q": qkey,
        "o": max(0, offset - limit),
    }) if has_prev else "nothing"

    next_callback = json.dumps({
        "a": "search",
        "q": qkey,
        "o": offset + limit,
    }) if has_next else "nothing"

    current_page = (offset // limit) + 1
    total_pages = math.ceil(results.totalNumberOfItems / limit) if results.totalNumberOfItems > 0 else 1

    buttons.append([
        types.InlineKeyboardButton("<" if has_prev else "-", callback_data=prev_callback),
        types.InlineKeyboardButton(f"{current_page}/{total_pages}", callback_data="nothing"),
        types.InlineKeyboardButton(">" if has_next else "-", callback_data=next_callback),
    ])

    await message.edit_text(
        "🔎 Search results",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons)
    )

def isrc_tidal(isrc: str) -> Optional[str]:
    headers = {
        "Authorization": f"Bearer {auth_data.token}",
        "Accept": "application/vnd.api+json"
    }

    payload = {
        "filter[isrc]": isrc,
        "countryCode": auth_data.country_code,
    }

    req = requests.get("https://openapi.tidal.com/v2/tracks", params=payload, headers=headers)
    if req.status_code != 200 or req.json().get("data") is None or len(req.json().get("data")) == 0:
        return None
    return req.json().get("data")[0].get("id")

async def process_tidal(message: types.Message, track_id: str):
    message = await message.answer("⏳ Downloading...")
    track, stream, path = await tidal_download(track_id)
    log.info(f"Saved {track.title} ({track.id})")

    log.info(f"Sending [blue]{track.id}[/]")
    await message.edit_text("⏳ Uploading...")

    await message.answer_audio(types.InputFile(path),
                               # caption=f"_[song\\.link]({song_link})_",
                               parse_mode="MarkdownV2",
                               performer=tidal_artists(track),
                               title=track.title)

    await message.answer(f"""{tidal_artists(track, True)} \\- {escape_md(track.title)} \\(`{track.id}`\\)
**BPM**: {track.bpm}
**Album**: [{escape_md(track.album.title)}](https://tidal.com/album/{track.album.id})
**Quality**: {escape_md(stream.audioQuality)} / {stream.bitDepth}\\-bit {escape_md(f'{stream.sampleRate/1000:g}')} kHz 
**Size**: {escape_md(file_size(path))}""", parse_mode="MarkdownV2", disable_web_page_preview=True)

    await message.delete()
    remove(path)

async def process_url(message: types.Message, provider: str, media_type: str, url: str):
    if media_type != "track":
        await message.reply(f"Unfortunately {media_type}s are not supported yet.")
        return

    loop = asyncio.get_running_loop()

    match provider:
        case "youtube":
            await message.reply("YouTube is not currently supported.")
        case "spotify":
            track = await loop.run_in_executor(api_executor, spotify.track, url)
            if not "isrc" in track["external_ids"]:
                await message.reply("Track not found.")
                # TODO: implement basic search
            isrc = track["external_ids"]["isrc"]
            track_id = await loop.run_in_executor(api_executor, isrc_tidal, isrc)
            await process_tidal(message, track_id)
        case "tidal":
            re_match = re.match(patterns["tidal"]["track"], url)
            track_id = re_match.group(1)
            await process_tidal(message, track_id)
        case _:
            await message.reply("Unsupported provider.")

async def report_exception(user: types.User, payload: str, e: Exception):
    # await user.reply("⚠ Unknown error occurred")
    await bot.send_message(user.id, "⚠ Unknown error occurred")
    log.error(f"[bold]{repr(e)}[/] occurred while trying to process [bold]{payload}[/] from [bold]{user.full_name}[/] / [bold]{user.id}[/]")
    log.console.print_exception()
    if "admin_id" not in config or config["admin_id"] is None:
        return
    await bot.send_message(config["admin_id"], f"""Exception: `{escape_md(type(e).__name__)}` occurred
Message: {escape_md(str(e))}
Payload: `{escape_md(payload)}`
User: [{escape_md(user.full_name)}](tg://user?id={user.id})
Traceback: 
```
{escape_md(traceback.format_exc())}
```""", parse_mode="MarkdownV2", disable_web_page_preview=True)


@dp.message_handler(regexp=url_regex)
async def handle_url(message: types.Message):
    try:
        re_match = re.match(url_regex, message.text)
        domain = re_match.group(2)
        for provider, urls in domains.items():
            if not domain in urls: continue
            media_type = None
            for med, tpat in patterns[provider].items():
                media_type = med if re.match(tpat, message.text) else media_type
            await process_url(message, provider, media_type, message.text)
            return
        await message.reply(f"{domain} is not currently supported.")
    except Exception as e:
        await report_exception(message.from_user, str(e), e)


@dp.message_handler()
async def handle_text(message: types.Message):
    try:
        log.info(f"Got text: [blue]{message.text}[/] from [blue]{message.from_user.full_name}[/] / [blue]{message.from_user.id}[/]")
        log.info(f"Searching [blue]{message.text}[/]")
        new = await message.reply("⏳ Searching...")
        await process_search(new, query=message.text)
    except Exception as e:
        await report_exception(message.from_user, message.text, e)


@dp.callback_query_handler()
async def handle_callback(query: types.CallbackQuery):
    try:
        log.info(f"Got callback: [blue]{query.data}[/] from [blue]{query.from_user.full_name}[/] / [blue]{query.from_user.id}[/]")
        if query.data == "nothing":
            await query.answer()
            return

        callback_data = json.loads(query.data) # TODO: check format

        match callback_data["a"]:
            case "tidal":
                await process_tidal(query.message, callback_data["t"])
            case "search":
                    original_query = search_cache.get(callback_data["q"])
                    if original_query is None:
                        await query.answer("Search expired, please search again")
                        return
                    await process_search(query.message, original_query, offset=callback_data["o"])
            case _:
                await query.answer("Invalid query specified")
    except Exception as e:
        await report_exception(query.from_user, query.data, e)


if __name__ == "__main__":
    log.info("Starting polling...")
    executor.start_polling(dp, skip_updates=True)
