import logging
import os
import pathlib
import queue
import shlex
import signal
import simpleaudio
import threading
import time
import wave
import zmq


logging.basicConfig(level=logging.INFO,
                    format='(%(threadName)s) %(levelname)s: %(message)s')

SONGS_DIR = 'songs/'
BUF_SIZE = 10
q = queue.Queue(BUF_SIZE)


class ClientThread(threading.Thread):
    def __init__(self, name):
        super(ClientThread, self).__init__()
        self.name = name
        self.socket = None
        self.playback_commands = {
            'play', 'stop', 'pause', 'resume', 'next', 'skip', 'prev', 'rm', 'info'
        }

    def run(self):
        while True:
            time.sleep(0.3)
            try:
                # shlex is used to also split quoted inputs
                user_input = shlex.split(input("> "))
            except Exception as e:
                logging.error(e)
                continue
            try:
                command = user_input[0].lower()
                args = user_input[1:]

                if command in self.playback_commands:
                    # Put the full command in the queue
                    self.put_instruction(command, args)

                elif command == 'search':
                    self.search(command, args)

                elif command == 'ls':
                    self.list_local()

                elif command == 'add':
                    if not args:
                        raise ValueError

                    # List containing the available songs
                    if new_args := self.download(args):
                        # Put instruction to add the songs to the playlist
                        self.put_instruction(command, new_args)

                elif command == 'del':
                    if not args:
                        raise ValueError
                    self.delete(args)
                    self.put_instruction(command, args)

                elif command == 'exit':
                    signal.raise_signal(signal.SIGINT)

                else:
                    logging.warning(f"Command '{command}' not supported")

            except IndexError:
                logging.warning("Enter a valid command")
            except ValueError:
                logging.warning("Provide the name of the songs")
                logging.info(f"usage: {command} 'song1' 'song2' ...")
            except Exception as e:
                logging.error(e)

    def connect(self, ip):
        """Connects the client to the server using its ip address."""
        context = zmq.Context()
        self.socket = context.socket(zmq.REQ)
        self.socket.connect(f'tcp://{ip}:5555')

    def search(self, command, args):
        """Lists all the files stored in the server, if no query is given."""
        self.socket.send_json({'command': command, 'args': args})
        reply = self.socket.recv_json()
        if reply['files']:
            print(*reply['files'], sep='\n')
        else:
            logging.info(f"No files containing {args} were found")

    def list_local(self):
        """Lists all the local files that are stores in the songs directory."""
        files = os.listdir(SONGS_DIR)
        print(*files, sep='\n')

    def download(self, args):
        """Returns a list containing all the available songs, which are the
        ones that were successfully downloaded or are already downloaded."""
        new_args = []
        for filename in args:
            # First check if it's already downloaded
            if os.path.exists(f"{SONGS_DIR}{filename}"):
                new_args.append(filename)
                logging.debug(f"'{filename}' already downloaded")
                continue

            # Download the song from the server
            self.socket.send_json({'command': 'down', 'args': filename})
            # Check if the first chunk is not empty
            if data := self.socket.recv():
                logging.debug(f"Downloading '{filename}'...")
                with open(f'{SONGS_DIR}{filename}', 'ab') as file:
                    file.write(data)  # Write the first chunk
                    self.socket.send_string('ok')
                    # Receive chunks until not empty
                    while data := self.socket.recv():
                        file.write(data)
                        self.socket.send_string('ok')

                new_args.append(filename)
                logging.debug(f"'{filename}' downloaded")
            else:
                logging.warning(f"'{filename}' not found or it's empty")

        return new_args

    def delete(self, args):
        """Deletes all the songs listed in args."""
        for filename in args:
            try:
                os.remove(f"{SONGS_DIR}{filename}")
                logging.debug(f"'{filename}' deleted")
            except OSError:
                logging.error(f"Can't delete '{filename}', not found")

    def put_instruction(self, command, args):
        """Puts an instruction in the queue,
        which has a command and a list of args."""
        if command == 'play' and args:
            args = self.download(args)
            if not args:
                return

        playback_instruction = {'command': command, 'args': args}
        q.put(playback_instruction)


class PlayerThread(threading.Thread):
    def __init__(self, name):
        super(PlayerThread, self).__init__()
        self.name = name
        self.playlist = []
        self.temp_playlist = []
        self.playlist_info = False
        self.playlist_end = False
        self.playback_thread = None
        self.thread_running = False
        self.song_name = None
        self.current_song = None
        self.index = 0
        self.update_index = True
        self.stopped = False
        self.paused = False

    def run(self):
        while True:
            if not q.empty():
                instruction = q.get()
                command = instruction['command']
                args = instruction['args']

                if command == 'add':
                    self.add(args)

                elif command == 'play':
                    if self.paused:
                        self.resume_song()  # Using play to unpause a song
                        continue

                    self.play(args)

                elif command == 'stop':
                    self.stop()
                elif command == 'pause':
                    self.pause_song()
                elif command == 'resume':
                    self.resume_song()
                elif command == 'prev':
                    self.switch_song(-1)
                elif command in ('next', 'skip'):
                    self.switch_song(1)
                elif command in ('del', 'rm'):
                    self.remove(args)
                elif command == 'info':
                    self.print_playlist()
                    logging.debug(f"Current song: {self.song_name}")

    def add(self, args):
        """Adds the songs listed in args to the playlist."""
        for filename in args:
            self.playlist.append(filename)
            logging.debug(f"'{filename}' added to playlist")
        self.print_playlist()

    def play(self, args=None):
        """Handles the playback logic."""
        if self.thread_running:
            return

        if args:
            # A lambda function that checks if the filename exists
            def f(x): return True if os.path.exists(
                f"{SONGS_DIR}{x}") else logging.warning(f"'{x}' not found")
            # The temp playlist is created only once, when this
            # function is called from run() with a list of songs
            self.temp_playlist = [file for file in args if f(file)]
            if not self.temp_playlist:
                logging.info(f"Type the name of the songs from '{SONGS_DIR}', "
                             "or download them from the server using 'add'")
                return

        if not self.playlist and not self.temp_playlist:
            logging.info("Add songs to the playlist using 'add', or use "
                         "'play song1 song2 ...' to play certain songs")
            return

        if not self.playlist_info:
            # Print the playlist the first time it starts playing
            self.print_playlist()
            self.playlist_info = True

        self.playback_thread = threading.Thread(
            target=self.play_all, name='Playback')
        self.stopped = False
        self.playlist_end = False
        self.thread_running = True
        self.playback_thread.start()

    def play_all(self):
        """Plays all the songs in playlist starting from index.
        If temp_playlist is not empty, will play its songs instead."""
        while not self.playlist_end:
            self.update_index = True
            if self.stopped:
                break

            if self.temp_playlist:
                self.play_song(self.temp_playlist[self.index])
            else:
                self.play_song(self.playlist[self.index])

            if self.update_index and not self.stopped:
                # Only increment index when a song stops playing on its own
                if self.valid_index(1):  # If the next index is valid
                    self.index += 1
                else:
                    # Reset index and temp list once the last song stops playing
                    self.index = 0
                    self.temp_playlist = []
                    self.playlist_info = False
                    self.playlist_end = True

        self.thread_running = False

    def play_song(self, filename):
        """Plays a song given its filename, and checks if the
        playlist will reach its end, after the song ends"""
        try:
            wave_obj = simpleaudio.WaveObject.from_wave_file(
                f"{SONGS_DIR}{filename}")
            self.song_name = filename
            self.current_song = wave_obj.play()
            self.print_songs()
            self.current_song.wait_done()

        except FileNotFoundError:
            logging.error(f"Can't play '{filename}', not found")
            self.remove_song(filename)
        except wave.Error as e:
            logging.error(f"{e}, file extension must be '.wav'")
            self.remove(filename)
        except EOFError:
            logging.error(f"'{filename}' is empty")
            self.remove(filename)
        finally:
            self.song_name = self.current_song = None

    def valid_index(self, amount):
        """Checks whether the next (or previous) index
        of the current playlist is valid or not."""
        if self.temp_playlist:
            return True if 0 <= self.index + amount < len(self.temp_playlist) else False
        else:
            return True if 0 <= self.index + amount < len(self.playlist) else False

    def stop(self, reset=True):
        """Stops whichever song is currently playing."""
        if not self.current_song:
            logging.debug("No song is currently playing")
            return

        # This prevents a paused song from not being stopped
        # (allowing the playback_thread to terminate)
        self.resume_song()
        self.stopped = True
        simpleaudio.stop_all()
        self.paused = False

        # If playback is stopped by user
        if reset:
            self.index = 0
            self.temp_playlist = []
            self.playlist_info = False

    def pause_song(self):
        """Pauses the song that's currently playing."""
        if not self.current_song:
            logging.debug("No song is currently playing")
            return

        if self.paused:
            logging.debug(f"'{self.song_name}' is already paused")
            return

        self.current_song.pause()
        self.paused = True

    def resume_song(self):
        if self.paused:
            self.paused = False
            self.current_song.resume()

    def switch_song(self, amount):
        """Switches to the next or to the previous song."""
        if not self.current_song:
            logging.info("Play a playlist first")
            return

        if not self.valid_index(amount):
            logging.debug(f"Index {self.index + amount} is not valid")
            return

        self.stop(reset=False)  # Stop and don't reset the index
        self.playback_thread.join()  # Wait until playback_thread finishes
        # Modify the index only if update_index is True
        self.index += amount if self.update_index else 0
        self.play()

    def print_playlist(self):
        playlist = self.temp_playlist if self.temp_playlist else self.playlist
        logging.info(f"Playlist: {playlist}")

    def print_songs(self):
        playlist = self.temp_playlist if self.temp_playlist else self.playlist
        prev = playlist[self.index - 1] if self.valid_index(-1) else '-'
        next_ = playlist[self.index + 1] if self.valid_index(1) else '-'
        logging.info(f"\n{'Previous' :<20}{'Current' :^20}{'Next' :>20}"
                     f"\n{'--------' :<20}{'--------' :^20}{'--------' :>20}"
                     f"\n{prev :<20}{self.song_name :^20}{next_ :>20}")

    def remove(self, args):
        """Removes all the songs in args from the playlist."""
        if not args:
            logging.warning("Provide the name of the songs to")
            return

        for filename in args:
            self.remove_song(filename)
        self.print_playlist()

    def remove_song(self, filename):
        """Removes only a song from the playlist."""
        try:
            rm_index = self.playlist.index(filename)
            name = self.playlist.pop(rm_index)
            self.fix_index(rm_index)
            logging.debug(f"'{name}' removed from the playlist")
        except ValueError:
            logging.warning(f"'{filename}' not in playlist")

    def fix_index(self, rm_index):
        """Fixes the playlist's index when a song is removed from it"""
        if self.index > rm_index:
            self.index -= 1
            logging.info(f"updated index : {self.index}")
        elif self.index == rm_index:
            # Don't update the index if the removed song
            # is the one that's currently playing
            self.update_index = False


def main():
    pathlib.Path(SONGS_DIR).mkdir(exist_ok=True)

    client = ClientThread(name='Client')
    player = PlayerThread(name='Player')

    client.connect("localhost")

    # Add local songs to the playlist
    player.add(os.listdir(SONGS_DIR))

    signal.signal(signal.SIGINT, signal.SIG_DFL)

    client.start()
    player.start()


if __name__ == '__main__':
    main()
