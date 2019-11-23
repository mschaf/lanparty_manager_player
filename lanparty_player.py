#!/usr/bin/env python3

from __future__ import unicode_literals
import threading
import spotify
from colorama import Fore, Back, Style
import subprocess
import re
import os
import vlc
import time
import requests
import json


class PlaybackApi:
    def __init__(self, url, token):
        self.playback_url = url + 'playback.json?token=' + token
        self.songs_url = url + 'songs.json?token=' + token

    def create_playback(self):
        result = requests.post(self.playback_url)
        if not result.status_code == requests.codes.ok:
            raise
        return result.json()

    def get_playback(self):
        result = requests.get(self.playback_url)
        if not result.status_code == requests.codes.ok:
            raise
        return result.json()

    def destroy_playback(self):
        result = requests.delete(self.playback_url)
        if not result.status_code == requests.codes.ok:
            raise
        return result.json()

    def update_playback(self, current_time):
        result = requests.put(self.playback_url + '&playback%5Bcurrent_time%5D=' + str(current_time))
        if not result.status_code == requests.codes.ok:
            raise
        return result.json()

    def get_songs(self):
        result = requests.get(self.songs_url)
        if not result.status_code == requests.codes.ok:
            raise
        return result.json()


print(f"{Fore.GREEN}Starting Lanparty Music Player{Fore.WHITE}")

config = json.load(open('config.json'))
print(f"{Fore.GREEN}Using api: {Fore.WHITE}{config['api']['url']}")

session = spotify.Session()

# Process events in the background
loop = spotify.EventLoop(session)
loop.start()

# Connect an audio sink
audio = spotify.AlsaSink(session)

# Events for coordination
logged_in = threading.Event()
end_of_track = threading.Event()


def on_connection_state_updated(session):
    if session.connection.state is spotify.ConnectionState.LOGGED_IN:
        logged_in.set()

def on_end_of_track(self):
    end_of_track.set()


# Register event listeners
session.on(spotify.SessionEvent.CONNECTION_STATE_UPDATED, on_connection_state_updated)
session.on(spotify.SessionEvent.END_OF_TRACK, on_end_of_track)

# login to spotify
session.login(config['spotify']['username'], config['spotify']['password'])
logged_in.wait()
session.preferred_bitrate(spotify.Bitrate.BITRATE_320k)

playback_api = PlaybackApi(config['api']['url'], config['api']['token'])

# make and clear temp dir for youtube downloads
subprocess.run('mkdir -p youtube_download_cache', shell=True)
subprocess.run('rm -f youtube_download_cache/*', shell=True)

# destroy playback if any
try:
    playback_api.destroy_playback()
except Exception as e:
    print(f'{Fore.RED} Error communication with playback api {Fore.WHITE}')
    print(f'{Fore.YELLOW} {e} {Fore.WHITE}')
    exit(-1)


download_queue = []
downloading = None
downloaded = []

download_queue_lock = threading.Lock()

exit_scipt = threading.Event()

def song_to_name(song):
    return f"{song['title']} - {song['artist']} ({song['type']}/{song['song_id']})"

def playback_to_name(playback):
    return f"{playback['song']['title']} - {playback['song']['artist']} ({playback['playback_type']}/{playback['song']['song_id']})"

def update_download_queue():
    songs = playback_api.get_songs()
    youtube_songs = list(filter(lambda song: song['type'] == 'youtube', songs))
    not_already_downloaded_or_enqueued = list(filter(lambda song: song['id'] not in (list(map(lambda s: s['id'], downloaded)) + list(map(lambda s: s['id'], download_queue))), youtube_songs))
    download_queue_lock.acquire()
    for song in not_already_downloaded_or_enqueued:
        song['downloaded'] = threading.Event()
        download_queue.append(song)
        # print(f"{Fore.YELLOW}Adding {Fore.WHITE}{song_to_name(song)}{Fore.YELLOW} to download queue{Fore.WHITE}")
    download_queue.sort(key=lambda song: song['updated_at'])
    download_queue_lock.release()


def download_yt():
    download_successful = False
    try:
        song = download_queue[0]
        video_id = song['song_id']
        if not re.match('\A[a-zA-Z0-9_-]{11}\Z', video_id):
            raise Exception(f'invalid video id ({video_id})')
        print(f'{Fore.YELLOW}Downloading song: {Fore.WHITE}{song_to_name(song)}')
        download_result = subprocess.run(f'youtube-dl -f bestaudio --extract-audio --audio-format mp3 --audio-quality 0 --quiet -o "youtube_download_cache/{video_id}.%(ext)s" https://www.youtube.com/watch?v={video_id}', shell=True)
        if not download_result.returncode == 0:
            raise Exception(f'Download of ({song_to_name(song)}) failed')
        print(f'{Fore.YELLOW}Download finished: {Fore.WHITE}{song_to_name(song)}')

        download_successful = True
    except Exception as e:
        print(f'{Fore.RED}Download of {song_to_name(song)} failed: ({e}){Fore.WHITE}')
        download_successful = False

    song = download_queue.pop(0)
    song['downloaded'].set()
    song['download_successful'] = download_successful
    downloaded.append(song)



def yt_pre_download_task():
    while not exit_scipt.wait(1):
        update_download_queue()
        if len(download_queue) > 0:
            download_yt()


download_thread = threading.Thread(target=yt_pre_download_task, args=())
download_thread.start()

time.sleep(5)

print(f"{Fore.GREEN}Ready to play music! {Fore.WHITE}")

try:
    while True:
        playback = None
        try:
            # get next song as a playback
            playback = playback_api.create_playback()
            if 'error' in playback:
                print(f'{Fore.RED}Got error from api: {playback["error"]} waiting 10 seconds before retrying ...{Fore.WHITE}')
                time.sleep(10)
                raise Exception('Api Error')

            print(f'{Fore.BLUE}Starting playback of: {Fore.WHITE}{playback_to_name(playback)}')

            # play spotify song
            if playback['playback_type'] == 'spotify':

                track = session.get_track('spotify:track:' + playback['song']['song_id']).load()
                session.player.load(track)
                session.player.play()
                end_of_track.clear()

                current_time = 0

                while not end_of_track.wait(1):
                    if session.player.state == 'playing':
                        current_time += 1000
                        playback = playback_api.update_playback(current_time)
                    else:
                        playback = playback_api.get_playback()
                    if session.player.state != playback['state']:
                        if playback['state'] == 'playing':
                            print(f'{Fore.CYAN}Resuming playback{Fore.WHITE}')
                            session.player.play()
                        elif playback['state'] == 'paused':
                            print(f'{Fore.CYAN}Pausing playback{Fore.WHITE}')
                            session.player.pause()
                        elif playback['state'] == 'skip':
                            print(f'{Fore.CYAN}Skipping to next track{Fore.WHITE}')
                            session.player.unload()
                            end_of_track.set()

            # play youtube song
            elif playback['playback_type'] == 'youtube':
                song = None

                # look for song in download queue
                for s in download_queue:
                    if s['id'] == playback['song']['id']:
                        song = s

                # if the song is still in the queue wait up to 10 seconds to finish if not exit
                if song is not None:
                    print(f'{Fore.BLUE}Song still in download queue (waiting up to 10 seconds for completion){Fore.WHITE}')
                    if not song['downloaded'].wait(10):
                        raise Exception('Song still in queue after 10 second timeout')

                # if the song is not in the queue it should be finished
                if song is None:
                    for s in downloaded:
                        if s['id'] == playback['song']['id']:
                            song = s

                if song is None:
                    raise Exception('Download for song not found')

                if not song['download_successful']:
                    raise Exception('Download for song failed')

                mp3_path = os.path.abspath(f"youtube_download_cache/{song['song_id']}.mp3")

                p = vlc.MediaPlayer(mp3_path)
                p.play()

                while not (p.get_state() == vlc.State.Ended or p.get_state() == vlc.State.Stopped):
                    time.sleep(1)
                    if p.get_state() == vlc.State.Playing:
                        playback = playback_api.update_playback(p.get_time())
                    else:
                        playback = playback_api.get_playback()

                    playback_state = ''
                    if p.get_state() == vlc.State.Playing:
                        playback_state = 'playing'
                    elif p.get_state() == vlc.State.Paused:
                        playback_state = 'paused'

                    if playback_state != playback['state']:
                        if playback['state'] == 'playing':
                            print(f'{Fore.CYAN}Resuming playback{Fore.WHITE}')
                            p.play()
                        elif playback['state'] == 'paused':
                            print(f'{Fore.CYAN}Pausing playback{Fore.WHITE}')
                            p.pause()
                        elif playback['state'] == 'skip':
                            print(f'{Fore.CYAN}Skipping to next track{Fore.WHITE}')
                            p.stop()

                subprocess.run(f'rm {mp3_path}', shell=True)

            # skip everything else
            else:
                print(f'{Fore.RED} unknown playback type ({playback["playback_type"]}): skipping{Fore.WHITE}')

            print(f'{Fore.BLUE}Playback finished{Fore.WHITE}')
        except Exception as e:
            time.sleep(2)
            print(f'{Fore.RED}error: ({e}){Fore.WHITE}')


except KeyboardInterrupt:
    exit_scipt.set()
    playback_api.destroy_playback()
    print(f"\n{Fore.GREEN}Bye.{Fore.WHITE}")
    download_thread.join()
