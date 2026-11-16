"""PiTFT photobooth with optional live camera and simulated printing."""

import argparse
from enum import Enum, auto
import os
import time

import pygame


WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
GRAY = (170, 170, 170)
BUTTON_PINS = [17, 22, 23, 27]
SHUTTER_PIN = 17
RETAKE_PIN = 22
QUIT_PIN = 27
DEBOUNCE_SECONDS = 0.05
FPS = 30


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
        photo = None
        frame_job = None
        job_is_photo = False
        state = State.IDLE
        state_started = time.monotonic()
        action_pins = (SHUTTER_PIN, RETAKE_PIN)
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
                                else:
                                    retake_pressed = True

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
                            array.tobytes(), (320, 240), "RGB"
                        ).copy()
                        if job_is_photo:
                            photo = frame
                        else:
                            live_frame = frame
                        frame_job = None

                needs_photo = state == State.CAPTURING and photo is None
                if frame_job is None and (
                    state in (State.IDLE, State.COUNTDOWN) or needs_photo
                ):
                    job_is_photo = needs_photo
                    frame_job = camera.capture_array("main", wait=False)

            # Consume presses every frame; only IDLE and REVIEW accept actions.
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
                # Wait for a choice; retake wins if both buttons are pressed.
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
                if state in (State.IDLE, State.COUNTDOWN):
                    photo = None
                print("State:", state.name)

            elapsed = now - state_started
            if state == State.IDLE:
                message = "Ready!"
                detail = "Press GPIO17 or SPACE"
            elif state == State.COUNTDOWN:
                message = str(max(1, 3 - int(elapsed)))
                detail = "Smile for your photo!"
            elif state == State.REVIEW:
                message = "Retake or continue?"
                detail = "GPIO17 / SPACE: Continue"
            else:
                message = {
                    State.CAPTURING: "Capturing...",
                    State.PROCESSING: "Processing...",
                    State.PRINTING: "Printing...",
                }[state]
                detail = "Simulation - please wait"

            screen.fill(BLACK)
            if camera is not None:
                image = live_frame if state in (State.IDLE, State.COUNTDOWN) else photo
                if image is not None:
                    preview = pygame.transform.scale(image, (192, 144))
                    screen.blit(preview, (64, 48))
            title = font_title.render("MINI PHOTOBOOTH", True, WHITE)
            screen.blit(title, title.get_rect(center=(160, 18 if args.camera else 28)))
            if not args.camera:
                pygame.draw.line(screen, GRAY, (20, 50), (300, 50))
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
