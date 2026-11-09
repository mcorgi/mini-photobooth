"""Milestone 1: PiTFT photobooth with simulated capture and printing."""

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
QUIT_PIN = 27
DEBOUNCE_SECONDS = 0.05
FPS = 30


class State(Enum):
    IDLE = auto()
    COUNTDOWN = auto()
    CAPTURING = auto()
    PROCESSING = auto()
    PRINTING = auto()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--desktop", action="store_true",
        help="use a desktop window and keyboard without Pi hardware",
    )
    args = parser.parse_args()

    GPIO = None
    pitft = None

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

        state = State.IDLE
        state_started = time.monotonic()
        raw_pressed = False if GPIO is None else GPIO.input(SHUTTER_PIN) == GPIO.LOW
        stable_pressed = raw_pressed
        button_changed = state_started
        shutter_key_down = False
        running = True

        while running:
            if pitft is not None:
                pitft.update()

            now = time.monotonic()
            shutter_pressed = False
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        running = False
                    elif event.key == pygame.K_SPACE and not shutter_key_down:
                        shutter_key_down = True
                        shutter_pressed = True
                elif event.type == pygame.KEYUP and event.key == pygame.K_SPACE:
                    shutter_key_down = False

            if GPIO is not None:
                if GPIO.input(QUIT_PIN) == GPIO.LOW:
                    running = False

                # Accept a new edge only after the input is stable for 50 ms.
                pressed = GPIO.input(SHUTTER_PIN) == GPIO.LOW
                if pressed != raw_pressed:
                    raw_pressed = pressed
                    button_changed = now
                if now - button_changed >= DEBOUNCE_SECONDS:
                    if raw_pressed != stable_pressed:
                        stable_pressed = raw_pressed
                        if stable_pressed:
                            shutter_pressed = True

            if not running:
                break

            # Consume presses every frame, but only IDLE can start a cycle.
            next_state = state
            elapsed = now - state_started
            if state == State.IDLE:
                if shutter_pressed:
                    next_state = State.COUNTDOWN
            elif state == State.COUNTDOWN:
                if elapsed >= 3.0:
                    next_state = State.CAPTURING
            elif state == State.CAPTURING:
                if elapsed >= 1.0:
                    next_state = State.PROCESSING
            elif state == State.PROCESSING:
                if elapsed >= 1.0:
                    next_state = State.PRINTING
            elif state == State.PRINTING:
                if elapsed >= 2.0:
                    next_state = State.IDLE

            # Reset the timer on entry; never wait inside a state.
            if next_state != state:
                state = next_state
                state_started = now
                print("State:", state.name)

            elapsed = now - state_started
            if state == State.IDLE:
                message = "Ready!"
                detail = "Press GPIO17 or SPACE"
            elif state == State.COUNTDOWN:
                message = str(max(1, 3 - int(elapsed)))
                detail = "Smile for your photo!"
            else:
                message = {
                    State.CAPTURING: "Capturing...",
                    State.PROCESSING: "Processing...",
                    State.PRINTING: "Printing...",
                }[state]
                detail = "Simulation - please wait"

            screen.fill(BLACK)
            title = font_title.render("MINI PHOTOBOOTH", True, WHITE)
            screen.blit(title, title.get_rect(center=(160, 28)))
            pygame.draw.line(screen, GRAY, (20, 50), (300, 50))
            font = font_countdown if state == State.COUNTDOWN else font_message
            text = font.render(message, True, WHITE)
            screen.blit(text, text.get_rect(center=(160, 110)))
            text = font_small.render(detail, True, WHITE)
            screen.blit(text, text.get_rect(center=(160, 170)))
            footer = "ESC / Q: quit" if args.desktop else "GPIO27 / ESC / Q: quit"
            text = font_small.render(footer, True, GRAY)
            screen.blit(text, text.get_rect(center=(160, 219)))
            pygame.display.flip()
            clock.tick(FPS)

    except KeyboardInterrupt:
        pass
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
