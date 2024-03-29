from dejavu.database import get_database, Database
import dejavu.decoder as decoder
from dejavu import fingerprint
import soundfile as sf
import multiprocessing
import os
import traceback
import sys


class Dejavu(object):

    SONG_ID = 'song_id'
    SONG_NAME = 'song_name'
    SONG_DURATION = 'song_duration'
    CREATIVE_ID = 'creative_id'
    CONFIDENCE = 'confidence'
    MATCH_TIME = 'match_time'
    OFFSET = 'offset'
    OFFSET_SECS = 'offset_seconds'

    def __init__(self, config):
        super(Dejavu, self).__init__()

        self.config = config

        # initialize db
        db_cls = get_database(config.get("database_type", None))

        self.db = db_cls(**config.get("database", {}))
        self.db.setup()

        # if we should limit seconds fingerprinted,
        # None|-1 means use entire track
        self.limit = self.config.get("fingerprint_limit", None)
        if self.limit == -1:  # for JSON compatibility
            self.limit = None
        self.get_fingerprinted_songs()

    def get_fingerprinted_songs(self):
        # get songs previously indexed
        self.songs = self.db.get_songs()
        self.songhashes_set = set()  # to know which ones we've computed before
        for song in self.songs:
            song_hash = song[Database.FIELD_FILE_SHA1]
            self.songhashes_set.add(song_hash)

    def fingerprint_directory(self, path, extensions, nprocesses=None):
        # Try to use the maximum amount of processes if not given.
        try:
            nprocesses = nprocesses or multiprocessing.cpu_count()
        except NotImplementedError:
            nprocesses = 1
        else:
            nprocesses = 1 if nprocesses <= 0 else nprocesses

        pool = multiprocessing.Pool(nprocesses)

        filenames_to_fingerprint = []
        for filename, _ in decoder.find_files(path, extensions):

            # don't refingerprint already fingerprinted files
            if decoder.unique_hash(filename) in self.songhashes_set:
                print("%s already fingerprinted, continuing..." % filename)
                continue

            filenames_to_fingerprint.append(filename)

        # Prepare _fingerprint_worker input
        worker_input = zip(filenames_to_fingerprint,
                           [self.limit] * len(filenames_to_fingerprint))

        # Send off our tasks
        iterator = pool.imap_unordered(_fingerprint_worker,
                                       worker_input)

        # Loop till we have all of them
        while True:
            try:
                song_name, hashes, file_hash = iterator.next()
            except multiprocessing.TimeoutError:
                continue
            except StopIteration:
                break
            except:
                print("Failed fingerprinting")
                # Print traceback because we can't reraise it here
                traceback.print_exc(file=sys.stdout)
            else:
                sid = self.db.insert_song(song_name, file_hash)

                self.db.insert_hashes(sid, hashes)
                self.db.set_song_fingerprinted(sid)
                self.get_fingerprinted_songs()

        pool.close()
        pool.join()

    def fingerprint_file(self, filepath, song_name=None, creative_id=None):
        songname = decoder.path_to_songname(filepath)
        song_hash = decoder.unique_hash(filepath)
        song_name = song_name or songname

        # get duration in seconds
        f = sf.SoundFile(filepath)
        duration = '{}'.format(len(f) / f.samplerate)

        # don't refingerprint already fingerprinted files
        if song_hash in self.songhashes_set:
            raise Exception("%s already fingerprinted." % song_name)
        else:
            song_name, hashes, file_hash = _fingerprint_worker(
                filepath,
                self.limit,
                song_name=song_name
            )
            sid = self.db.insert_song(song_name, file_hash, duration, creative_id)

            self.db.insert_hashes(sid, hashes)
            self.db.set_song_fingerprinted(sid)
            self.get_fingerprinted_songs()

    def find_matches(self, samples, Fs=fingerprint.DEFAULT_FS):
        hashes = fingerprint.fingerprint(samples, Fs=Fs)
        return self.db.return_matches(hashes)

    def align_matches(self, matches):
        """
            Finds hash matches that align in time with other matches and finds
            consensus about which hashes are "true" signal from the audio.

            Returns a dictionary with match information.
        """
        # align by diffs
        diff_counter = {}
        largest = 0
        largest_count = 0
        song_id = -1
        largest_matches = {}

        for tup in matches:
            sid, diff = tup
            if diff not in diff_counter:
                diff_counter[diff] = {}
            if sid not in diff_counter[diff]:
                diff_counter[diff][sid] = 0
            diff_counter[diff][sid] += 1

            if diff_counter[diff][sid] > largest_count:
                largest = diff
                largest_count = diff_counter[diff][sid]
                song_id = sid
                largest_matches[sid] = {
                    'count': largest_count,
                    'diff': diff
                }

        # extract idenfication
        song = self.db.get_song_by_id(song_id)
        if song:
            # TODO: Clarify what `get_song_by_id` should return.
            songname = song.get(Dejavu.SONG_NAME, None)
            duration = song.get(Dejavu.SONG_DURATION, None)
            creative = song.get(Dejavu.CREATIVE_ID, None)
        else:
            return None

        # return match info
        nseconds = round(float(largest) / fingerprint.DEFAULT_FS *
                         fingerprint.DEFAULT_WINDOW_SIZE *
                         fingerprint.DEFAULT_OVERLAP_RATIO, 5)
        song = {
            Dejavu.SONG_ID: song_id,
            Dejavu.SONG_NAME: songname,
            Dejavu.SONG_DURATION: duration,
            Dejavu.CREATIVE_ID: creative,
            Dejavu.CONFIDENCE: largest_count,
            Dejavu.OFFSET: int(largest),
            Dejavu.OFFSET_SECS: nseconds,
            Database.FIELD_FILE_SHA1: song.get(Database.FIELD_FILE_SHA1, None),
        }

        # fallback songs workflow
        accepted_fallbacks = []
        for key in largest_matches:
            distance = largest_matches[key]['count'] / largest_count
            # accept matches that have at least 10% of the largest confidence
            if distance >= 0.1 and song_id != key:
                # print("Largest Matches %d - %d" % (key, largest_matches[key]['count']))
                nseconds = round(float(largest_matches[key]['diff']) / fingerprint.DEFAULT_FS *
                                 fingerprint.DEFAULT_WINDOW_SIZE *
                                 fingerprint.DEFAULT_OVERLAP_RATIO, 5)

                song_fallback = self.db.get_song_by_id(key)

                accepted_fallbacks.append({
                    Dejavu.SONG_ID: key,
                    Dejavu.SONG_NAME: song_fallback.get(Dejavu.SONG_NAME, None),
                    Dejavu.SONG_DURATION: song_fallback.get(Dejavu.SONG_DURATION, None),
                    Dejavu.CREATIVE_ID: song_fallback.get(Dejavu.CREATIVE_ID, None),
                    Dejavu.CONFIDENCE: largest_matches[key]['count'],
                    Dejavu.OFFSET_SECS: nseconds,
                    Database.FIELD_FILE_SHA1: song_fallback.get(Database.FIELD_FILE_SHA1, None),
                })

        song['fallback_matches'] = accepted_fallbacks

        return song

    def recognize(self, recognizer, *options, **kwoptions):
        r = recognizer(self)
        return r.recognize(*options, **kwoptions)


def _fingerprint_worker(filename, limit=None, song_name=None):
    # Pool.imap sends arguments as tuples so we have to unpack
    # them ourself.
    try:
        filename, limit = filename
    except ValueError:
        pass

    songname, extension = os.path.splitext(os.path.basename(filename))
    song_name = song_name or songname
    channels, Fs, file_hash = decoder.read(filename, limit)
    result = set()
    channel_amount = len(channels)

    for channeln, channel in enumerate(channels):
        # TODO: Remove prints or change them into optional logging.
        # print("Fingerprinting channel %d/%d for %s" % (channeln + 1,
        #                                                channel_amount,
        #                                                filename))
        hashes = fingerprint.fingerprint(channel, Fs=Fs)
        # print("Finished channel %d/%d for %s" % (channeln + 1, channel_amount,
        #                                          filename))
        result |= set(hashes)

    return song_name, result, file_hash


def chunkify(lst, n):
    """
    Splits a list into roughly n equal parts.
    http://stackoverflow.com/questions/2130016/splitting-a-list-of-arbitrary-size-into-only-roughly-n-equal-parts
    """
    return [lst[i::n] for i in range(n)]
