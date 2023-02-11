import calicam.logger

logger = calicam.logger.get(__name__)
import logging
logger.setLevel(logging.DEBUG)

import time
from pathlib import Path
from queue import Queue
from threading import Thread, Event

import cv2
import numpy as np
from calicam.cameras.data_packets import SyncPacket

class Synchronizer:
    
    def __init__(self, streams: dict, fps_target=6):
        self.streams = streams
        self.current_synched_frames = None

        self.sync_notice_subscribers = (
            []
        )  # queues that will be notified of new synched frames
        self.synched_frames_subscribers = (
            []
        )  # queues that will receive actual frame data

        self.all_frame_packets = {}
        self.stop_event = Event()
        self.frames_complete = False  # only relevant for video playback, but provides a way to wrap up the thread

        self.ports = []
        for port, stream in self.streams.items():
            self.ports.append(port)

        self.fps_target = fps_target
        self.update_fps_targets(fps_target)
        self.fps = fps_target

        self.initialize_ledgers()
        self.start()

    def update_fps_targets(self, target):
        logger.info(f"Attempting to change target fps in streams to {target}")
        for port, stream in self.streams.items():
            stream.set_fps_target(target)

    def stop(self):
        self.stop_event.set()
        self.thread.join()
        for t in self.threads:
            t.join()

    def initialize_ledgers(self):

        self.port_frame_count = {port: 0 for port in self.ports}
        self.port_current_frame = {port: 0 for port in self.ports}
        self.mean_frame_times = []

    def start(self):

        logger.info("About to submit Threadpool of frame Harvesters")
        self.threads = []
        for port, stream in self.streams.items():
            t = Thread(target=self.harvest_frame_packets, args=(stream,), daemon=True)
            t.start()
            self.threads.append(t)
        logger.info("Frame harvesters just submitted")

        logger.info("Starting frame synchronizer...")
        self.thread = Thread(target=self.synch_frames_worker, args=(), daemon=True)
        self.thread.start()

    def subscribe_to_notice(self, q):
        # subscribers are notified via the queue that new frames are available
        # this is intended to avoid issues with latency due to multiple iterations
        # of frames being passed from one queue to another
        logger.info("Adding queue to receive notice of synched frames update")
        self.sync_notice_subscribers.append(q)

    def subscribe_to_synched_frames(self, q):
        logger.info("Adding queue to receive synched frames")
        self.synched_frames_subscribers.append(q)

    def release_synched_frames_q(self, q):
        logger.info("Releasing record queue")
        self.synched_frames_subscribers.remove(q)

    def harvest_frame_packets(self, stream):
        port = stream.port
        stream.push_to_out_q = True
        if port == 0:
            print("wait")

        logger.info(f"Beginning to collect data generated at port {port}")

        while not self.stop_event.is_set():
            frame_packet = stream.out_q.get()
            frame_index = self.port_frame_count[port]
            frame_packet.frame_index = frame_index

            self.all_frame_packets[f"{port}_{frame_packet.frame_index}"] = frame_packet
            self.port_frame_count[port] += 1

            logger.debug(
                f"Frame data harvested from reel {frame_packet.port} with index {frame_packet.frame_index} and frame time of {frame_packet.frame_time}"
            )

        logger.info(f"Frame harvester for port {port} completed")

    # get minimum value of frame_time for next layer
    def earliest_next_frame(self, port):
        """Looks at next unassigned frame across the ports to determine
        the earliest time at which each of them was read"""
        times_of_next_frames = []
        for p in self.ports:
            next_index = self.port_current_frame[p] + 1
            frame_data_key = f"{p}_{next_index}"

            # problem with outpacing the threads reading data in, so wait if need be
            while frame_data_key not in self.all_frame_packets.keys():
                logger.debug(
                    f"Waiting in a loop for frame data to populate with key: {frame_data_key}"
                )
                time.sleep(0.01)

            # this is off by one...don't understand why
            next_frame_time = self.all_frame_packets[frame_data_key].frame_time

            if next_frame_time == -1:
                logger.info(
                    f"End of frames at port {p} detected; ending synchronization"
                )
                self.frames_complete = True
                self.stop_event.set()

            if p != port:
                times_of_next_frames.append(next_frame_time)

        return min(times_of_next_frames)

    def latest_current_frame(self, port):
        """Provides the latest frame_time of the current frames not inclusive of the provided port"""
        times_of_current_frames = []
        for p in self.ports:
            current_index = self.port_current_frame[p]
            frame_data_key = f"{p}_{current_index}"
            current_frame_time = self.all_frame_packets[frame_data_key].frame_time
            if p != port:
                times_of_current_frames.append(current_frame_time)

        return max(times_of_current_frames)

    def frame_slack(self):
        """Determine how many unassigned frames are sitting in self.dataframe"""
        slack = [
            self.port_frame_count[port] - self.port_current_frame[port]
            for port in self.ports
        ]
        logger.debug(f"Slack in frames is {slack}")
        return min(slack)

    def average_fps(self):
        if len(self.mean_frame_times) > 10:  # only looking at the most recent layers
            self.mean_frame_times = self.mean_frame_times[-10:]

        delta_t = np.diff(self.mean_frame_times)
        mean_delta_t = np.mean(delta_t)

        return 1 / mean_delta_t

    def synch_frames_worker(self):

        logger.info(f"Waiting for all ports to begin harvesting corners...")

        sync_time = time.perf_counter()

        sync_index = 0

        logger.info("About to start synchronizing frames...")
        while not self.stop_event.is_set():

            current_frame_packets = {}
            
            layer_frame_times = []

            # build earliest next/latest current dictionaries for each port to determine where to put frames
            # must be done before going in and making any updates to the frame index
            earliest_next = {}
            latest_current = {}

            for port in self.ports:
                earliest_next[port] = self.earliest_next_frame(port)
                latest_current[port] = self.latest_current_frame(port)
                current_frame_index = self.port_current_frame[port]

            for port in self.ports:
                current_frame_index = self.port_current_frame[port]

                port_index_key = f"{port}_{current_frame_index}"
                current_frame_packet = self.all_frame_packets[port_index_key]
                frame_time = current_frame_packet.frame_time

                # don't put a frame in a synched frame packet if the next packet has a frame before it
                if frame_time > earliest_next[port]:
                    # definitly should be put in the next layer and not this one
                    current_frame_packets[port] = None
                    logger.warn(f"Skipped frame at port {port}: > earliest_next")
                elif (
                    earliest_next[port] - frame_time < frame_time - latest_current[port]
                ):  # frame time is closer to earliest next than latest current
                    # if it's closer to the earliest next frame than the latest current frame, bump it up
                    # only applying for 2 camera setup where I noticed this was an issue (frames stay out of synch)
                    current_frame_packets[port] = None
                    logger.warn(
                        f"Skipped frame at port {port}: delta < time-latest_current"
                    )
                else:
                    # add the data and increment the index
                    current_frame_packets[port] = self.all_frame_packets.pop(port_index_key)
                    # frame_packets[port]["sync_index"] = sync_index
                    self.port_current_frame[port] += 1
                    layer_frame_times.append(frame_time)
                    logger.debug(
                        f"Adding to layer from port {port} at index {current_frame_index} and frame time: {frame_time}"
                    )

            logger.debug(f"Unassigned Frames: {len(self.all_frame_packets)}")

            self.mean_frame_times.append(np.mean(layer_frame_times))

            self.current_sync_packet = SyncPacket(sync_index, current_frame_packets)
            sync_index += 1

            if self.stop_event.is_set():
                logger.info("Sending `None` on queue to signal end of synced frames.")
                self.current_sync_packet = None

            # notify other processes that the new frames are ready for processing
            # only for tasks that can risk missing frames (i.e. only for gui purposes)
            for q in self.sync_notice_subscribers:
                logger.debug(f"Giving notice of new synched frames packet via {q}")
                q.put("new synched frames available")

            for q in self.synched_frames_subscribers:
                logger.debug(f"Placing new synched frames packet on queue: {q}")
                q.put(self.current_sync_packet)

            self.fps = self.average_fps()

        logger.info("Frame synch worker successfully ended")


if __name__ == "__main__":
    from calicam.calibration.charuco import Charuco
    from calicam.calibration.corner_tracker import CornerTracker
    from calicam.recording.recorded_stream import RecordedStream, RecordedStreamPool
    
    # DON"T DEAL WITH THE SESSION OBJECT IN TESTS...ONLY MORE FOUNDATIONAL ELEMENTS
    from calicam.cameras.camera import Camera
    from calicam.cameras.live_stream import LiveStream
    from calicam.session import Session
    import pandas as pd
    import time
    
    repo = Path(str(Path(__file__)).split("calicam")[0], "calicam")
    recording_directory = Path(repo, "sessions", "5_cameras", "recording")

    ports = [0, 1, 2, 3, 4]
    # ports = [0,1]
    charuco = Charuco(
        4, 5, 11, 8.5, aruco_scale=0.75, square_size_overide_cm=5.25, inverted=True
    )

    trackr = CornerTracker(charuco)
    # recorded_stream_pool = RecordedStreamPool(ports, recording_directory, tracker=trackr)
    recorded_stream_pool = RecordedStreamPool(ports, recording_directory, tracker=None)
    logger.info("Creating Synchronizer")
    syncr = Synchronizer(recorded_stream_pool.streams, fps_target=None)
    recorded_stream_pool.play_videos()

    notification_q = Queue()

    syncr.subscribe_to_notice(notification_q)
    logger.info(f"Beginning playback at {time.perf_counter()}")
    while not syncr.stop_event.is_set():
        synched_frames_notice = notification_q.get()
        sync_packet = syncr.current_sync_packet
        for port, frame_packet in sync_packet.frame_packets.items():
            
            if frame_packet:
                cv2.imshow(f"Port {port}", frame_packet.frame)
                # print(frame_packet.points)
                
        key = cv2.waitKey(1)

        if key == ord("q"):
            cv2.destroyAllWindows()
            break


    logger.info(f"Playback finished at {time.perf_counter()}")