from subprocess import DEVNULL, PIPE, Popen, run as sub_run, CalledProcessError
from threading import Thread
from pathlib import Path
from time import time
from typing import Literal, Union
from . import better_split, logger
from .type_declarations import State
from ..queue import PeekableQueue, Empty, try_

class Worker(Thread):
    process = None
    state_flag = State.UNKNOWN
    state = None
    should_exit = False
    queue = None
    signal = None
    settings = None

    def __init__(self, queue: PeekableQueue, signal: PeekableQueue) -> None:
        self.queue = queue
        self.signal = signal
        logger.debug("Creating worker thread:")
        super().__init__()

    @staticmethod
    def get_video_duration(input: Path) -> Union[float, Literal[-1]]:
        command = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 '{str(input)}'"
        try:
            output = sub_run(better_split(command), capture_output=True, check=True)
            length = float(output.stdout.decode("ascii")[:-1])
            logger.debug(f"{input} is {length} seconds long.")
            return length
        except CalledProcessError:
            logger.warning(f"Couldn't find the length {input}")
            return -1

    def run(self) -> None:
        signal = None
        logger.info("Worker started, waiting for inputs:")
        self.state_flag = State.WAITING
        while True:
            while True:
                try:
                    self.settings = try_(self.queue.get, Empty, timeout=1)
                    signal = try_(self.signal.get, Empty, timeout=1)
                except (ValueError, OSError):
                    self.should_exit = True
                    break

                if self.should_exit or self.settings or signal:
                    break
            
            if signal:
                self.handle_signal(signal)
            if self.should_exit:
                break

            assert self.settings != None, "Settings are None???"
            
            video_duration = self.get_video_duration(self.settings.input)

            running_settings = better_split(str(self.settings))
            self.state_flag = State.WORKING
            start_time = time()
            logger.info(f"Got work to do: {self.settings}")
            self.process = Popen(running_settings, stdout=PIPE, stderr=DEVNULL, text=True)

            while self.process.poll() is None:
                # self.process.stdout.readline() is blocking :/
                # i could spawn subprocesses to watch stderr and stdout *shrug*
                # TODO???: put stderr into some sort of circular buffer to show it to user
                # for now, stderr goes brr inside /dev/null
                temp = ""
                while (output := self.process.stdout.readline()) and self.process.poll() is None:
                    if "progress" not in output:
                        temp += output
                        continue
                    else:
                        temp += output
                        self.state = self.update_state(video_duration, start_time, temp)
                    try:
                        temp = ""
                        signal = try_(self.signal.get, Empty, block=False)
                    except (ValueError, OSError):
                        self.process.kill()
                        self.should_exit = True
                        break
                if self.should_exit:
                    break

            self.state_flag = State.WAITING
            logger.info(f"Work done: {self.settings}")
            if self.process.returncode:
                logger.warning(f"Subprocess exited: {self.process.returncode}")
            else:
                logger.info(f"Subprocess exited: {self.process.returncode}")
            self.settings = None
            self.state = None

        self.state_flag = State.DEAD
        logger.debug("Worker dead")


    def update_state(self, video_duration: float, time_from_start: float, output: str) -> dict:
        # frame=12                    # int       current frame of an video
        # fps=0.00                    # float     conversion speed
        # stream_0_0_q=0.0            # 
        # bitrate=N/A                 # int       current bitrate
        # total_size=44               # int       current size
        # out_time_us=0               # 
        # out_time_ms=0               # int       current frame, time in ms
        # out_time=00:00:00.000000    # 
        # dup_frames=0                # 
        # drop_frames=0               # 
        # speed=   0x                 # float     conversion speed
        # progress=continue           #           what its doing rn
        output = output.split("\n")
        timed = time() - time_from_start

        try:
            current_frame = int(output[0][6:])
            fps = float(output[1][4:])
            time_in_s = int(output[6][12:])/1e6
            conversion_speed=output[10][6:]
        except IndexError:
            return None
        
        percent = (time_in_s / video_duration) if video_duration >= 0 else 0
        
        try:
            estimated = round(((1 / percent) * timed) - timed)
            minutes, seconds = divmod(estimated, 60)
            hours, minutes = divmod(minutes, 60)
            estimated = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        except (TypeError, ZeroDivisionError):
            estimated = None

        return {"current_frame": current_frame,
                "fps": fps,
                "time_in_s": time_in_s,
                "conversion_speed": conversion_speed,
                "estimated": estimated,
                "percent": __ if (__ := round(percent*100, 2)) >= 0 else 0,}
            
    def handle_signal(self, signal: int) -> bool:
        if signal == 1:
            return True
