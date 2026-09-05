import hashlib
import json
import math
import os
import re
import requests

from aiogram import Bot, Dispatcher, executor, types
from aiogram.bot.api import TelegramAPIServer
from aiogram.utils.markdown import escape_md

from rich.pretty import pprint
from rich import inspect, print

from os import mkdir, remove, walk, PathLike
from os.path import exists
from pathlib import Path

from tiddl.core.api import TidalAPI, TidalClient, models, exceptions
from tiddl.core.utils import get_track_stream_data
from tiddl.core.metadata import add_track_metadata
from tiddl.cli.utils.auth import load_auth_data

from yaml import load, dump, Loader

from logger import Logger
from tidal_auth import start_pkce_auth, finish_pkce_auth, refresh_pkce_token


url_regex = r"^(https?:\/\/)?([\da-z\.-]+\.[a-z\.]{2,6})(.*)\/?#?$"
youtube_domains = ("m.youtube.com", "youtube.com", "www.youtube.com", "youtu.be", "music.youtube.com")
spotify_domains = ("open.spotify.com",)
tidal_domains = ("tidal.com",)
spotify_regex = {
    "track": r"(?:https:\/\/open\.spotify\.com\/playlist\/|spotify:playlist:)([a-zA-Z0-9]+)",
    "album": r"(?:https:\/\/open\.spotify\.com\/album\/|spotify:album:)([a-zA-Z0-9]+)"
}
quality_suffixes = {
    "LOW": "", "HIGH": "", "LOSSLESS": " 🅻", "HI_RES_LOSSLESS": " 🅷",
}

log = Logger()
config = load(open("config.yml"), Loader=Loader)
bot = Bot(config["bot_token"], server=TelegramAPIServer.from_base("http://localhost:8081"))
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

tidal = TidalAPI(
    TidalClient(
        token=auth_data.token,
        cache_name="./tidal_cache",
    ),
    country_code=auth_data.country_code,
    user_id=auth_data.user_id,
)

try:
    session = tidal.get_session()
except exceptions.ApiError as e:
    log.warn(e.user_message)
    log.info("Refreshing token...")
    result = refresh_pkce_token(auth_data.refresh_token)
    auth_data = load_auth_data()
    tidal = TidalAPI(
        TidalClient(
            token=auth_data.token,
            cache_name="./tidal_cache",
        ),
        country_code=auth_data.country_code,
        user_id=auth_data.user_id,
    )
    log.success("Token refreshed")

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

def track_search(query: str, limit: int = 10, offset: int = 0):
    search = tidal.client.fetch(
        models.Search,
        "search",
        {"countryCode": tidal.country_code, "query": query, "offset": offset, "limit": limit},
        expire_after=0x0D0E0200020704, # DO_NOT_CACHE
    )
    return search.tracks

def tidal_download(track_id: str) -> tuple[models.Track, models.TrackStream, Path]:
    track_stream = tidal.get_track_stream(track_id, "HI_RES_LOSSLESS")
    stream_data, file_extension = get_track_stream_data(track_stream)

    filename = f"cache/{track_id}_{track_stream.audioQuality}"
    track_path = Path(filename).with_suffix(file_extension)

    track_path.write_bytes(stream_data)
    track = tidal.get_track(track_id)
    # add_track_metadata(track_path, track)
    return track, track_stream, track_path

def get_artists(track: models.Track, detailed: bool = False) -> str:
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
    results = track_search(query, limit, offset)
    log.info(f"Got {len(results.items)} results")
    if len(results.items) == 0:
        await message.edit_text("🔎 No results found")
        return

    buttons = []
    for track in results.items:
        stream = tidal.get_track_stream(track.id, "HI_RES_LOSSLESS")
        suffix = quality_suffixes[stream.audioQuality]
        callback = {
            "a": "tidal",
            "t": str(track.id),
        }
        buttons.append([
            types.InlineKeyboardButton(
                text=f"{track.title} - {get_artists(track)}{suffix}",
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


@dp.message_handler()
async def handle_text(message: types.Message):
    log.info(f"Got text: [blue]{message.text}[/] from [blue]{message.from_user.full_name}[/] / [blue]{message.from_user.id}[/]")
    log.info(f"Searching [blue]{message.text}[/]")
    new = await message.reply("⏳ Searching...")
    await process_search(new, query=message.text)


@dp.callback_query_handler()
async def handle_callback(query: types.CallbackQuery):
    log.info(f"Got callback: [blue]{query.data}[/] from [blue]{query.from_user.full_name}[/] / [blue]{query.from_user.id}[/]")
    if query.data == "nothing":
        await query.answer()
        return

    callback_data = json.loads(query.data) # TODO: check format

    match callback_data["a"]:
        case "tidal":
            message = await query.message.answer("⏳ Downloading...")
            track, stream, path = tidal_download(callback_data["t"])
            log.info(f"Saved {track.title} ({track.id}) for [blue]{query.from_user.full_name}[/] / [blue]{query.from_user.id}[/]")

            log.info(f"Sending [blue]{track.id}[/]")
            await message.edit_text("⏳ Uploading...")

            await message.answer_audio(types.InputFile(path),
                                       # caption=f"_[song\\.link]({song_link})_",
                                       parse_mode="MarkdownV2",
                                       performer=get_artists(track),
                                       title=track.title)

            await message.answer(f"""{get_artists(track, True)} \\- {escape_md(track.title)} \\(`{track.id}`\\)
**BPM**: {track.bpm}
**Album**: [{escape_md(track.album.title)}](https://tidal.com/album/{track.album.id})
**Quality**: {escape_md(stream.audioQuality)} / {stream.bitDepth}\\-bit {escape_md(f'{stream.sampleRate/1000:g}')} kHz 
**Size**: {escape_md(file_size(path))}""", parse_mode="MarkdownV2", disable_web_page_preview=True)

            await message.delete()
            remove(path)
        case "search":
                original_query = search_cache.get(callback_data["q"])
                if original_query is None:
                    await query.answer("Search expired, please search again")
                    return
                await process_search(query.message, original_query, offset=callback_data["o"])
        case _:
            await query.answer("Invalid query specified")


if __name__ == "__main__":
    log.info("Starting polling...")
    executor.start_polling(dp, skip_updates=True)
