#!/usr/bin/env python3
#
# Continuously download YouTube audio as MP3 into ./out
# Proper song metadata ripped from Spotify
#
# Requirements:
#   - www.youtube.com_cookies.txt (export youtube cookies with https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
#   - spotify_credentials.txt (create an app at https://developer.spotify.com/dashboard)
#         1. CLIENT_ID
#         2. CLIENT_SECRET
#   - pip install yt-dlp mutagen requests
#   - ffmpeg installed and on PATH

import base64
import os
import re
from pathlib import Path

import requests
import yt_dlp
from mutagen.id3 import ID3, ID3NoHeaderError, TPE1, TIT2, TALB, TDRC, APIC

OUT_DIR = Path("out")
CREDS_FILE = Path("spotify_credentials.txt")

RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
CYAN = "\033[96m"
BLUE = "\033[36m"
GOLD = "\033[33m"
RESET = "\033[0m"

if os.name == "nt":
    os.system("") # enable ANSI colors in older Windows terminals

# Spotify auth + lookup

def load_spotify_credentials():
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret

    if CREDS_FILE.exists():
        lines = [l.strip() for l in CREDS_FILE.read_text().splitlines() if l.strip()]
        if len(lines) >= 2:
            return lines[0], lines[1]

    print("\nNo Spotify API credentials found (needed to tag from Spotify links).")
    print("Get free credentials at https://developer.spotify.com/dashboard")
    client_id = input("  Client ID (or leave blank to skip Spotify tagging): ").strip()
    if not client_id:
        return None, None
    client_secret = input("  Client Secret: ").strip()
    if not client_secret:
        return None, None

    save = input("  Save these to spotify_credentials.txt for next time? [Y/n]: ").strip().lower()
    if save in ("", "y", "yes"):
        CREDS_FILE.write_text(f"{client_id}\n{client_secret}\n")

    return client_id, client_secret

def get_spotify_token(client_id, client_secret):
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {auth}"},
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]

def extract_spotify_track_id(url):
    match = re.search(r"track/([a-zA-Z0-9]+)", url)
    if not match:
        raise ValueError(f"Could not find a track ID in Spotify URL: {url}")
    return match.group(1)

def fetch_spotify_track(url, token):
    track_id = extract_spotify_track_id(url)
    resp = requests.get(
        f"https://api.spotify.com/v1/tracks/{track_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    artists = [a["name"] for a in data["artists"]]
    title = data["name"]
    album = data["album"]["name"]
    year = data["album"]["release_date"][:4]  # release_date is 'YYYY' or 'YYYY-MM-DD'
    images = data["album"]["images"]
    artwork_url = images[0]["url"] if images else None

    return {
        "artists": artists,
        "title": title,
        "album": album,
        "year": year,
        "artwork_url": artwork_url,
    }

# Metadata formatting (only lists first author in author tag)

def format_feat(extra_artists):
    if len(extra_artists) == 1:
        joined = extra_artists[0]
    elif len(extra_artists) == 2:
        joined = f"{extra_artists[0]} & {extra_artists[1]}"
    else:
        joined = ", ".join(extra_artists[:-1]) + f" & {extra_artists[-1]}"
    return f" (feat. {joined})"

def build_tag_values(track_info):
    artists = track_info["artists"]
    title = track_info["title"]

    main_artist = artists[0]
    extra_artists = artists[1:]

    final_title = title
    if extra_artists:
        already_has_feat = any(marker in title for marker in ("(F", "(f", "(w", "[F", "[f", "[w"))
        if not already_has_feat:
            feat_str = format_feat(extra_artists)
            if " - " in title:
                idx = title.index(" - ")
                final_title = title[:idx] + feat_str + title[idx:]
            else:
                final_title = title + feat_str

    return main_artist, final_title

def apply_spotify_tags(mp3_path, track_info):
    main_artist, final_title = build_tag_values(track_info)

    try:
        id3 = ID3(mp3_path)
    except ID3NoHeaderError:
        id3 = ID3()

    id3.setall("TPE1", [TPE1(encoding=3, text=[main_artist])])
    id3.setall("TIT2", [TIT2(encoding=3, text=[final_title])])
    id3.setall("TALB", [TALB(encoding=3, text=[track_info["album"]])])
    id3.setall("TDRC", [TDRC(encoding=3, text=[track_info["year"]])])

    if track_info["artwork_url"]:
        img_resp = requests.get(track_info["artwork_url"], timeout=15)
        img_resp.raise_for_status()
        id3.delall("APIC")
        id3.add(APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,  # front cover
            desc="Cover",
            data=img_resp.content,
        ))

    id3.save(mp3_path, v2_version=3)
    extra_artist_count = len(track_info["artists"]) - 1
    return main_artist, final_title, extra_artist_count

# YouTube download

def download_audio(url):
    OUT_DIR.mkdir(exist_ok=True)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(OUT_DIR / "%(title)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "noplaylist": True,
        "remote_components": ["ejs:github"],
        "extractor_args": {"youtube": {"player_client": ["default"]}},
        "quiet": False,
        "no_warnings": False,
    }

    cookies_path = Path("www.youtube.com_cookies.txt")
    if cookies_path.exists():
        ydl_opts["cookiefile"] = str(cookies_path)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        mp3_path = Path(filename).with_suffix(".mp3")
        return mp3_path

# Main loop

def main():
    print(f"Downloading to '{OUT_DIR}'.")
    print(f"args: <{RED}youtube_url{RESET}>                      : Download audio")
    print(f"args: <{RED}youtube_url{RESET}> <{GREEN}spotify_track_url{RESET}>  : Download audio w/ Spotify tags")
    print(f"Press {YELLOW}Enter{RESET} with no input to quit.\n")

    spotify_token = None
    spotify_creds_tried = False

    count = 0
    while True:
        line = input("Input: ").strip()

        if line == "":
            break

        parts = line.split()
        youtube_url = parts[0]
        spotify_url = parts[1] if len(parts) > 1 else None

        try:
            if spotify_url:
                if spotify_token is None and not spotify_creds_tried:
                    spotify_creds_tried = True
                    client_id, client_secret = load_spotify_credentials()
                    if client_id and client_secret:
                        spotify_token = get_spotify_token(client_id, client_secret)

                if spotify_token is None:
                    print("  No Spotify credentials available; downloading without tagging.\n")
                    download_audio(youtube_url)
                    count += 1
                    print(f"  Done. ({CYAN}{count}{RESET} downloaded so far)\n")
                    continue

                track_info = fetch_spotify_track(spotify_url, spotify_token)
                mp3_path = download_audio(youtube_url)
                main_artist, final_title, extra_artist_count = apply_spotify_tags(mp3_path, track_info)
                count += 1
                print(f"  Tagged: {BLUE}{main_artist}{RESET} - {GOLD}{final_title}{RESET}")
                print(f"  Done. ({CYAN}{count}{RESET} downloaded so far)\n")
                if extra_artist_count > 0:
                    print(f"  {YELLOW}Note: multiple artists on this track -- double check the title formatting above.{RESET}")
            else:
                download_audio(youtube_url)
                count += 1
                print(f"  Done. ({CYAN}{count}{RESET} downloaded so far)\n")

        except Exception as e:
            print(f"  Failed: {e}\n")

    print(f"\nFinished. {CYAN}{count}{RESET} track(s) downloaded to '{OUT_DIR}'.")

if __name__ == "__main__":
    main()