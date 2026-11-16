"""PiTFT single-photo, strip, and grid capture with simulated printing."""

import argparse
from enum import Enum, auto
import os
from pathlib import Path
import time
from uuid import uuid4

import numpy as np
import pygame
from PIL import Image, ImageEnhance, ImageFilter, ImageOps


WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
GRAY = (170, 170, 170)
SHUTTER_PIN = 26
FILTER_PIN = 17
RETAKE_PIN = 22
MODE_PIN = 23
QUIT_PIN = 27
BUTTON_PINS = [SHUTTER_PIN, FILTER_PIN, RETAKE_PIN, MODE_PIN, QUIT_PIN]
FILTER_NAMES = ("High Contrast", "Outline", "Invert")
FILTER_STEPS = len(FILTER_NAMES) + 1  # Final step is Off.
DEBOUNCE_SECONDS = 0.05
FPS = 30
CAMERA_TIMEOUT_SECONDS = 10.0


class PhotoMode(Enum):
    SINGLE_PHOTO = "Single Photo"
    PHOTO_STRIP = "Photo Strip (4)"
    PHOTO_GRID = "Photo Grid (4)"

    @property
    def count(self):
        return 1 if self == PhotoMode.SINGLE_PHOTO else 4


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


# Keep the 192-pixel-tall preview clear of the compact header and footer.
PREVIEW_SIZE = (256, 192)
PREVIEW_POS = (32, 24)
FILTER_TOUCH = (160, 0, 160, 24)

# Default resolutions (recommended for Pi 4 / Pi 5):
#   --camera-size 1280x960  --live-size 480x360
# For Pi Zero 2 W, downgrade if frames are dropped:
#   --camera-size 640x480   --live-size 320x240
DEFAULT_CAMERA_SIZE = "1280x960"
DEFAULT_LIVE_SIZE = "480x360"

# Outline filter tuning parameters.
OUTLINE_MEDIAN_SIZE = 3        # Denoise window before sketch (3 is usually enough)
OUTLINE_BLUR_SCALE = 0.012     # Blur radius = min(w, h) * this (0.012 => ~4px @360p)
OUTLINE_BLUR_MIN = 3           # Minimum blur radius
OUTLINE_DITHER = True          # True: soft pencil look via dithering
                               # False: hard binary lines via threshold
OUTLINE_THRESHOLD = 128        # Used only when OUTLINE_DITHER is False


def parse_size(text):
    """Parse a WxH string into a (width, height) tuple."""
    w, h = text.lower().split("x")
    size = (int(w), int(h))
    if min(size) <= 0:
        raise ValueError("Image dimensions must be positive")
    return size


def current_filter_name(filter_index):
    """Return the filter name, or None when filters are switched off."""
    if filter_index < len(FILTER_NAMES):
        return FILTER_NAMES[filter_index]
    return None


def filter_label(filter_index):
    """Human-readable filter label, including the Off step."""
    if filter_index < len(FILTER_NAMES):
        return FILTER_NAMES[filter_index]
    return "Off"


def outline_sketch(gray):
    """Pencil-sketch effect using the classic dodge blend.

    Algorithm:
      1. Optional median filter to remove salt-and-pepper noise.
      2. Invert the grayscale image: inv = 255 - gray.
      3. Blur the inverted image with a radius proportional to image size.
      4. Dodge blend: result = gray * 255 / (255 - blurred + 1).
         - Flat regions: blurred ~ inv, so result ~ 255 (white).
         - Edges: blurred < inv, so result drops (dark lines).
    The output is a continuous grayscale sketch, which is what makes it
    look like pencil shading instead of harsh binary dots.
    """
    denoised = gray.filter(ImageFilter.MedianFilter(OUTLINE_MEDIAN_SIZE))

    w, h = denoised.size
    radius = max(OUTLINE_BLUR_MIN, int(min(w, h) * OUTLINE_BLUR_SCALE))

    g = np.asarray(denoised, dtype=np.float32)
    inv = 255.0 - g
    blurred = np.asarray(
        Image.fromarray(inv.astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(radius)
        ),
        dtype=np.float32,
    )
    # +1 avoids divide-by-zero on pure-black pixels; clip keeps values valid.
    sketch = g * 255.0 / (255.0 - blurred + 1.0)
    sketch = np.clip(sketch, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(sketch, mode="L")


def filter_grayscale(source, filter_name):
    """Apply the partner's image effects before the final monochrome conversion."""
    gray = ImageOps.autocontrast(source.convert("L"))
    if filter_name == "High Contrast":
        return ImageEnhance.Contrast(gray).enhance(2.5)
    if filter_name == "Outline":
        return outline_sketch(gray)
    if filter_name == "Invert":
        return ImageOps.invert(gray)
    return gray


def monochrome(image, filter_name):
    if filter_name == "Outline" and not OUTLINE_DITHER:
        return image.point(lambda p: 0 if p < OUTLINE_THRESHOLD else 255, mode="1")
    return image.convert("1", dither=Image.Dither.FLOYDSTEINBERG)


def apply_filter(surface, filter_name):
    """Return the live preview and the corresponding 1-bit print image."""
    source = Image.frombytes("RGB", surface.get_size(), pygame.image.tostring(surface, "RGB"))
    gray = filter_grayscale(source, filter_name)
    result = monochrome(gray, filter_name)
    if filter_name is None:
        return surface.copy(), result
    preview_image = (gray if filter_name == "Outline" else result).convert("RGB")
    preview = pygame.image.fromstring(preview_image.tobytes(), preview_image.size, "RGB").copy()
    return preview, result


class State(Enum):
    IDLE = auto()
    COUNTDOWN = auto()
    CAPTURING = auto()
    PROCESSING = auto()
    REVIEW = auto()
    PRINTING = auto()


def compose_image(photos, mode, filter_name=None, color=False):
    """Compose continuous-tone tiles, retaining color for the Off review preview."""
    if len(photos) != mode.count:
        raise ValueError("Incorrect photo count for selected mode")
    photos = [photo.convert("RGB") if color else filter_grayscale(photo, filter_name)
              for photo in photos]
    if mode == PhotoMode.SINGLE_PHOTO:
        photo = photos[0]
        image = photo.resize((384, max(1, round(photo.height * 384 / photo.width))),
                             Image.Resampling.LANCZOS)
    else:
        margin, gap = 8, 8
        columns = 2 if mode == PhotoMode.PHOTO_GRID else 1
        rows = mode.count // columns
        width = (384 - 2 * margin - (columns - 1) * gap) // columns
        height = width * 3 // 4  # Identical centered 4:3 crops.
        image = Image.new("RGB" if color else "L",
                          (384, 2 * margin + rows * height + (rows - 1) * gap), "white")
        for index, photo in enumerate(photos):
            crop = ImageOps.fit(photo, (width, height), method=Image.Resampling.LANCZOS)
            row, column = divmod(index, columns)
            image.paste(crop, (margin + column * (width + gap), margin + row * (height + gap)))
    return image


def compose_output(photos, mode, filter_name=None):
    """Dither the completed printer image once, after resizing and composition."""
    return monochrome(compose_image(photos, mode, filter_name), filter_name)


def make_review_preview(photos, mode, filter_name=None):
    # Resizing 1-bit printer dots for the tiny display causes aliasing. Review
    # continuous tones instead; Off preserves color. The saved print PNG stays 1-bit.
    image = compose_image(photos, mode, filter_name, color=filter_name is None)
    image = ImageOps.contain(image, PREVIEW_SIZE, Image.Resampling.LANCZOS).convert("RGB")
    return pygame.image.fromstring(image.tobytes(), image.size, "RGB").copy()


def save_output(photos, mode, output_dir, filter_name=None, path=None):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = {PhotoMode.SINGLE_PHOTO: "single", PhotoMode.PHOTO_STRIP: "strip",
              PhotoMode.PHOTO_GRID: "grid"}[mode]
    path = path or output_dir / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
                                + "-" + suffix + ".png")
    compose_output(photos, mode, filter_name).save(path)
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
        if retake and self.state in (State.COUNTDOWN, State.CAPTURING,
                                     State.PROCESSING, State.PRINTING):
            self.photos.clear()
            self.output_path = None
            self.error = None
            self.enter(State.IDLE, now)
            return
        elapsed = now - self.state_started
        if self.state == State.IDLE:
            if mode:
                modes = tuple(PhotoMode)
                self.selected_mode = modes[(modes.index(self.selected_mode) + 1) % len(modes)]
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


def draw_ui(screen, session, filter_index, image, font_small, font_message,
            font_countdown, now, camera_failed=False, font_button=None):
    """Reserve the header/footer for labels; leave the live image unobstructed."""
    screen.fill(BLACK)
    if image is not None:
        rect = image.get_rect().fit(pygame.Rect(PREVIEW_POS, PREVIEW_SIZE))
        screen.blit(pygame.transform.smoothscale(image, rect.size), rect)

    header = (24, 24, 24)
    pygame.draw.rect(screen, header, (0, 0, 320, 24))
    mode = {PhotoMode.SINGLE_PHOTO: "Single Photo", PhotoMode.PHOTO_STRIP: "Strip (4)",
            PhotoMode.PHOTO_GRID: "Grid (4)"}[session.selected_mode]
    label = font_small.render("Mode: " + mode, True, WHITE)
    screen.blit(label, label.get_rect(midleft=(8, 12)))
    label = font_small.render("Filter: " + filter_label(filter_index), True, GRAY)
    screen.blit(label, label.get_rect(midright=(312, 12)))

    state = session.state
    # Physical buttons run bottom-to-top: GPIO17, GPIO22, GPIO23, GPIO27.
    # These labels occupy the existing 32-pixel gutter, outside the preview.
    active_session = state in (State.COUNTDOWN, State.CAPTURING, State.PROCESSING, State.PRINTING)
    button_labels = (
        ("Quit", 36, True),
        ("Mode", 92, state == State.IDLE),
        ("Stop" if active_session else "Redo", 148, active_session or state == State.REVIEW),
        ("Filter", 204, state in (State.IDLE, State.COUNTDOWN, State.REVIEW)),
    )
    for caption, y, enabled in button_labels:
        color = GRAY if enabled else (70, 70, 70)
        pygame.draw.line(screen, color, (0, y), (2, y))
        text = (font_button or font_small).render(caption, True, color)
        screen.blit(text, text.get_rect(midleft=(4, y)))
    elapsed = now - session.state_started
    message = None
    status = "Ready"
    if state == State.COUNTDOWN:
        message = str(max(1, 3 - int(elapsed)))
        status = "Photo {} of {}".format(len(session.photos) + 1, session.selected_mode.count)
    elif state == State.CAPTURING:
        status = "Capturing {} of {}".format(len(session.photos) + 1, session.selected_mode.count)
    elif state == State.PROCESSING:
        message = "Processing..."
        status = {PhotoMode.SINGLE_PHOTO: "Preparing photo", PhotoMode.PHOTO_STRIP: "Making your photo strip",
                  PhotoMode.PHOTO_GRID: "Making your photo grid"}[session.selected_mode]
    elif state == State.REVIEW:
        status = "Shutter: continue"
    elif state == State.PRINTING:
        message = "Printing..."
        status = "Print simulation"
    elif camera_failed:
        message = "Camera unavailable"
        status = "Restart the application"
    elif session.error:
        message = "Photo failed"
        status = "Press shutter to retry"
    elif image is None:
        message = "Ready!"

    # Only transient countdowns and busy/error messages cover the preview.
    if message is not None:
        font = font_countdown if state == State.COUNTDOWN else font_message
        text = font.render(message, True, WHITE)
        rect = text.get_rect(center=(160, 120))
        pygame.draw.rect(screen, BLACK, rect.inflate(16, 8), border_radius=6)
        screen.blit(text, rect)
    text = font_small.render(status, True, GRAY)
    screen.blit(text, text.get_rect(center=(160, 228)))


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
    parser.add_argument("--camera-size", default=DEFAULT_CAMERA_SIZE,
                        help="capture resolution WxH (default: %(default)s)")
    parser.add_argument("--live-size", default=DEFAULT_LIVE_SIZE,
                        help="live filter resolution WxH (default: %(default)s)")
    args = parser.parse_args()
    try:
        camera_size = parse_size(args.camera_size)
        live_size = parse_size(args.live_size)
    except (ValueError, TypeError):
        parser.error("--camera-size and --live-size must be positive WxH values")

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
            for pin in (SHUTTER_PIN, FILTER_PIN, RETAKE_PIN, MODE_PIN):
                GPIO.add_event_detect(pin, GPIO.FALLING, bouncetime=50)

        screen = pygame.display.set_mode((320, 240))
        pygame.display.set_caption("Mini Photobooth")
        pygame.mouse.set_visible(False)
        font_title = pygame.font.Font(None, 30)
        font_message = pygame.font.Font(None, 30)
        font_small = pygame.font.Font(None, 18)
        font_button = pygame.font.Font(None, 14)
        font_countdown = pygame.font.Font(None, 88)
        clock = pygame.time.Clock()

        if args.camera:
            from picamera2 import Picamera2
            from libcamera import controls

            camera = Picamera2()
            config = camera.create_preview_configuration(
                # Picamera2 BGR888 produces RGB byte order for Pygame.
                main={"size": camera_size, "format": "BGR888"},
                controls={"AfMode": controls.AfModeEnum.Continuous},
                queue=False,
            )
            camera.configure(config)
            camera.start(show_preview=False)

        live_frame = None
        live_filtered_frame = None
        filter_index = len(FILTER_NAMES)  # Start with Off; the next press selects High Contrast.
        filter_touch = pygame.Rect(FILTER_TOUCH)
        review_photo = None
        frame_job = None
        frame_job_started = None
        job_is_photo = False
        camera_failed = False
        session = Session(time.monotonic())
        action_pins = (SHUTTER_PIN, FILTER_PIN, RETAKE_PIN, MODE_PIN)
        button_states = {
            SHUTTER_PIN: (State.IDLE, State.REVIEW),
            FILTER_PIN: (State.IDLE, State.COUNTDOWN, State.REVIEW),
            RETAKE_PIN: (State.COUNTDOWN, State.CAPTURING, State.PROCESSING,
                         State.REVIEW, State.PRINTING),
            MODE_PIN: (State.IDLE,),
        }
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
            filter_pressed = False
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
                        elif event.key == pygame.K_f:
                            filter_pressed = True
                elif event.type == pygame.KEYUP:
                    keys_down.discard(event.key)
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if filter_touch.collidepoint(event.pos):
                        filter_pressed = True

            if GPIO is not None:
                if GPIO.input(QUIT_PIN) == GPIO.LOW:
                    running = False

                presses = {}
                for pin in action_pins:
                    eligible = session.state in button_states[pin]
                    presses[pin] = buttons[pin].update(
                        GPIO.input(pin) == GPIO.LOW, now,
                        GPIO.event_detected(pin), eligible,
                    )
                shutter_pressed |= presses[SHUTTER_PIN]
                filter_pressed |= presses[FILTER_PIN]
                retake_pressed |= presses[RETAKE_PIN]
                mode_pressed |= presses[MODE_PIN]

            if not running:
                break

            # Cancel before polling/saving, so a stop press cannot finish a photo.
            # Drain any outstanding camera job as a preview; never start a second
            # request over it or reuse its photo in the next session.
            if retake_pressed and session.state in (State.COUNTDOWN, State.CAPTURING,
                                                    State.PROCESSING, State.PRINTING):
                session.advance(now, retake=True)
                job_is_photo = False
                review_photo = None
                shutter_pressed = retake_pressed = mode_pressed = filter_pressed = False

            state = session.state
            camera_timed_out = False
            try:
                filter_changed = filter_pressed and state in (State.IDLE, State.COUNTDOWN, State.REVIEW)
                if filter_changed:
                    filter_index = (filter_index + 1) % FILTER_STEPS
                    if live_frame is not None:
                        live_filtered_frame, _ = apply_filter(live_frame, current_filter_name(filter_index))

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
                            array.tobytes(), camera_size, "RGB"
                        ).copy()
                        if job_is_photo:
                            session.photo = frame
                        else:
                            live_frame = pygame.transform.smoothscale(frame, live_size)
                            live_filtered_frame, _ = apply_filter(live_frame, current_filter_name(filter_index))
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
                if ((state == State.PROCESSING and session.output_path is None)
                        or (state == State.REVIEW and filter_changed)):
                    photos = [Image.frombytes("RGB", photo.get_size(),
                              pygame.image.tostring(photo, "RGB")) for photo in session.photos]
                    session.output_path = save_output(
                        photos, session.selected_mode, args.output_dir,
                        current_filter_name(filter_index), session.output_path,
                    )
                    review_photo = make_review_preview(
                        photos, session.selected_mode, current_filter_name(filter_index))
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

            image = (live_filtered_frame if state in (State.IDLE, State.COUNTDOWN)
                     else review_photo if state in (State.REVIEW, State.PRINTING)
                     else session.photo)
            draw_ui(screen, session, filter_index, image, font_small, font_message,
                    font_countdown, now, camera_failed, font_button)
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
