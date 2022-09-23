#!/usr/bin/env python3

from decimal import Decimal
from fractions import Fraction
import hashlib
import logging
import os
from pathlib import Path
import queue
import shutil
import threading
from typing import Any, Callable, DefaultDict, Optional
import ffmpeg
import json
import asyncio
import concurrent
import multiprocessing
from pymediainfo import MediaInfo


from . import database as DB

TARGET_VIDEO_MAX_BITRATE = 2.5*(10**6)
TARGET_AUDIO_BITRATE = 128*(10**3)
TARGET_VIDEO_MAX_W = '1920'

# Bug(?) workaround. Otherwise p.exitcode=1 always when multiprocessing is spawned in a thread. See:
# https://stackoverflow.com/questions/67273533/processes-in-python-3-9-exiting-with-code-1-when-its-created-inside-a-threadpoo
multiprocessing.set_start_method('forkserver', force=True)


# Used for returning multiprocessing results in a queue
class ProcessingResult:
    def __init__(self, orig_file: Path, file_owner_id: str, success: bool, msg: str = None, video_hash: str = None):
        self.orig_file = orig_file
        self.video_hash = video_hash
        self.file_owner_id = file_owner_id
        self.success = success
        self.msg = msg
    
    def __repr__(self) -> str:
        return f"ProcessingResult(orig_file={self.orig_file}, video_hash={self.video_hash}, file_owner_id={self.file_owner_id}, success={self.success}, msg={self.msg})"

class VideoProcessor:
    def __init__(self, db_file: Path, logger: logging.Logger = None) -> None:
        self.logger = logger or logging.getLogger("vp")
        self.db_file = db_file


    def convert_video(self, src: Path, dst: Path, logger: logging.Logger, orig_bit_rate: int, orig_codec: str, error_q: Optional[multiprocessing.Queue] = None) \
        -> Optional[tuple[Path, Path]]:
        """
        Convert & scale down video to with ffmpeg-python, if necessary.

        Args:
            src: Path to the source video file
            dst: Path to the destination video file
            logger: logger to use
            orig_bit_rate: original video bit rate (for skipping conversion if not necessary)
            orig_codec: original video codec (for skipping conversion if not necessary)

        Returns:
            tuple(stdout_log: Path, stderr_log: Path) -- logs from FFmpeg, or None if no conversion was necessary

        Raises:
            Exception: if the conversion fails (also writes ffmpeg output to a log)
        """

        try:
            logger.info(f"Converting '{src}' to '{dst}'...")
            assert src.exists()

            fn_stdout = dst.parent / 'encoder.stdout'
            fn_stderr = dst.parent / 'encoder.stderr'

            newbitrate = max(int(orig_bit_rate/2), min(int(orig_bit_rate), TARGET_VIDEO_MAX_BITRATE))
            if (newbitrate >= orig_bit_rate or orig_bit_rate <= 1.2*TARGET_VIDEO_MAX_BITRATE) and \
                orig_codec.lower() in ('h264', 'hevc', 'h265', 'avc') and src.suffix.lower() in ('.mp4', '.mkv'):
                    logger.info(f"Keeping original video codec '{orig_codec}' because new bitrate is lower than target. Copying instead of transcoding.")
                    shutil.copy(src, dst)
                    logger.debug(f"Video copied ok'")
            else:
                try:
                    logger.info(f"Transcoding video '{src}' with new bitrate {newbitrate} as '{dst}'...")
                    out, err = ffmpeg \
                        .input(filename=src.absolute()) \
                        .output(filename=dst.absolute(), 
                            vcodec='libx264', preset='faster', 
                            vf=f'scale={TARGET_VIDEO_MAX_W}:-8',
                            map=0,          # copy all streams
                            acodec='aac',
                            ac=2,           # stereo
                            strict='experimental',
                            **{'b:v': newbitrate, 'b:a': TARGET_AUDIO_BITRATE}) \
                        .global_args('-nostdin', '-hide_banner', '-nostats') \
                        .overwrite_output()  \
                        .run(capture_stdout=True, capture_stderr=True)

                    fn_stdout.write_bytes(out or b'')
                    fn_stderr.write_bytes(err or b'')
                    logger.debug(f"Conversion done")
                    
                    return fn_stdout, fn_stderr

                except ffmpeg.Error as e:
                    fn_stdout.write_bytes(bytes(e.stdout))
                    fn_stderr.write_bytes(bytes(e.stderr))
                    msg = f"FFMPEG error converting video '{src}' to '{dst}'. See '{fn_stderr}' and '{fn_stdout}' for details."
                    logger.error(msg)
                    raise Exception(msg)

        except Exception as e:
            if error_q:
                error_q.put_nowait(e)
            else:
                raise e

        return None


    def read_video_metadata(self,
        src: Path,
        video_hash: str,
        logger: logging.Logger,
        fmt_result: Callable[[str, bool], ProcessingResult],
        test_mock: dict = {}) \
            -> tuple[Optional[ProcessingResult], str, int]:
        """
        Read video metadata with ffmpeg-python and write it to the database.

        Args:
            src:        Path to the source video file
            video_hash: Hash (unique id) of the video file
            logger:     Logger to use
            fmt_result: Function to post a ProcessingResult, if error occurs
            test_mock:  Overrides for testing
        
        Returns:
            tuple(result: ProcessingResult, orig_codec: str, orig_bitrate: int) -- result is None if no error occurred
        """

        try:
            logger.debug(f"Reading metadata for '{src}'...")

            video = None
            mediainfo = MediaInfo.parse(src.absolute())
            for track in mediainfo.tracks:
                if track.track_type == "Video" and not test_mock.get('no_video_stream'):
                    for x in ('frame_count', 'frame_rate', 'height', 'width', 'duration', 'format'):
                        if x not in track.to_data() or test_mock.get('missing_mediainfo_fields'):
                            raise ValueError(f"No field '{x}' in video track")                    
                    video = track
                    break
            if not video:
                return fmt_result("No video stream found in '{src}'. Giving up.", False), 'None', 0
        except Exception as e:
            return fmt_result(f"Error reading mediainfo for '{src}': {e}", False), 'None', 0

        # Calc duration and bitrate (if not found in mediainfo)
        duration_sec = Decimal(video.duration) / Decimal(1000)
        bit_rate = video.to_data().get('bit_rate') or video.to_data().get('nominal_bit_rate')
        if not bit_rate or test_mock.get('no_bit_rate'):
            logger.warning(f"No bit rate found for '{src}'. Calculating it from file size.")
            bit_rate = int(src.stat().st_size * 8 / duration_sec)

        logger.debug(f"Metadata for '{src}': codec='{video.format}', fps='{video.frame_rate}', bit_rate='{int(bit_rate)}', frame_count='{video.frame_count}', duration='{duration_sec}'")

        try:
            logger.debug(f"Writing metadata to database...")
            async def add_video_to_db():
                logger.debug(f"Opening DB '{self.db_file}'...")
                async with DB.Database(Path(self.db_file), logger) as db:
                    assert not db.error_state, f"DB error state {db.error_state}"
                    logger.debug(f"db.add_video ...")
                    await db.add_video(DB.Video(
                        video_hash=video_hash,
                        added_by_userid=src.owner(),
                        added_by_username=src.owner(),       # TODO: get username from user id (wrap LDAP in some kind of abstraction)
                        orig_filename=src.name,
                        total_frames=video.frame_count,
                        duration=Decimal(duration_sec),
                        fps=str(video.frame_rate),
                        raw_metadata_video=json.dumps(video.to_data()),
                        raw_metadata_all=mediainfo.to_json(),
                    ))
            asyncio.run(add_video_to_db())
            logger.debug(f"Metadata wrote")
        except Exception as e:
            return fmt_result(f"Error inserting video info into DB: '{e}'", False), video.format, int(bit_rate)

        return None, video.format, int(bit_rate)


    def process_file(self,
        src: Path,
        dst_dir: Path,
        user_id=None,
        test_mock: dict = {}) -> ProcessingResult:
        """
        Process a video file: recompress and get metadata.            
        Args:
            src:         Path to the source video file
            dst_dir:     Path to the destination directory
            user_id:     User id of the submitter. Defaults to file owner.
            test_mock:   Overrides for testing
        Returns:
            ProcessingResult
        """
        logger = logging.getLogger(f"vp.wrk_{os.getpid()}")
        try:
            user_id = user_id or src.owner()

            # Hash = Filename + user_id + size + first 32k of contents
            def calc_video_hash(fn: Path) -> str:
                file_hash = hashlib.sha256((str(fn) + str(user_id) + str(fn.stat().st_size)).encode('utf-8'))
                with open(fn, 'rb') as f:
                    file_hash.update(f.read(32*1024))
                hash = file_hash.hexdigest()
                assert len(hash) >= 8
                return hash[:8]

            video_hash = calc_video_hash(src)
            new_dir = dst_dir / video_hash
            logger.debug(f"Video_hash for '{src}' = '{video_hash}. New dir: '{new_dir}'")

            # Helper for returning results through multiporcessing queue
            def fmt_result(msg: str, success: bool) -> ProcessingResult:
                if success:
                    logger.info(f"Succesfully processed '{src}' -> '{new_dir}'")
                else:
                    logger.error(f"Error processing '{src}' -> '{new_dir}': {msg}")
                return ProcessingResult(
                    orig_file=src,
                    file_owner_id=user_id,
                    success=success,
                    video_hash=video_hash,
                    msg=msg)

            if test_mock.get('preexisting_dir'):
                new_dir.mkdir(parents=True, exist_ok=True)

            # Check if video is already processed
            if new_dir.exists():
                async def lookup_existing():
                    async with DB.Database(Path(self.db_file), logger) as db:
                        return await db.get_video(video_hash)
                old_vid = asyncio.run(lookup_existing())
                
                if old_vid:
                    assert old_vid.added_by_userid == user_id, \
                        f"Hash collision?!? Video '{video_hash}' already owned by '{old_vid.added_by_userid}'."
                    res = fmt_result(f"You already have this video.", True)
                    src.unlink() # Just delete the original. It's already in /videos anyway.
                    return res
                else:
                    logger.warning(f"Dir for '{video_hash}' exists, but not in DB. Deleting old and reprocessing.")
                    shutil.rmtree(new_dir)

            # Move video to video dir
            logger.debug(f"Creating dir '{new_dir}'...")
            new_dir.mkdir(parents=False, exist_ok=True)

            dir_for_orig = new_dir / "orig"
            assert not (dir_for_orig / src.name).exists(), f"File '{src.name}' already exists in '{dir_for_orig}'. Aborting."
            logger.debug(f"Creating dir '{dir_for_orig}'...")
            dir_for_orig.mkdir(parents=False)

            logger.debug(f"Moving '{src}' to '{dir_for_orig}'...")
            shutil.move(src, dir_for_orig)
            assert (dir_for_orig / src.name).exists(), f"Failed to move '{src}' to {dir_for_orig}. Aborting."
            src = dir_for_orig / src.name       # update src to point to the new location

            opt_res, orig_codec, orig_bitrate = self.read_video_metadata(src, video_hash, logger, fmt_result)
            if opt_res:
                assert not opt_res.success, "read_video_metadata should not return success"
                return opt_res
            
            # Convert video to mp4 with ffmpeg
            mp4_file = new_dir / "video.mp4"
            
            errq = multiprocessing.Queue()  # type: multiprocessing.Queue[Exception]
            p = multiprocessing.Process(target=self.convert_video, args=(src, mp4_file, logger, orig_bitrate, orig_codec, errq))
            p.start()
            p.join()
            if not errq.empty():
                e = errq.get()
                return fmt_result(f"FFMPEG error converting video:: {e}", False)
            elif p.exitcode != 0:
                return fmt_result(f"FFMPEG subprocess exitcode={p.exitcode}. Got no exception.", False)
            else:
                logger.debug(f"FFMPEG subprocess finished successfully")
                async def mark_recompressed():
                    async with DB.Database(Path(self.db_file), logger) as db:
                        assert not db.error_state, f"DB error state {db.error_state}"
                        await db.set_video_recompressed(video_hash)
                asyncio.run(mark_recompressed())

            return fmt_result("Video processing complete", True)

        except Exception as e:
            logger.error(f"General processing error '{str(src)}' to : {e}")
            return ProcessingResult(
                orig_file=src,
                file_owner_id=user_id,
                success=False,
                msg=f"Generic video processing error: {e}")


    def cleanup_and_move_to_rejected(self, orig_src: Path, video_hash: Optional[str], dst_dir: Path, reject_dir: Path) -> None:
        """
        Move the video to the rejected directory, and delete the video hash directory (if it exists).

        Args:
            orig_src:       Original source path
            video_hash:     ID/hash of the video
            dst_dir:        Directory for succesfully processed videos
            reject_dir:     Directory for rejected videos
        
        Raises:
            AssertionError: If cleanup fails
        """        
        if video_hash:
            hash_dir = dst_dir / video_hash
            if hash_dir.exists():
                file_to_move = hash_dir / "orig" / orig_src.name
                move_to_dir = reject_dir / video_hash
                if file_to_move.exists():
                    move_to_dir.mkdir(parents=False, exist_ok=True)
                    assert not (move_to_dir / orig_src.name).exists(), f"File '{orig_src.name}' already exists in '{move_to_dir}'. Aborting cleanup."
                    shutil.move(file_to_move, move_to_dir)
                    assert not file_to_move.exists(), f"Failed to move '{file_to_move}' - still exists. Aborting cleanup."
                    assert (move_to_dir / orig_src.name).exists(), f"DISASTER! File '{file_to_move}' disappeared after moving it to '{move_to_dir}'! Aborting cleanup."
                if not file_to_move.exists():
                    shutil.rmtree(hash_dir)
                    assert not hash_dir.exists(), f"Failed to delete '{hash_dir}' - still exists. Please delete manually."
        else:
            if orig_src.exists():
                assert reject_dir.exists() and reject_dir.is_dir(), f"Reject directory '{reject_dir}' does not exist. Aborting cleanup."
                assert not (reject_dir / orig_src.name).exists(), f"File '{orig_src.name}' already exists in '{reject_dir}'. Aborting cleanup."
                shutil.move(orig_src, reject_dir)
                assert not orig_src.exists(), f"Failed to move '{orig_src}' into {reject_dir}. Please move manually."
                assert (reject_dir / orig_src.name).exists(), f"DISASTER! File '{orig_src}' disappeared after moving it to '{reject_dir}'! Please investigate."


    def monitor_incoming_folder_loop(self,
        incoming_dir: Path, 
        dst_dir: Path,
        rejected_dir: Path,
        interrupt_flag: threading.Event,
        results: queue.Queue,
        poll_interval: float,
        test_mock: dict = {}) -> None:
        """
        Monitor the incoming folder for new files and process them.
        This is a blocking function that runs in a separate thread.
        It spawns a new process for each file it finds as soon as it determines that the file is not being written to anymore.

        Args:
            incoming_dir:    Incoming videos directory
            dst_dir:         Where to store the processed videos
            rejected_dir:    Where to move rejected videos
            interrupt_flag:  Event to signal process should be interrupted
            results:         Queue to post ProcessingResults to
            poll_interval:   How often to check for new files (in seconds)        
        """
        logger = logging.getLogger(f"vp.incoming")

        incoming = Path(incoming_dir)
        logger.info(f"Starting incoming folder monitor in '{incoming}'...")

        last_tested_size: DefaultDict[Path, int] = DefaultDict(int) # For detecting files that are still being written to
        skip_list: set[Path] = set()    # For skipping files that failed to process before (and could not be moved to rejected)

        if test_mock.get("test_skip_list"):
            skip_list.add(Path("non-existent-file"))

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            while not interrupt_flag.is_set():
                logger.debug("Checking for new files...")
                process_now = []

                # Clean up skip_list (remove files that no longer exist)
                skip_list = set(filter(lambda x: x.exists(), skip_list))

                # Check for new files in the incoming folder
                for fn in incoming.iterdir():
                    if fn.is_file() and fn not in skip_list:
                        # Check if file is still being written to
                        cur_size = fn.stat().st_size
                        if cur_size == last_tested_size[fn]:
                            logger.info(f"File '{fn}' not growing any more. Processing it...")
                            process_now.append(fn)
                        else:
                            logger.info(f"File '{fn}' size changed since last poll. Skipping it for now...")
                            last_tested_size[fn] = cur_size

                # Process new files in parallel and wait for them to finish
                # (otherwise we might process the same file twice)
                if process_now:
                    for r in executor.map(lambda x: self.process_file(x, dst_dir), process_now):
                        if not r.success:
                            logger.error(f"Failed to process '{r.orig_file}': {r.msg}. Cleaning up...")
                            try:
                                self.cleanup_and_move_to_rejected(r.orig_file, r.video_hash, dst_dir, rejected_dir)
                            except Exception as e:
                                logger.error(f"Failed to cleanup after processing '{r.orig_file}':: {e}")
                                r.msg = f"{r.msg}. ALSO, failed to cleanup: {e}"
                            if r.orig_file.exists():
                                logger.error(f"File '{r.orig_file}' still exists after cleanup. Adding to skip_list, so we don't reprocess it.")
                                skip_list.add(r.orig_file)
                        results.put(r)

                interrupt_flag.wait(timeout=poll_interval)

        logger.info("Video processor stopped")
