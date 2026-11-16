# Mini thermal photobooth

A 320×240 Pygame interface for the Raspberry Pi 4 and existing PiTFT setup.
With `--camera`, the Camera Module 3 supplies a live preview and a captured
photo for review. Without that option, capture is simulated and no camera
is required. Processing and printing remain **only timed messages** in both modes.

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

If your working labs use local helper files, copy both beside this script:

```bash
cp ~/lab3/pigame.py ~/lab3/pitft_touchscreen.py ~/miniphotobooth/
```

From this project directory:

```bash
python3 photobooth.py
```

### Live Camera Module 3 preview

With the camera connected to the Pi's camera connector, first check detection:

```bash
rpicam-hello --list-cameras
```

On older Raspberry Pi OS releases the command is `libcamera-hello --list-cameras`.
Close any other camera programs before launching the photobooth. If Picamera2
is missing from the system Python, install the Raspberry Pi OS package:

```bash
sudo apt update
sudo apt install python3-picamera2
```

Then run:

```bash
python3 photobooth.py --camera
```

Use the system Python environment that provides Picamera2/libcamera and the
working PiTFT dependencies. A virtual environment must have access to those
system packages. Do not substitute the older `picamera` library.

IDLE and COUNTDOWN show live video. Once the countdown finishes, CAPTURING
requests a fresh frame and stores its own Pygame surface. The static image
stays on screen during processing, review, and simulated printing. REVIEW
shows both choices; retake resumes live video and starts another countdown.
Returning to IDLE resumes live video too. Continuous autofocus is enabled
for the Camera Module 3.

The camera stream and captured image are 320×240; the UI displays them at
192×144 to leave room for controls. The photo is kept only in memory and is
not saved to a file. Higher-resolution capture and real image processing or
printing are not implemented yet.

Pygame owns the PiTFT display; Picamera2 does not open another preview window.
Only one asynchronous camera request is outstanding at a time. The UI polls
its result with a zero timeout, keeping input responsive while a frame is
pending. No application worker threads are added; Picamera2 and the existing
PiTFT helper manage their own internal event handling. If camera initialization
or capture raises an error, the program releases resources and shows the
traceback rather than silently switching to simulation.

Camera API/setup reference: [Raspberry Pi camera documentation](https://www.raspberrypi.com/documentation/accessories/camera-software.html)
and [Picamera2 manual](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf).

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

- **GPIO17:** shutter; continue to printing on the review screen.
- **GPIO22:** retake on the review screen (starts a fresh countdown).
- **GPIO27:** quit immediately, including while busy.
- **Space:** simulated shutter / continue when keyboard events reach Pygame.
- **R:** retake on the review screen.
- **Escape / Q / window close / Ctrl+C:** quit.
- GPIO23 remains an unused pull-up input, matching the four-button lab.

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
     → PROCESSING (1 s) → REVIEW
REVIEW → Continue (GPIO17 / Space) → PRINTING (2 s) → IDLE
REVIEW → Retake (GPIO22 / R) → COUNTDOWN
```

`state_started` records `time.monotonic()` on every transition. Each frame
compares elapsed time with the current state's duration, so no state blocks
the UI with `time.sleep()`. `clock.tick(30)` limits redraws to 30 FPS. The
countdown displays `3 - int(elapsed)` during its three seconds.

Each frame pumps the PiTFT helper (on the Pi), drains Pygame events, polls
GPIO, advances the state, and redraws. GPIO17 and GPIO22 must remain stable for 50 ms
before a press or release is accepted. Holding the button does not repeat.
Keyboard presses also require release before another press. Busy presses are
consumed immediately and never queued for the next cycle.

REVIEW waits indefinitely for a fresh button press, showing both choices.
Retake restarts the countdown without printing; continue starts simulated
printing. If both choices arrive in the same frame, retake takes priority.
Without `--camera`, review is text-only. With `--camera`, it shows the captured photo.

Manual checks:

1. Confirm the title and shutter prompt appear at startup.
2. Press Space (or GPIO17 on the Pi). Confirm 3, 2, and 1 each appear for
   approximately one second, then capturing, processing, and the review screen.
3. Wait on the review screen and confirm printing does not start automatically.
   Press R (or GPIO22) and confirm a new countdown starts without printing.
4. On the next review screen, press Space (or GPIO17). Confirm printing runs
   for two seconds and returns to IDLE. Presses during timed stages must be
   ignored. Holding a choice button before review must require release and a
   new press before accepting that choice. Start another cycle from IDLE.
5. Quit during the countdown and during printing. Repeat using GPIO27 on the
   Pi and Ctrl+C in the launching terminal.
6. With `--camera`, move in front of the camera during IDLE and COUNTDOWN:
   the preview should move. After capture, move again and confirm the review
   photo stays still. Retake should resume live video and capture a new photo.
   Check focus, colors, orientation, and quit while waiting for a camera frame.

The `finally` block closes the camera, releases the PiTFT helper, calls `GPIO.cleanup()` in Pi
mode, and quits Pygame, including after initialization or runtime errors.

Actual display routing, orientation, physical button wiring/debouncing, and
touchscreen-helper shutdown, camera detection, autofocus, colors, and live-preview
performance must be checked on the Raspberry Pi. Desktop
testing cannot verify those hardware behaviors.
