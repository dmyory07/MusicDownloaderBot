import math
import os
from pathlib import Path

from youtubesearchpython.__future__ import VideosSearch

import re
import requests
from requests import HTTPError

from rich.pretty import pprint
from rich import inspect, print

from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.markdown import escape_md

from yaml import load, dump, Loader

from odesli.Odesli import Odesli
from yt_dlp import YoutubeDL
from spotipy import Spotify, SpotifyClientCredentials

from tiddl.core.api import TidalAPI, TidalClient, models, exceptions
from tiddl.core.utils import get_track_stream_data
from tiddl.core.metadata import add_track_metadata
from tiddl.cli.utils.auth import load_auth_data
from tiddl.cli.commands.auth import login

from os import mkdir, remove, walk, PathLike
from os.path import exists

from logger import Logger
from tidal_auth import start_pkce_auth, finish_pkce_auth, refresh_pkce_token

# TODO:
# - Add support for playlists/albums for Spotify and Youtube
#   - Ability to stop downloading
# - Multiple pages in search results
# - Add metadata to MP3 files
# - Spotify search
# - "Cache" channel with songs
# - Migrate to aiogram v3


url_regex = r"^(https?:\/\/)?([\da-z\.-]+\.[a-z\.]{2,6})(.*)\/?#?$"
youtube_domains = ("m.youtube.com", "youtube.com", "www.youtube.com", "youtu.be", "music.youtube.com")
spotify_domains = ("open.spotify.com",)
spotify_regex = {
    "track": r"(?:https:\/\/open\.spotify\.com\/playlist\/|spotify:playlist:)([a-zA-Z0-9]+)",
    "album": r"(?:https:\/\/open\.spotify\.com\/album\/|spotify:album:)([a-zA-Z0-9]+)"
}
quality_suffixes = {
    "LOW": "", "HIGH": "", "LOSSLESS": " 🅻", "HI_RES_LOSSLESS": " 🅷",
}
stream_suffixes = {
    "STEREO": "🆂", "DOLBY_ATMOS": "🅳",
}

log = Logger()
config = load(open("config.yml"), Loader=Loader)
bot = Bot(config["bot_token"])
dp = Dispatcher(bot)


odesli = Odesli()

ytdl = YoutubeDL({
    "format": "bestaudio/best",
    "outtmpl": "cache/%(id)s.%(ext)s",
    "cookiefile": "cookies.txt",
    "postprocessors": [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "320",
    }],
})

spotify = Spotify(auth_manager=SpotifyClientCredentials(
    client_id=config["spotify_id"],
    client_secret=config["spotify_secret"]
))


auth_data = load_auth_data()
if not auth_data.token:
    login_url = start_pkce_auth()
    print("Login URL:", login_url)
    resp = input()
    result = finish_pkce_auth(resp)
    pprint(result)
    auth_data = load_auth_data()
    # login()
    # auth_data = load_auth_data()

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


def parse_duration(string: str) -> int:
    split = list(map(int, string.split(":")))
    if len(split) == 2:
        return split[0]*60 + split[1]
    elif len(split) == 3:
        return split[0]*3600 + split[1]*60 + split[2]
    else:
        return 0


async def handle_song(message: types.Message, song, meta, song_link: str):
    log.info(f"Downloading [blue]{song.id}[/]")
    await message.edit_text("⏳ Downloading...")
    ytdl.download(list(song.linksByPlatform.values())[:1])

    log.info(f"Saving thumbnail for [blue]{song.id}[/]")
    thumb = save_url(meta.thumbnailUrl, f"cache/{song.id}.jpg")

    log.info(f"Sending [blue]{song.id}[/]")
    await message.edit_text("⏳ Uploading...")
    await message.answer_audio(types.InputFile(f"cache/{song.id}.mp3"),
                               caption=f"_[song\\.link]({song_link})_",
                               parse_mode="MarkdownV2",
                               performer=meta.artistName,
                               title=meta.title,
                               thumb=open(thumb, "rb"))
    await message.delete()
    remove(thumb)
    remove(f"cache/{song.id}.mp3")


async def handle_youtube(message: types.Message, url: str):
    new = await message.answer("⏳ Acquiring metadata...")
    result = odesli.getByUrl(url)
    yt = result.songsByProvider["youtube"]
    meta = result.songsByProvider["youtube"]
    if "spotify" in result.songsByProvider.keys():
        meta = result.songsByProvider["spotify"]
    else:
        log.warn(f"No Spotify link found for {url}")

    await handle_song(new, yt, meta, result.songLink)


async def handle_inline(message: types.Message, url: str):
    message.edit_reply_markup(types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton(text=f"⏳ Acquiring metadata...", callback_data=f"nothing")
    ))

    result = odesli.getByUrl(url)
    yt = result.songsByProvider["youtube"]
    meta = result.songsByProvider["youtube"]
    if "spotify" in result.songsByProvider.keys():
        meta = result.songsByProvider["spotify"]
    else:
        log.warn(f"No Spotify link found for {url}")
        log.info(f"Downloading [blue]{yt.id}[/]")
    
    message.edit_reply_markup(types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton(text=f"⏳ Downloading...", callback_data=f"nothing")
    ))
    ytdl.download(list(yt.linksByPlatform.values())[:1])

    log.info(f"Saving thumbnail for [blue]{yt.id}[/]")
    thumb = save_url(meta.thumbnailUrl, f"cache/{yt.id}.jpg")

    log.info(f"Sending [blue]{yt.id}[/]")
    message.edit_reply_markup(types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton(text=f"⏳ Uploading...", callback_data=f"nothing")
    ))
    await message.edit_media(types.InputMedia(f"cache/{yt.id}.mp3"),
                               caption=f"_[song\\.link]({result.songLink})_",
                               parse_mode="MarkdownV2",
                               performer=meta.artistName,
                               title=meta.title,
                               thumb=open(thumb, "rb"), reply_markup=types.InlineKeyboardMarkup.clean())
    remove(thumb)
    remove(f"cache/{yt.id}.mp3")


def track_search(query: str, limit: int = 10, offset: int = 0):
    search = tidal.client.fetch(
        models.Search,
        "search",
        {"countryCode": tidal.country_code, "query": query, "offset": offset, "limit": limit},
        expire_after=0x0D0E0200020704, # DO_NOT_CACHE
    )
    return search.tracks

def tidal_download(track_id: str) -> tuple[models.Track, Path]:
    track_stream = tidal.get_track_stream(track_id, "HI_RES_LOSSLESS")
    stream_data, file_extension = get_track_stream_data(track_stream)

    filename = f"{track_id}_{track_stream.audioQuality}"
    track_path = Path(filename).with_suffix(file_extension)

    track_path.write_bytes(stream_data)
    track = tidal.get_track(track_id)
    # add_track_metadata(track_path, track)
    return track, track_path

def get_artists(track: models.Track, detailed: bool = False) -> str:
    return ", ".join([artist.name if not detailed
                      else f"[{escape_md(artist.name)}](https://tidal.com/artist/{artist.id})"
                      for artist in track.artists])


@dp.message_handler(regexp=url_regex)
async def handle_url(message: types.Message):
    # TODO: Add support for playlists (so far only Spotify and Youtube)
    new = await message.reply("⏳ Acquiring metadata...")
    try:
        log.info(f"Got URL: [blue]{message.text}[/] from [blue]{message.from_user.full_name}[/] / [blue]{message.from_id}[/]")
        result = odesli.getByUrl(message.text)
        if "youtube" not in result.songsByProvider.keys():
            log.warn(f"No YouTube link found for [yellow]{message.text}[/]")
            await new.edit_text("⚠ Song not found!")
            return
        yt = result.songsByProvider["youtube"]
        meta = result.songsByProvider["youtube"]
        if "spotify" in result.songsByProvider.keys():
            meta = result.songsByProvider["spotify"]
        else:
            log.warn(f"No Spotify link found for [yellow]{message.text}[/]")
        await handle_song(new, yt, meta, result.songLink)
    except HTTPError as e:
        if e.response.status_code >= 400 and e.response.status_code < 500:
            log.warn(f"Code {e.response.status_code} for {message.text}")
            log.warn(e.response.json())
            await new.edit_text("⚠ Song not found!")
            return
        else:
            log.console.print_exception()
            await new.edit_text("⚠ Unknown HTTP error occurred!")
    except Exception:
        log.console.print_exception()
        await new.edit_text("⚠ Unknown error occurred!")
        return


@dp.message_handler()
async def handle_text(message: types.Message):
    log.info(f"Got text: [blue]{message.text}[/] from [blue]{message.from_user.full_name}[/] / [blue]{message.from_user.id}[/]")
    log.info(f"Searching [blue]{message.text}[/]")
    new = await message.reply("⏳ Searching...")

    results = track_search(message.text)
    log.info(f"Got {len(results.items)} results")
    if len(results.items) == 0:
        await new.edit_text("🔎 No results found")
        return

    buttons = []
    for track in results.items:
        stream = tidal.get_track_stream(track.id, "HI_RES_LOSSLESS")
        suffix = quality_suffixes[stream.audioQuality] + " " + stream_suffixes[stream.audioMode]
        buttons.append([
            types.InlineKeyboardButton(text=f"{track.title} - {get_artists(track)}{suffix}", callback_data=f"tidal_{track.id}")
        ])
    await new.edit_text("🔎 Search results", reply_markup=types.InlineKeyboardMarkup(row_width=1, inline_keyboard=buttons))

def file_size(path: PathLike) -> str:
    size = os.path.getsize(path)
    if size <= 0:
        return "0B"

    prefixes = ["", "Ki", "Mi", "Gi", "Ti"]
    power = math.floor(math.log(size, 1024))
    prefix = prefixes[power]
    new_size = round(size / 1024 ** power, 2)
    return f"{new_size}{prefix}B"


@dp.callback_query_handler()
async def handle_callback(query: types.CallbackQuery):
    log.info(f"Got callback: [blue]{query.data}[/] from [blue]{query.from_user.full_name}[/] / [blue]{query.from_user.id}[/]")
    if query.data == "nothing":
        await query.answer()
        return
    split = query.data.split("_", maxsplit=1)
    if len(split) != 2:
        await query.answer("Invalid query specified")
        return
    category, payload = split
    match category:
        case "tidal":
            message = await query.message.answer("⏳ Downloading...")
            track, path = tidal_download(payload)
            log.info(f"Saved {track.title} ({track.id}) for [blue]{query.from_user.full_name}[/] / [blue]{query.from_user.id}[/]")

            log.info(f"Sending [blue]{track.id}[/]")
            await message.edit_text("⏳ Uploading...")
                                       # thumb=open(thumb, "rb"))
            await message.answer(f"""{get_artists(track, True)} \\- {escape_md(track.title)} \\(`{track.id}`\\)
**BPM**: {track.bpm}
**Album**: [{escape_md(track.album.title)}](https://tidal.com/album/{track.album.id})
**Quality**: {track.audioQuality}
**Size**: {escape_md(file_size(path))}""", parse_mode="MarkdownV2", disable_web_page_preview=True)

            await message.answer_audio(types.InputFile(path),
                                       # caption=f"_[song\\.link]({song_link})_",
                                       parse_mode="MarkdownV2",
                                       performer=get_artists(track),
                                       title=track.title)
            await message.delete()
            remove(path)


        case "download":
            await handle_youtube(query.message, payload)
        case "inline":
            await handle_inline(query.message, payload)
        case _:
            await query.answer("Invalid query specified")


@dp.inline_handler()
async def inline_handler(inline_query: types.InlineQuery):
    # inspect(inline_query)
    query = inline_query.query.strip()
    if query == "":
        return

    search = VideosSearch(query, limit=config["search_limit"])
    print(query)
    results = (await search.next())["result"]
    if len(results) == 0:
        return

    query_results = []
    for i, vid in enumerate(results):
        thumb = vid["thumbnails"][0]
        query_results.append(
            types.InlineQueryResultArticle(
                id=str(i),
                title=vid["title"],
                description=vid["duration"],
                thumb_url=thumb["url"],
                thumb_height=thumb["height"],
                thumb_width=thumb["width"],
                input_message_content=types.InputTextMessageContent(
                    f"⬇ _[YouTube]({vid['link']})_",
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True
                ),
                reply_markup=types.InlineKeyboardMarkup().add(
                    types.InlineKeyboardButton(text=f"Download", callback_data=f"inline_{vid['link']}")
                )
            )
        )
    await bot.answer_inline_query(inline_query.id, results=query_results, cache_time=1)


if __name__ == "__main__":
    log.info("Starting polling...")
    executor.start_polling(dp, skip_updates=True)
