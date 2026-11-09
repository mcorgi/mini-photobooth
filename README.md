# Mini thermal photobooth — milestone 1

A 320×240 Pygame interface for the Raspberry Pi 4 and existing PiTFT setup.
Capture, processing, and printing are **only timed messages**. No camera or
printer is needed, and no images are captured or processed.

## Lab references

- `../lab1/four_buttons.py`: `RPi.GPIO`, BCM numbering, pins 17/22/23/27,
  pull-up inputs, and active-low presses.
- `../lab2/control_two_collide.py`: Pygame initialization, `pigame.PiTft()`,
  `pitft.update()`, 320×240 graphics, event handling, state-based drawing,
  `pygame.time.Clock`, and GPIO27 bailout.
- `../lab3/rolling_control.py`: the same display environment, four button
  inputs, hidden pointer, and centered text.

Previous lab files are unchanged. No display drivers or hardware settings
are installed or changed by this program.

## Run on the Raspberry Pi

Use the same Python environment, terminal/session, and permissions that run
`control_two_collide.py`. It must already provide `pygame`, `RPi.GPIO`,
`pigame`, and pigame's `pitft_touchscreen` dependency.

From this project directory:

```bash
python3 photobooth.py
```

If your working lab invocation requires `sudo`, use the same interpreter and
environment with `sudo` for this program too.

The program sets these values before Pygame initialization, exactly as the
main lab programs do; you do not need to export them separately:

```bash
export SDL_MOUSEDRV=dummy
export SDL_MOUSEDEV=/dev/null
export DISPLAY=
```

It leaves `SDL_VIDEODRIVER` and `SDL_FBDEV` untouched. The main lab programs
do not set them; older examples in `lab2/Raspberry-Pi-Testing` mention `fbcon`
and `/dev/fb1`. If your known-working lab launch uses those environment
variables externally, preserve those same values for this launch. Do not
switch drivers or framebuffers just to run this milestone.

Controls (BCM GPIO numbers, not connector pin numbers):

- **GPIO17:** shutter.
- **GPIO27:** quit immediately, including while busy.
- **Space:** simulated shutter when keyboard events reach Pygame.
- **Escape / Q / window close / Ctrl+C:** quit.
- GPIO22 and GPIO23 remain unused pull-up inputs, matching the four-button lab.

## Test without Pi hardware

Desktop mode needs only the existing Pygame dependency. If it is missing,
install it in your chosen Python environment with `python -m pip install pygame`.

```powershell
python photobooth.py --desktop
```

On Linux/macOS, use `python3` if that is your interpreter name. Desktop mode
skips GPIO and pigame imports and leaves the desktop display environment alone.
Click the window to give it keyboard focus, then press Space. No Pi hardware
is needed. The keyboard controls use the same state machine as the Pi buttons.

## How it works and what to check

The explicit `State` Enum follows this sequence:

```text
IDLE → COUNTDOWN (3 → 2 → 1) → CAPTURING (1 s)
     → PROCESSING (1 s) → PRINTING (2 s) → IDLE
```

`state_started` records `time.monotonic()` on every transition. Each frame
compares elapsed time with the current state's duration, so no state blocks
the UI with `time.sleep()`. `clock.tick(30)` limits redraws to 30 FPS. The
countdown displays `3 - int(elapsed)` during its three seconds.

Each frame pumps the PiTFT helper (on the Pi), drains Pygame events, polls
GPIO, advances the state, and redraws. GPIO17 must remain stable for 50 ms
before a press or release is accepted. Holding the button does not repeat.
Keyboard presses also require release before another press. Busy presses are
consumed immediately and never queued for the next cycle.

Manual checks:

1. Confirm the title and shutter prompt appear at startup.
2. Press Space (or GPIO17 on the Pi). Confirm 3, 2, and 1 each appear for
   approximately one second, then the three simulation messages appear.
3. Press repeatedly during the cycle. Confirm it returns to IDLE without
   automatically starting again. Hold the shutter through the end of a cycle
   and confirm a release and new press are required.
4. Start another cycle after returning to IDLE.
5. Quit during the countdown and during printing. Repeat using GPIO27 on the
   Pi and Ctrl+C in the launching terminal.

The `finally` block releases the PiTFT helper, calls `GPIO.cleanup()` in Pi
mode, and quits Pygame, including after initialization or runtime errors.

Actual display routing, orientation, physical button wiring/debouncing, and
touchscreen-helper shutdown must be checked on the Raspberry Pi. Desktop
testing cannot verify those hardware behaviors.
