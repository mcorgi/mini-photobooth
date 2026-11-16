"""PiTFT photobooth with optional live camera and simulated printing."""

import argparse
from enum import Enum, auto
import os
import time

import numpy as np
import pygame
from PIL import Image, ImageEnhance, ImageFilter, ImageOps


WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
GRAY = (170, 170, 170)
BUTTON_PINS = [17, 22, 23, 27]
SHUTTER_PIN = 17
RETAKE_PIN = 22
FILTER_PIN = 23
QUIT_PIN = 27
FILTER_NAMES = ("Photo", "High Contrast", "Outline", "Invert")
FILTER_STEPS = len(FILTER_NAMES) + 1  # +1 for "Off"
DEBOUNCE_SECONDS = 0.05
FPS = 30

# The PiTFT is physically 320x240; 256x192 is close to the practical limit.
PREVIEW_SIZE = (256, 192)
PREVIEW_POS = (32, 32)

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
    return (int(w), int(h))


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


def apply_filter(surface, filter_name):
    """Create a preview and a 1-bit image for thermal printing.

    filter_name=None means filters are disabled: the preview is the original
    colour surface, while the printable image still uses default grayscale
    dithering because the thermal printer only supports 1-bit output.
    """
    rgb_bytes = pygame.image.tostring(surface, "RGB")
    source = Image.frombytes("RGB", surface.get_size(), rgb_bytes)
    gray = ImageOps.autocontrast(source.convert("L"))
    dither = (
        Image.Dither.FLOYDSTEINBERG
        if hasattr(Image, "Dither")
        else Image.FLOYDSTEINBERG
    )

    if filter_name is None:
        result = gray.convert("1", dither=dither)
        return surface.copy(), result

    if filter_name == "Photo":
        result = gray.convert("1", dither=dither)
    elif filter_name == "High Contrast":
        contrast = ImageEnhance.Contrast(gray).enhance(2.5)
        result = contrast.convert("1", dither=dither)
    elif filter_name == "Outline":
        sketch = outline_sketch(gray)
        if OUTLINE_DITHER:
            # Soft pencil look: the 1-bit print approximates the gray sketch.
            result = sketch.convert("1", dither=dither)
        else:
            # Hard binary lines: sharp but less forgiving of noise.
            result = sketch.point(
                lambda p: 0 if p < OUTLINE_THRESHOLD else 255,
                mode="1",
            )
        # Preview the grayscale sketch, not the 1-bit print image.
        preview_image = sketch.convert("RGB")
        preview = pygame.image.fromstring(
            preview_image.tobytes(), preview_image.size, "RGB"
        ).copy()
        return preview, result
    elif filter_name == "Invert":
        result = ImageOps.invert(gray).convert("1", dither=dither)
    else:
        result = gray.convert("1", dither=dither)

    preview_image = result.convert("RGB")
    preview = pygame.image.fromstring(
        preview_image.tobytes(), preview_image.size, "RGB"
    ).copy()
    return preview, result


class State(Enum):
    IDLE = auto()
    COUNTDOWN = auto()
    CAPTURING = auto()
    PROCESSING = auto()
    REVIEW = auto()
    PRINTING = auto()


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
    parser.add_argument(
        "--camera-size", default=DEFAULT_CAMERA_SIZE,
        help=(
            "camera capture resolution WxH, e.g. 1280x960 (Pi 4/5), "
            "640x480 (Pi Zero 2 W). Default: " + DEFAULT_CAMERA_SIZE
        ),
    )
    parser.add_argument(
        "--live-size", default=DEFAULT_LIVE_SIZE,
        help=(
            "live preview processing resolution WxH, e.g. 480x360 or 320x240. "
            "Default: " + DEFAULT_LIVE_SIZE
        ),
    )
    args = parser.parse_args()

    camera_size = parse_size(args.camera_size)
    live_size = parse_size(args.live_size)

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
                main={"size": camera_size, "format": "BGR888"},
                controls={"AfMode": controls.AfModeEnum.Continuous},
                queue=False,
            )
            camera.configure(config)
            camera.start(show_preview=False)

        live_frame = None
        live_filtered_frame = None
        photo = None
        filtered_photo = None
        print_photo = None
        # 0..len(FILTER_NAMES)-1 are filters; len(FILTER_NAMES) means Off.
        filter_index = 0
        frame_job = None
        job_is_photo = False
        state = State.IDLE
        state_started = time.monotonic()
        action_pins = (SHUTTER_PIN, RETAKE_PIN, FILTER_PIN)
        raw_pressed = {
            pin: False if GPIO is None else GPIO.input(pin) == GPIO.LOW
            for pin in action_pins
        }
        stable_pressed = raw_pressed.copy()
        button_changed = {pin: state_started for pin in action_pins}
        keys_down = set()
        running = True

        while running:
            if pitft is not None:
                pitft.update()

            now = time.monotonic()
            shutter_pressed = False
            retake_pressed = False
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
                        elif event.key == pygame.K_f:
                            filter_pressed = True
                elif event.type == pygame.KEYUP:
                    keys_down.discard(event.key)

            if GPIO is not None:
                if GPIO.input(QUIT_PIN) == GPIO.LOW:
                    running = False

                # Accept a new edge only after the input is stable for 50 ms.
                for pin in action_pins:
                    pressed = GPIO.input(pin) == GPIO.LOW
                    if pressed != raw_pressed[pin]:
                        raw_pressed[pin] = pressed
                        button_changed[pin] = now
                    if now - button_changed[pin] >= DEBOUNCE_SECONDS:
                        if raw_pressed[pin] != stable_pressed[pin]:
                            stable_pressed[pin] = raw_pressed[pin]
                            if stable_pressed[pin]:
                                if pin == SHUTTER_PIN:
                                    shutter_pressed = True
                                elif pin == RETAKE_PIN:
                                    retake_pressed = True
                                else:
                                    filter_pressed = True

            if not running:
                break

            if camera is not None:
                # Poll one camera job without waiting, so quit/buttons still work.
                if frame_job is not None:
                    try:
                        array = frame_job.get_result(timeout=0)
                    except TimeoutError:
                        pass
                    else:
                        frame = pygame.image.frombuffer(
                            array.tobytes(), camera_size, "RGB"
                        ).copy()
                        if job_is_photo:
                            # Full-resolution photo for review and printing.
                            photo = frame
                            filtered_photo = None
                            print_photo = None
                        else:
                            # Downsample the live preview for speed; the
                            # result is still larger than the 256x192
                            # preview area, so no visible detail is lost.
                            live_frame = pygame.transform.smoothscale(
                                frame, live_size
                            )
                            live_filtered_frame, _ = apply_filter(
                                live_frame, current_filter_name(filter_index)
                            )
                        frame_job = None

                needs_photo = state == State.CAPTURING and photo is None
                if frame_job is None and (
                    state in (State.IDLE, State.COUNTDOWN) or needs_photo
                ):
                    job_is_photo = needs_photo
                    frame_job = camera.capture_array("main", wait=False)

            # GPIO23 / F cycles through the filters and the final "Off" step.
            # The selection is kept through countdown and review.
            if filter_pressed and state in (
                State.IDLE, State.COUNTDOWN, State.REVIEW
            ):
                filter_index = (filter_index + 1) % FILTER_STEPS
                if live_frame is not None:
                    live_filtered_frame, _ = apply_filter(
                        live_frame, current_filter_name(filter_index)
                    )
                if state == State.REVIEW and photo is not None:
                    filtered_photo, print_photo = apply_filter(
                        photo, current_filter_name(filter_index)
                    )
                print("Filter:", filter_label(filter_index))

            # Consume presses every frame; only IDLE and REVIEW start actions.
            next_state = state
            elapsed = now - state_started
            if state == State.IDLE:
                if shutter_pressed:
                    next_state = State.COUNTDOWN
            elif state == State.COUNTDOWN:
                if elapsed >= 3.0:
                    next_state = State.CAPTURING
            elif state == State.CAPTURING:
                if elapsed >= 1.0 and (camera is None or photo is not None):
                    next_state = State.PROCESSING
            elif state == State.PROCESSING:
                if elapsed >= 1.0:
                    next_state = State.REVIEW
            elif state == State.REVIEW:
                # Retake wins; filter changes were handled above.
                if retake_pressed:
                    next_state = State.COUNTDOWN
                elif shutter_pressed:
                    next_state = State.PRINTING
            elif state == State.PRINTING:
                if elapsed >= 2.0:
                    next_state = State.IDLE

            # Reset the timer on entry; never wait inside a state.
            if next_state != state:
                state = next_state
                state_started = now
                if state == State.IDLE:
                    photo = None
                    filtered_photo = None
                    print_photo = None
                    filter_index = 0
                    if live_frame is not None:
                        live_filtered_frame, _ = apply_filter(
                            live_frame, current_filter_name(filter_index)
                        )
                elif state == State.COUNTDOWN:
                    photo = None
                    filtered_photo = None
                    print_photo = None
                elif state == State.REVIEW and photo is not None:
                    filtered_photo, print_photo = apply_filter(
                        photo, current_filter_name(filter_index)
                    )
                elif state == State.PRINTING and print_photo is not None:
                    # Saves to the current working directory; overwrites
                    # the previous file each time.
                    print_photo.save("photo_for_printing.png")
                    print(
                        "Saved: photo_for_printing.png "
                        "(" + str(print_photo.size[0]) + "x"
                        + str(print_photo.size[1]) + ")"
                    )
                print("State:", state.name)

            elapsed = now - state_started
            if state == State.IDLE:
                message = "Ready!"
                detail = (
                    "SPACE: Capture | F: Filter"
                    if args.desktop
                    else "GPIO17: Capture | GPIO23: Filter"
                )
            elif state == State.COUNTDOWN:
                message = str(max(1, 3 - int(elapsed)))
                detail = "Smile for your photo!"
            elif state == State.REVIEW:
                message = "Filter: " + filter_label(filter_index)
                detail = "GPIO23 / F: Change filter"
            else:
                message = {
                    State.CAPTURING: "Capturing...",
                    State.PROCESSING: "Processing...",
                    State.PRINTING: "Printing...",
                }[state]
                detail = "Simulation - please wait"

            screen.fill(BLACK)
            if camera is not None:
                if state in (State.IDLE, State.COUNTDOWN):
                    image = live_filtered_frame
                elif state in (State.REVIEW, State.PRINTING) and filtered_photo is not None:
                    image = filtered_photo
                else:
                    image = photo
                if image is not None:
                    preview = pygame.transform.smoothscale(image, PREVIEW_SIZE)
                    screen.blit(preview, PREVIEW_POS)

            title = font_title.render("MINI PHOTOBOOTH", True, WHITE)
            screen.blit(title, title.get_rect(center=(160, 16 if args.camera else 28)))

            if args.camera and state in (State.IDLE, State.COUNTDOWN):
                filter_text = font_small.render(
                    "Filter: " + filter_label(filter_index), True, WHITE
                )
                filter_rect = filter_text.get_rect(center=(160, 30))
                pygame.draw.rect(screen, BLACK, filter_rect.inflate(8, 4))
                screen.blit(filter_text, filter_rect)

            if not args.camera:
                pygame.draw.line(screen, GRAY, (20, 50), (300, 50))

            font = font_countdown if state == State.COUNTDOWN else font_message
            if args.camera and state == State.REVIEW:
                text = font_small.render(message, True, WHITE)
                text_rect = text.get_rect(center=(160, 30))
                pygame.draw.rect(screen, BLACK, text_rect.inflate(8, 4))
                screen.blit(text, text_rect)
            elif not args.camera or state != State.IDLE:
                text = font.render(message, True, WHITE)
                text_rect = text.get_rect(center=(160, 128 if args.camera else 110))
                pygame.draw.rect(screen, BLACK, text_rect.inflate(8, 4))
                screen.blit(text, text_rect)

            text = font_small.render(detail, True, WHITE)
            text_rect = text.get_rect(center=(160, 212 if args.camera else 170))
            pygame.draw.rect(screen, BLACK, text_rect.inflate(8, 4))
            screen.blit(text, text_rect)

            if state == State.REVIEW:
                text = font_small.render(
                    "GPIO17: Print | GPIO22: Retake", True, WHITE
                )
                text_rect = text.get_rect(center=(160, 226 if args.camera else 191))
                pygame.draw.rect(screen, BLACK, text_rect.inflate(8, 4))
                screen.blit(text, text_rect)

            footer = "ESC / Q: quit" if args.desktop else "GPIO27 / ESC / Q: quit"
            text = font_small.render(footer, True, GRAY)
            text_rect = text.get_rect(center=(160, 232))
            pygame.draw.rect(screen, BLACK, text_rect.inflate(8, 4))
            screen.blit(text, text_rect)

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
