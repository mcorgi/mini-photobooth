"""PiTFT single-photo/strip capture, PNG output, and simulated printing."""

import argparse
from enum import Enum, auto
import os
from pathlib import Path
import time
from uuid import uuid4

import pygame
from PIL import Image, ImageOps


WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
GRAY = (170, 170, 170)
BUTTON_PINS = [17, 22, 23, 27]
SHUTTER_PIN = 17
RETAKE_PIN = 22
MODE_PIN = 23
QUIT_PIN = 27
DEBOUNCE_SECONDS = 0.05
FPS = 30
CAMERA_TIMEOUT_SECONDS = 10.0


class PhotoMode(Enum):
    SINGLE_PHOTO = "Single Photo"
    PHOTO_STRIP = "Photo Strip (4)"

    @property
    def count(self):
        return 4 if self == PhotoMode.PHOTO_STRIP else 1


class DebouncedButton:
    """Consume GPIO falling edges; require a stable release before rearming."""

    def __init__(self, pressed, now):
        self.raw = self.stable = pressed
        self.changed = now
        self.armed = not pressed
        self.pending = False
        self.allowed = False

    def update(self, pressed, now, falling_edge, eligible=True):
        if pressed != self.raw:
            self.raw = pressed
            self.changed = now
        if falling_edge and self.armed and not self.pending:
            self.pending = True
            self.allowed = eligible
        if now - self.changed < DEBOUNCE_SECONDS:
            return False
        if not self.raw:
            self.stable = False
            self.armed = True
            self.pending = False
        elif not self.stable:
            self.stable = True
            self.armed = False
            accepted = self.pending and self.allowed and eligible
            self.pending = False
            return accepted
        return False


class State(Enum):
    IDLE = auto()
    COUNTDOWN = auto()
    CAPTURING = auto()
    PROCESSING = auto()
    REVIEW = auto()
    PRINTING = auto()


def compose_output(photos, mode):
    """Compose grayscale first; dither the completed print image exactly once."""
    if len(photos) != mode.count:
        raise ValueError("Incorrect photo count for selected mode")
    if mode == PhotoMode.SINGLE_PHOTO:
        photo = photos[0].convert("L")
        image = photo.resize((384, max(1, round(photo.height * 384 / photo.width))))
    else:
        margin, gap = 8, 8
        width, height = 368, 276  # Identical centered 4:3 crops.
        image = Image.new("L", (384, 2 * margin + 4 * height + 3 * gap), 255)
        for index, photo in enumerate(photos):
            crop = ImageOps.fit(photo.convert("L"), (width, height))
            image.paste(crop, (margin, margin + index * (height + gap)))
    return image.convert("1", dither=Image.Dither.FLOYDSTEINBERG)


def save_output(photos, mode, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
                         + ("-strip.png" if mode == PhotoMode.PHOTO_STRIP else "-single.png"))
    compose_output(photos, mode).save(path)
    return path


class Session:
    """The existing timed state machine, with repeated countdown/capture cycles."""

    def __init__(self, now):
        self.state = State.IDLE
        self.state_started = now
        self.selected_mode = PhotoMode.SINGLE_PHOTO
        self.photo = None
        self.photos = []
        self.output_path = None
        self.error = None

    def enter(self, state, now):
        self.state = state
        self.state_started = now
        if state in (State.IDLE, State.COUNTDOWN):
            self.photo = None
        print("State:", state.name)

    def fail(self, error, now):
        self.photos.clear()
        self.output_path = None
        self.error = str(error)
        self.enter(State.IDLE, now)
        print("Session failed:", self.error)

    def advance(self, now, shutter=False, retake=False, mode=False):
        elapsed = now - self.state_started
        if self.state == State.IDLE:
            if mode:
                self.selected_mode = (PhotoMode.PHOTO_STRIP
                                      if self.selected_mode == PhotoMode.SINGLE_PHOTO
                                      else PhotoMode.SINGLE_PHOTO)
            if shutter:
                self.photos.clear()
                self.output_path = None
                self.error = None
                self.enter(State.COUNTDOWN, now)
        elif self.state == State.COUNTDOWN and elapsed >= 3.0:
            self.enter(State.CAPTURING, now)
        elif self.state == State.CAPTURING:
            if elapsed >= 1.0 and self.photo is not None:
                self.photos.append(self.photo)
                self.enter(State.COUNTDOWN if len(self.photos) < self.selected_mode.count
                           else State.PROCESSING, now)
        elif self.state == State.PROCESSING:
            if elapsed >= 1.0 and self.output_path is not None:
                self.enter(State.REVIEW, now)
        elif self.state == State.REVIEW:
            if retake:
                self.photos.clear()
                self.output_path = None
                self.enter(State.COUNTDOWN, now)
            elif shutter:
                self.enter(State.PRINTING, now)
        elif self.state == State.PRINTING and elapsed >= 2.0:
            self.enter(State.IDLE, now)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--desktop", action="store_true",
        help="use a desktop window and keyboard instead of PiTFT/GPIO",
    )
    parser.add_argument(
        "--camera", action="store_true",
        help="use the Pi camera for live preview and an in-memory photo",
    )
    parser.add_argument("--output-dir", default=str(Path(__file__).parent / "output"),
                        help="directory for completed monochrome PNGs")
    args = parser.parse_args()

    GPIO = None
    pitft = None
    camera = None

    try:
        if not args.desktop:
            # Match control_two_collide.py; preserve inherited video/FB settings.
            os.environ["SDL_MOUSEDRV"] = "dummy"
            os.environ["SDL_MOUSEDEV"] = "/dev/null"
            os.environ["DISPLAY"] = ""

            import RPi.GPIO as GPIO
            import pigame

            GPIO.setmode(GPIO.BCM)
            for pin in BUTTON_PINS:
                GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        pygame.init()
        if not args.desktop:
            pitft = pigame.PiTft()
            # Poll edge flags on the UI thread; no callbacks mutate app state.
            for pin in (SHUTTER_PIN, RETAKE_PIN, MODE_PIN):
                GPIO.add_event_detect(pin, GPIO.FALLING, bouncetime=50)

        screen = pygame.display.set_mode((320, 240))
        pygame.display.set_caption("Mini Photobooth")
        pygame.mouse.set_visible(False)
        font_title = pygame.font.Font(None, 30)
        font_message = pygame.font.Font(None, 30)
        font_small = pygame.font.Font(None, 21)
        font_countdown = pygame.font.Font(None, 88)
        clock = pygame.time.Clock()

        if args.camera:
            from picamera2 import Picamera2
            from libcamera import controls

            camera = Picamera2()
            config = camera.create_preview_configuration(
                # Picamera2 BGR888 produces RGB byte order for Pygame.
                main={"size": (320, 240), "format": "BGR888"},
                controls={"AfMode": controls.AfModeEnum.Continuous},
                queue=False,
            )
            camera.configure(config)
            camera.start(show_preview=False)

        live_frame = None
        review_photo = None
        frame_job = None
        frame_job_started = None
        job_is_photo = False
        camera_failed = False
        session = Session(time.monotonic())
        action_pins = (SHUTTER_PIN, RETAKE_PIN, MODE_PIN)
        buttons = {
            pin: DebouncedButton(False if GPIO is None else GPIO.input(pin) == GPIO.LOW,
                                 session.state_started)
            for pin in action_pins
        }
        keys_down = set()
        running = True

        while running:
            if pitft is not None:
                pitft.update()

            now = time.monotonic()
            shutter_pressed = False
            retake_pressed = False
            mode_pressed = False
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        running = False
                    elif event.key not in keys_down:
                        keys_down.add(event.key)
                        if event.key == pygame.K_SPACE:
                            shutter_pressed = True
                        elif event.key == pygame.K_r:
                            retake_pressed = True
                        elif event.key == pygame.K_m:
                            mode_pressed = True
                elif event.type == pygame.KEYUP:
                    keys_down.discard(event.key)

            if GPIO is not None:
                if GPIO.input(QUIT_PIN) == GPIO.LOW:
                    running = False

                presses = {}
                for pin in action_pins:
                    eligible = (session.state == State.IDLE if pin == MODE_PIN
                                else session.state in (State.IDLE, State.REVIEW))
                    presses[pin] = buttons[pin].update(
                        GPIO.input(pin) == GPIO.LOW, now,
                        GPIO.event_detected(pin), eligible,
                    )
                shutter_pressed |= presses[SHUTTER_PIN]
                retake_pressed |= presses[RETAKE_PIN]
                mode_pressed |= presses[MODE_PIN]

            if not running:
                break

            state = session.state
            camera_timed_out = False
            try:
                # Poll one camera job without waiting, so quit/buttons still work.
                if frame_job is not None:
                    try:
                        array = frame_job.get_result(timeout=0)
                    except TimeoutError:
                        if now - frame_job_started >= CAMERA_TIMEOUT_SECONDS:
                            camera_timed_out = True
                            raise RuntimeError("Camera timed out; restart with --camera")
                    else:
                        frame = pygame.image.frombuffer(
                            array.tobytes(), (320, 240), "RGB"
                        ).copy()
                        if job_is_photo:
                            session.photo = frame
                        else:
                            live_frame = frame
                        frame_job = None

                needs_photo = state == State.CAPTURING and session.photo is None
                if camera is not None and frame_job is None and (
                    (state == State.IDLE and session.error is None)
                    or state == State.COUNTDOWN or needs_photo
                ):
                    job_is_photo = needs_photo
                    frame_job = camera.capture_array("main", wait=False)
                    frame_job_started = now

                if camera is None and needs_photo:
                    session.photo = pygame.Surface((320, 240))
                    session.photo.fill(GRAY)
                    label = font_title.render("Sample photo " + str(len(session.photos) + 1),
                                              True, BLACK)
                    session.photo.blit(label, (60, 100))
                if state == State.PROCESSING and session.output_path is None:
                    photos = [Image.frombytes("RGB", photo.get_size(),
                              pygame.image.tostring(photo, "RGB")) for photo in session.photos]
                    session.output_path = save_output(photos, session.selected_mode, args.output_dir)
                    review_photo = (session.photo if session.selected_mode == PhotoMode.SINGLE_PHOTO
                                    else pygame.image.load(str(session.output_path)))
                    print("Saved:", session.output_path)
            except Exception as error:
                # A failed session must never reach REVIEW/PRINTING with stale photos.
                if camera_timed_out:
                    # Do not submit another request over a stalled camera job.
                    try:
                        camera.close()
                    except Exception:
                        pass
                    camera = None
                    args.camera = False
                    live_frame = None
                    camera_failed = True
                frame_job = None
                session.fail(error, now)
                shutter_pressed = retake_pressed = mode_pressed = False

            # Consume every press now, including presses in busy states.
            session.advance(now, shutter_pressed and not camera_failed, retake_pressed, mode_pressed)
            state = session.state

            elapsed = now - session.state_started
            if state == State.IDLE:
                message = "Camera unavailable" if camera_failed else "Ready!"
                detail = ("Restart with --camera" if camera_failed
                          else "GPIO17/SPACE: Start  GPIO23/M: Mode")
            elif state == State.COUNTDOWN:
                message = str(max(1, 3 - int(elapsed)))
                detail = "Photo {} of {} - Smile!".format(
                    len(session.photos) + 1, session.selected_mode.count)
            elif state == State.REVIEW:
                message = "Retake or continue?"
                detail = "GPIO17 / SPACE: Continue"
            else:
                message = {
                    State.CAPTURING: "Capturing...",
                    State.PROCESSING: "Processing...",
                    State.PRINTING: "Printing...",
                }[state]
                detail = ("Photo {} of {}".format(len(session.photos) + 1, session.selected_mode.count)
                          if state == State.CAPTURING else
                          "Print simulation" if state == State.PRINTING else "Please wait")

            screen.fill(BLACK)
            if camera is not None:
                image = (live_frame if state in (State.IDLE, State.COUNTDOWN)
                         else review_photo if state in (State.REVIEW, State.PRINTING)
                         else session.photo)
                if image is not None:
                    rect = image.get_rect().fit(pygame.Rect(64, 48, 192, 144))
                    preview = pygame.transform.scale(image, rect.size)
                    screen.blit(preview, rect)
            title = font_title.render("MINI PHOTOBOOTH", True, WHITE)
            screen.blit(title, title.get_rect(center=(160, 18 if args.camera else 28)))
            if not args.camera:
                pygame.draw.line(screen, GRAY, (20, 50), (300, 50))
            mode_text = font_small.render("Mode: " + session.selected_mode.value, True, WHITE)
            # A fixed band remains legible over the live camera preview.
            pygame.draw.rect(screen, BLACK, (0, 54, 320, 20))
            screen.blit(mode_text, mode_text.get_rect(center=(160, 64)))
            font = font_countdown if state == State.COUNTDOWN else font_message
            if args.camera and state == State.REVIEW:
                text = font_small.render(message, True, WHITE)
                screen.blit(text, text.get_rect(center=(160, 37)))
            elif not args.camera or state != State.IDLE:
                text = font.render(message, True, WHITE)
                text_rect = text.get_rect(center=(160, 110))
                pygame.draw.rect(screen, BLACK, text_rect.inflate(8, 4))
                screen.blit(text, text_rect)
            text = font_small.render(detail, True, WHITE)
            screen.blit(text, text.get_rect(center=(160, 201 if args.camera else 170)))
            if state == State.IDLE and session.error:
                error_hint = ("Camera timed out - restart application" if camera_failed
                              else "Session failed - see terminal; retry")
                text = font_small.render(error_hint, True, WHITE)
                screen.blit(text, text.get_rect(center=(160, 218 if args.camera else 191)))
            if state == State.REVIEW:
                text = font_small.render("GPIO22 / R: Retake", True, WHITE)
                screen.blit(text, text.get_rect(center=(160, 218 if args.camera else 191)))
            footer = "ESC / Q: quit" if args.desktop else "GPIO27 / ESC / Q: quit"
            text = font_small.render(footer, True, GRAY)
            screen.blit(text, text.get_rect(center=(160, 232 if args.camera else 219)))
            pygame.display.flip()
            clock.tick(FPS)

    except KeyboardInterrupt:
        pass
    finally:
        try:
            if camera is not None:
                camera.close()
        finally:
            # The lab's pigame destructor stops its touchscreen helper.
            try:
                if pitft is not None:
                    del pitft
            finally:
                try:
                    if GPIO is not None:
                        GPIO.cleanup()
                finally:
                    pygame.quit()


if __name__ == "__main__":
    main()
