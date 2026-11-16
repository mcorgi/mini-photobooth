"""Hardware-independent state, GPIO debounce, output, and UI-loop checks."""

import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame
from PIL import Image, ImageStat

from photobooth import DebouncedButton, PhotoMode, Session, State, compose_output, save_output
import photobooth


class ButtonTests(unittest.TestCase):
    def test_bounce_hold_release_and_second_press(self):
        button = DebouncedButton(False, 0)
        samples = [(True, .10, True), (False, .11, False), (True, .12, True),
                   (True, .18, False), (True, 1., False), (True, 2., True),
                   (False, 2.1, False), (True, 2.12, True),
                   (False, 2.13, False), (False, 2.20, False),
                   (True, 2.3, True), (True, 2.36, False)]
        accepted = [now for pressed, now, edge in samples
                    if button.update(pressed, now, edge)]
        self.assertEqual(accepted, [.18, 2.36])

    def test_busy_press_held_into_idle_is_discarded(self):
        button = DebouncedButton(False, 0)
        self.assertFalse(button.update(True, .1, True, eligible=False))
        self.assertFalse(button.update(True, .2, False, eligible=True))
        self.assertFalse(button.update(True, 2., False, eligible=True))
        button.update(False, 2.1, False)
        button.update(False, 2.2, False)
        button.update(True, 2.3, True)
        self.assertTrue(button.update(True, 2.4, False))

    def test_initially_held_button_and_level_without_edge(self):
        button = DebouncedButton(True, 0)
        self.assertFalse(button.update(True, 1., True))
        button.update(False, 2., False)
        button.update(False, 2.1, False)
        self.assertFalse(button.update(True, 3., False))
        self.assertFalse(button.update(True, 3.1, False))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)

    def test_default_toggle_persistence_and_busy_lock(self):
        session = Session(0)
        self.assertEqual(session.selected_mode, PhotoMode.SINGLE_PHOTO)
        for expected in [PhotoMode.PHOTO_STRIP, PhotoMode.PHOTO_GRID,
                         PhotoMode.SINGLE_PHOTO, PhotoMode.PHOTO_STRIP]:
            session.advance(1, mode=True)
            self.assertEqual(session.selected_mode, expected)
        session.advance(2)
        self.assertEqual(session.selected_mode, PhotoMode.PHOTO_STRIP)
        for state in State:
            if state != State.IDLE:
                session.enter(state, 3)
                session.advance(3, mode=True)
                self.assertEqual(session.selected_mode, PhotoMode.PHOTO_STRIP)

    def run_capture(self, session, start):
        session.advance(start, shutter=True)
        for index in range(session.selected_mode.count):
            tick = session.state_started
            self.assertEqual(session.state, State.COUNTDOWN)
            session.advance(tick + 2.99, shutter=True, mode=True)
            self.assertEqual(session.state, State.COUNTDOWN)
            session.advance(tick + 3.001)
            self.assertEqual(session.state, State.CAPTURING)
            session.advance(tick + 4.)  # Wait for a real photo; time alone cannot capture.
            self.assertEqual(session.state, State.CAPTURING)
            session.photo = index
            session.advance(tick + 4.1, shutter=True, mode=True)
        self.assertEqual(session.photos, list(range(session.selected_mode.count)))
        self.assertEqual(session.state, State.PROCESSING)
        tick = session.state_started
        session.advance(tick + 2.)
        self.assertEqual(session.state, State.PROCESSING)
        session.output_path = Path("saved.png")
        session.advance(tick + 2.1)
        self.assertEqual(session.state, State.REVIEW)

    def test_single_strip_then_single_and_retake(self):
        session = Session(0)
        self.run_capture(session, 1.)
        # Retake wins if continue is also pressed.
        session.advance(10., shutter=True, retake=True)
        self.assertEqual(session.state, State.COUNTDOWN)
        self.assertEqual(session.photos, [])
        session.fail(RuntimeError("test failure"), 11.)
        session.advance(12., mode=True)
        self.run_capture(session, 13.)
        session.advance(40., shutter=True)
        self.assertEqual(session.state, State.PRINTING)
        session.advance(41., mode=True, shutter=True)
        self.assertEqual(session.state, State.PRINTING)
        session.advance(42., mode=True)
        self.assertEqual(session.state, State.IDLE)
        self.assertEqual(session.selected_mode, PhotoMode.PHOTO_STRIP)
        session.advance(43., mode=True)
        self.assertEqual(session.selected_mode, PhotoMode.PHOTO_GRID)
        self.run_capture(session, 44.)
        session.advance(70., shutter=True)
        session.advance(72.)
        session.advance(73., mode=True)
        self.assertEqual(session.selected_mode, PhotoMode.SINGLE_PHOTO)
        self.run_capture(session, 74.)

    def test_failure_clears_partial_capture_and_allows_retry(self):
        session = Session(0)
        session.advance(1, mode=True, shutter=True)
        session.photos = [object()]
        session.fail(OSError("camera failed"), 5)
        self.assertEqual(session.state, State.IDLE)
        self.assertEqual(session.photos, [])
        self.assertIsNone(session.photo)
        self.run_capture(session, 6.)
        self.assertIsNone(session.error)

    def test_stop_returns_idle_clears_photos_and_preserves_mode(self):
        for state in (State.COUNTDOWN, State.CAPTURING, State.PROCESSING, State.PRINTING):
            with self.subTest(state=state):
                session = Session(0)
                session.selected_mode = PhotoMode.PHOTO_STRIP
                session.enter(state, 1)
                session.photos = [object()]
                session.photo = object()
                session.advance(2, retake=True, shutter=True, mode=True)
                self.assertEqual(session.state, State.IDLE)
                self.assertEqual(session.photos, [])
                self.assertIsNone(session.photo)
                self.assertIsNone(session.output_path)
                self.assertEqual(session.selected_mode, PhotoMode.PHOTO_STRIP)
                self.run_capture(session, 3)


class OutputTests(unittest.TestCase):
    def test_grid_dimensions_order_gaps_and_saved_output(self):
        photos = [Image.new("RGB", (320, 240), (gray, gray, gray))
                  for gray in (0, 85, 170, 255)]
        grid = photobooth.compose_image(photos, PhotoMode.PHOTO_GRID)
        self.assertEqual(grid.size, (384, 294))
        for point, gray in zip(((98, 75), (286, 75), (98, 218), (286, 218)),
                               (0, 85, 170, 255)):
            self.assertEqual(grid.getpixel(point), gray)
        for box in ((0, 0, 384, 8), (188, 0, 196, 294), (0, 143, 384, 151),
                    (0, 286, 384, 294)):
            self.assertEqual(grid.crop(box).getextrema(), (255, 255))
        with tempfile.TemporaryDirectory() as folder:
            path = save_output(photos, PhotoMode.PHOTO_GRID, folder)
            self.assertTrue(path.name.endswith("-grid.png"))
            with Image.open(path) as output:
                self.assertEqual(output.size, (384, 294))
                self.assertEqual(output.mode, "1")
        preview = photobooth.make_review_preview(photos, PhotoMode.PHOTO_GRID)
        self.assertLessEqual(preview.get_width(), 256)
        self.assertEqual(preview.get_height(), 192)

    def test_review_is_continuous_tone_and_off_preserves_color(self):
        photo = Image.new("RGB", (320, 240), (180, 100, 60))
        preview = photobooth.make_review_preview([photo] * 4, PhotoMode.PHOTO_STRIP)
        self.assertEqual(preview.get_size(), (64, 192))
        color = preview.get_at((32, 24))
        self.assertGreater(color.r, color.g)
        self.assertGreater(color.g, color.b)
        preview = photobooth.make_review_preview([photo] * 4, PhotoMode.PHOTO_STRIP, "High Contrast")
        color = preview.get_at((32, 24))
        self.assertEqual(color.r, color.g)
        self.assertEqual(color.g, color.b)
        self.assertGreater(color.r, 0)
        self.assertLess(color.r, 255)
        self.assertEqual(compose_output([photo] * 4, PhotoMode.PHOTO_STRIP).mode, "1")

    def test_filters_preserve_source_and_compose_printable_strip(self):
        surface = pygame.Surface((64, 48))
        for x in range(64):
            pygame.draw.line(surface, (x * 4, x * 3, x * 2), (x, 0), (x, 47))
        original = pygame.image.tostring(surface, "RGB")
        photo = Image.frombytes("RGB", surface.get_size(), original)
        for name in (*photobooth.FILTER_NAMES, None):
            with self.subTest(filter=name):
                preview, printable = photobooth.apply_filter(surface, name)
                self.assertEqual(preview.get_size(), surface.get_size())
                self.assertEqual(printable.mode, "1")
                strip = compose_output([photo] * 4, PhotoMode.PHOTO_STRIP, name)
                self.assertEqual(strip.size, (384, 1144))
                self.assertEqual(strip.mode, "1")
        self.assertEqual(pygame.image.tostring(surface, "RGB"), original)
        self.assertNotEqual(compose_output([photo], PhotoMode.SINGLE_PHOTO).tobytes(),
                            compose_output([photo], PhotoMode.SINGLE_PHOTO, "Invert").tobytes())

    def test_strip_size_order_margins_and_monochrome(self):
        photos = [Image.new("RGB", size, (gray, gray, gray))
                  for size, gray in zip([(500, 200), (320, 240), (200, 500), (800, 600)],
                                        [0, 85, 170, 255])]
        output = compose_output(photos, PhotoMode.PHOTO_STRIP)
        self.assertEqual(output.size, (384, 1144))
        self.assertEqual(output.mode, "1")
        for index, expected in enumerate([0, 85, 170, 255]):
            top = 8 + index * 284
            region = output.crop((20, top + 10, 364, top + 266)).convert("L")
            self.assertAlmostEqual(ImageStat.Stat(region).mean[0], expected, delta=1.)
        for box in [(0, 0, 384, 8), (0, 0, 8, 1144), (376, 0, 384, 1144),
                    (0, 1136, 384, 1144), (0, 284, 384, 292)]:
            self.assertEqual(output.crop(box).getextrema(), (255, 255))

    def test_single_save_and_wrong_count(self):
        photo = Image.new("RGB", (320, 240), "red")
        with tempfile.TemporaryDirectory() as folder:
            path = save_output([photo], PhotoMode.SINGLE_PHOTO, folder)
            with Image.open(path) as image:
                self.assertEqual(image.size, (384, 288))
                self.assertEqual(image.mode, "1")
            self.assertEqual(len(list(Path(folder).glob("*.png"))), 1)
        with self.assertRaises(ValueError):
            compose_output([photo], PhotoMode.PHOTO_STRIP)


class LoopTests(unittest.TestCase):
    def test_async_camera_capture_counts_and_retry_after_driver_error(self):
        for mode, fail_once, stall, cancel in [(PhotoMode.SINGLE_PHOTO, False, False, False),
                                              (PhotoMode.PHOTO_STRIP, False, False, False),
                                              (PhotoMode.PHOTO_GRID, False, False, False),
                                              (PhotoMode.PHOTO_STRIP, True, False, False),
                                              (PhotoMode.SINGLE_PHOTO, False, True, False),
                                              (PhotoMode.PHOTO_STRIP, False, False, True)]:
            with self.subTest(mode=mode, fail_once=fail_once, stall=stall, cancel=cancel):
                now, sessions, requests = [0.], [], []
                started, completed = [False], [False]
                failed = [False]
                cancelled = [False]

                def create_session(tick):
                    session = Session(tick)
                    sessions.append(session)
                    return session

                class Job:
                    def __init__(self, photo, size):
                        self.photo, self.polled = photo, False
                        self.size = size

                    def get_result(self, timeout):
                        self_test.assertEqual(timeout, 0)
                        if self.photo and stall:
                            raise TimeoutError()
                        if not self.polled:
                            self.polled = True
                            raise TimeoutError()
                        if self.photo and fail_once and not failed[0]:
                            failed[0] = True
                            raise OSError("injected camera error")
                        return SimpleNamespace(tobytes=lambda: bytes([85, 170, 255])
                                               * (self.size[0] * self.size[1]))

                class Camera:
                    def create_preview_configuration(self, **kwargs):
                        return kwargs

                    def configure(self, config):
                        self.size = config["main"]["size"]
                        self_test.assertEqual(self.size, (640, 480))

                    def start(self, **kwargs):
                        pass

                    def capture_array(self, stream, wait):
                        self_test.assertFalse(wait)
                        photo = sessions[0].state == State.CAPTURING
                        if photo:
                            requests.append(now[0])
                        return Job(photo, self.size)

                    def close(self):
                        pass

                def press(key):
                    return [pygame.event.Event(kind, {"key": key})
                            for kind in (pygame.KEYDOWN, pygame.KEYUP)]

                def events():
                    session = sessions[0]
                    if now[0] > 60:
                        raise AssertionError("camera session failed to complete")
                    if session.state == State.IDLE:
                        if completed[0]:
                            return [pygame.event.Event(pygame.QUIT)]
                        if not started[0]:
                            if session.selected_mode != mode:
                                return press(pygame.K_m)
                            started[0] = True
                            return press(pygame.K_SPACE)
                        if session.error:
                            if stall:
                                return [pygame.event.Event(pygame.QUIT)]
                            return press(pygame.K_SPACE)
                    if session.state == State.REVIEW:
                        completed[0] = True
                        return press(pygame.K_SPACE)
                    if cancel and not cancelled[0] and session.state == State.CAPTURING and requests:
                        cancelled[0] = True
                        started[0] = False
                        return press(pygame.K_r)
                    return press(pygame.K_m) if session.state != State.IDLE else []

                def tick(fps):
                    now[0] = round(now[0] + .1, 2)

                self_test = self
                modules = {"picamera2": SimpleNamespace(Picamera2=Camera),
                           "libcamera": SimpleNamespace(controls=SimpleNamespace(
                               AfModeEnum=SimpleNamespace(Continuous=1)))}
                with tempfile.TemporaryDirectory() as folder, \
                        contextlib.redirect_stdout(io.StringIO()), \
                        patch.dict(sys.modules, modules), \
                        patch.object(sys, "argv", ["photobooth.py", "--desktop", "--camera",
                                                   "--camera-size", "640x480", "--live-size", "320x240",
                                                   "--output-dir", folder]), \
                        patch.object(photobooth, "Session", side_effect=create_session), \
                        patch.object(photobooth.time, "monotonic", side_effect=lambda: now[0]), \
                        patch.object(pygame.time, "Clock") as clock, \
                        patch.object(pygame.event, "get", side_effect=events):
                    clock.return_value.tick.side_effect = tick
                    photobooth.main()
                    self.assertEqual(len(requests), mode.count + int(fail_once) + int(cancel))
                    self.assertEqual(len(list(Path(folder).glob("*.png"))), 0 if stall else 1)
                    self.assertEqual(sessions[0].selected_mode, mode)
                    self.assertEqual(sessions[0].state, State.IDLE)

    def test_desktop_event_loop_strip_hold_busy_presses_and_single(self):
        now = [0.]
        session_ref = []
        screenshots = {}
        steps = [
            (.1, pygame.KEYDOWN, pygame.K_m),
            (.2, pygame.KEYDOWN, pygame.K_m),  # Keyboard repeat while held.
            (.3, pygame.KEYUP, pygame.K_m),
            (.31, pygame.KEYDOWN, pygame.K_f),
            (.32, pygame.KEYUP, pygame.K_f),
            (.4, pygame.KEYDOWN, pygame.K_SPACE),
            (.5, pygame.KEYUP, pygame.K_SPACE),
            (1., pygame.KEYDOWN, pygame.K_m),  # Busy press held through return to IDLE.
            (2., pygame.KEYDOWN, pygame.K_SPACE),
            (2.1, pygame.KEYUP, pygame.K_SPACE),
            (18., pygame.KEYDOWN, pygame.K_f),  # Change reviewed strip, reusing its file.
            (18.1, pygame.KEYUP, pygame.K_f),
            (19., pygame.KEYDOWN, pygame.K_SPACE),  # Continue from strip review.
            (19.1, pygame.KEYUP, pygame.K_SPACE),
            (22., pygame.KEYDOWN, pygame.K_m),  # Still held: ignored.
            (22.1, pygame.KEYUP, pygame.K_m),
            (22.2, pygame.KEYDOWN, pygame.K_m),
            (22.3, pygame.KEYUP, pygame.K_m),
            (22.31, pygame.KEYDOWN, pygame.K_m),  # Grid -> Single Photo.
            (22.32, pygame.KEYUP, pygame.K_m),
            (22.4, pygame.KEYDOWN, pygame.K_SPACE),
            (22.5, pygame.KEYUP, pygame.K_SPACE),
            (29., pygame.QUIT, None),
        ]

        def create_session(tick):
            session = Session(tick)
            session_ref.append(session)
            return session

        def events():
            result = []
            while steps and now[0] >= steps[0][0]:
                _, kind, key = steps.pop(0)
                result.append(pygame.event.Event(kind, {} if key is None else {"key": key}))
            return result

        def tick(fps):
            self.assertEqual(fps, 30)
            if session_ref:
                session = session_ref[0]
                if session.state == State.IDLE:
                    screenshots[session.selected_mode] = pygame.display.get_surface().copy()
            now[0] = round(now[0] + .1, 2)

        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()), \
                patch.object(sys, "argv", ["photobooth.py", "--desktop", "--output-dir", folder]), \
                patch.object(photobooth, "Session", side_effect=create_session), \
                patch.object(photobooth, "save_output", wraps=save_output) as writer, \
                patch.object(photobooth.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(pygame.time, "Clock") as clock, \
                patch.object(pygame.event, "get", side_effect=events):
            clock.return_value.tick.side_effect = tick
            photobooth.main()
            paths = list(Path(folder).glob("*.png"))
            self.assertEqual(len(paths), 2)
            self.assertEqual([call.args[3] for call in writer.call_args_list],
                             ["High Contrast", "Outline", "Outline"])
            sizes = []
            for path in paths:
                with Image.open(path) as image:
                    sizes.append(image.size)
            self.assertEqual(sorted(sizes), [(384, 288), (384, 1144)])
        self.assertEqual(session_ref[0].selected_mode, PhotoMode.SINGLE_PHOTO)
        self.assertEqual(session_ref[0].state, State.REVIEW)
        # The mode band changes with the shared mode on the actual rendered surface.
        band = (0, 0, 160, 24)
        self.assertNotEqual(pygame.image.tostring(screenshots[PhotoMode.SINGLE_PHOTO].subsurface(band), "RGB"),
                            pygame.image.tostring(screenshots[PhotoMode.PHOTO_STRIP].subsurface(band), "RGB"))


if __name__ == "__main__":
    unittest.main()
