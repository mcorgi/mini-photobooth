# Mini thermal photobooth

A 320×240 Pygame interface for the Raspberry Pi 4 and existing PiTFT setup.
With `--camera`, the Camera Module 3 supplies a live preview and a captured
photo for review. Without that option, capture uses labeled sample images and no
camera is required. Single Photo is the default; Photo Strip captures four photos
after one shutter press. Processing saves a monochrome PNG. Printing remains
**only a timed message**, with no printer communication.

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
`pigame`, and pigame's `pitft_touchscreen` dependency. Image composition also
requires Pillow (`sudo apt install python3-pil` on Raspberry Pi OS).

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
stays on screen during processing. REVIEW shows the original color photo in
Single Photo mode or the completed monochrome strip in Photo Strip mode.
Retake resumes live video and restarts the entire selected sequence.
Returning to IDLE resumes live video too. Continuous autofocus is enabled
for the Camera Module 3.

The camera stream and captured images are 320×240; the UI fits them within
192×144 to leave room for controls. Original color frames remain in memory
during the session, as before. Completed print images are saved to `output/`
beside the script, or the directory passed with `--output-dir PATH`. Each
session gets a unique `-single.png` or `-strip.png` filename. Single outputs
are 384×288; strips are 384×1144. Higher-resolution capture and real printing
are not implemented.

Pygame owns the PiTFT display; Picamera2 does not open another preview window.
Only one asynchronous camera request is outstanding at a time. The UI polls
its result with a zero timeout, keeping input responsive while a frame is
pending. No application worker threads are added; Picamera2 and the existing
PiTFT helper manage their own internal event handling. Capture or processing
errors discard the partial session, return to IDLE, and show a retry message
with details in the terminal. A timed-out camera job (10 seconds) closes the
camera and requires restarting the application; shutter is disabled until then.
It does not silently save simulated photos after a camera timeout. Camera
initialization errors still release resources and show the traceback.

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
- **GPIO23:** toggle Single Photo / Photo Strip (4), **only while IDLE**.
- **GPIO27:** quit immediately, including while busy.
- **Space:** simulated shutter / continue when keyboard events reach Pygame.
- **R:** retake on the review screen.
- **M:** toggle modes while IDLE; release before pressing again.
- **Escape / Q / window close / Ctrl+C:** quit.

GPIO23 (BCM numbering; connector pin 16) is the previously unused PiTFT
button input in this project. `lab1/four_buttons.py` and the local pigame helper
identify the same four PiTFT button GPIOs. `lab2/video_control.py` used GPIO23
for video control and `lab3/rolling_control.py` used it for motor stop, but those
are separate programs, not photobooth actions. Shutter GPIO17, retake GPIO22,
and quit GPIO27 retain their assignments. The photobooth configures no motor
pins; `lab4/test_rt_v16.c` uses GPIO26, not GPIO23. The camera uses its camera
connector and the planned printer is USB. No display/touchscreen pins are
repurposed. This matches [Adafruit's four button GPIO assignments](https://learn.adafruit.com/adafruit-pitft-28-inch-resistive-touchscreen-display-raspberry-pi/overview).
Run the photobooth by itself, as with the existing labs.

The mode label is always visible. There was no touchscreen mode selector in
the existing application; GPIO23 and keyboard M now update the single shared
`Session.selected_mode` enum, which also drives the label and capture count.

## Test without Pi hardware

Desktop mode requires Pygame and Pillow. Install them in your chosen Python
environment with `python -m pip install pygame Pillow` if needed.

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

In Photo Strip mode, CAPTURING appends exactly one completed photo and returns
to COUNTDOWN until four photos have been collected. Only then does it enter
PROCESSING, save one combined PNG, and enter REVIEW. A retake clears the whole
collection and begins again. The mode remains selected after printing or retake;
only an IDLE mode press changes it. Simultaneous mode and shutter presses in
IDLE select the new mode before starting that sequence.

`state_started` records `time.monotonic()` on every transition. Each frame
compares elapsed time with the current state's duration, so no state blocks
the UI with `time.sleep()`. `clock.tick(30)` limits redraws to 30 FPS. The
countdown displays `3 - int(elapsed)` during its three seconds.

Each frame pumps the PiTFT helper (on the Pi), drains Pygame events, polls
GPIO, advances the state, and redraws. GPIO17, GPIO22, and GPIO23 use
`RPi.GPIO.add_event_detect(..., GPIO.FALLING, bouncetime=50)` and nonblocking
`event_detected()` polling, followed by the existing 50 ms stable-level debounce
approach. No GPIO callback changes application state. A stable release is
required before accepting another edge, so holding a button does not repeat.
Eligibility is recorded when the edge is consumed as well as when debounce
finishes: a busy press cannot become an IDLE action as the debounce completes.
Keyboard presses also require release before another press. Busy presses are
consumed immediately and never queued for the next cycle.

REVIEW waits indefinitely for a fresh button press, showing both choices.
Retake restarts the countdown without printing; continue starts simulated
printing. If both choices arrive in the same frame, retake takes priority.
Without `--camera`, review is text-only. With `--camera`, it shows the captured photo.

`compose_output()` converts images to grayscale, applies identical centered
4:3 crops to four 368×276 tiles, and pastes them in capture order on a white
384×1144 canvas with 8-pixel margins and gaps. Pillow applies Floyd–Steinberg
monochrome dithering once to the complete image. Single Photo uses the same
final dithering step after resizing to 384 pixels wide. PNG saving runs once
in PROCESSING, guarded by `output_path`; review/printing never submits another
save or print job. Composition is bounded to these small images and runs in
the UI thread without new threads or sleeps. Slow storage can delay one frame;
responsiveness on the Pi should be checked.

Manual checks:

1. Confirm the title, shutter prompt, and **Mode: Single Photo** appear at startup.
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
7. From IDLE, press GPIO23 (M on desktop) once. Confirm **Mode: Photo Strip (4)**
   appears immediately after debounce. Hold the button: it must toggle only once.
   Release and press repeatedly: modes must alternate once per press. Restart
   the application and confirm Single Photo is the default again.
8. Select Photo Strip, press GPIO17 once, and confirm four countdowns, each
   followed by one capture. During countdown the UI shows `Photo N of 4`.
   Check that one `-strip.png` is saved and is 384×1144 with the photos in order.
   Continue and confirm simulated printing returns to IDLE in the same mode.
9. Press and hold GPIO23 during countdown, capture, processing, REVIEW, and
   printing (repeat sessions as needed). It must not change the label or capture
   count, including if held through the return to IDLE. Release and press again
   in IDLE to change modes. Repeat with extra shutter presses while busy.
10. Switch back to Single Photo, capture, and check one `-single.png` (384×288).
    Retake a strip and confirm it restarts at photo 1. Inspect output files from
    desktop mode as well; those contain numbered sample photos.

Automated checks from this directory:

```bash
python -m unittest -v test_photobooth.py
```

The tests use synthetic images, a simulated clock, GPIO-edge samples, and a
headless Pygame event loop. They cover default mode, alternate toggles, bounce,
holding/rearming, busy presses across IDLE boundaries, countdown/capture counts,
retake, failure recovery, one output per session, dimensions, vertical ordering,
white spacing, monochrome output, and mode-label redraws. They do not emulate
physical GPIO timing or the real camera driver.

The `finally` block closes the camera, releases the PiTFT helper, calls `GPIO.cleanup()` in Pi
mode, and quits Pygame, including after initialization or runtime errors.

Actual display routing, orientation, physical button wiring/debouncing, and
touchscreen-helper shutdown, camera detection, autofocus, colors, and live-preview
performance must be checked on the Raspberry Pi. Desktop
testing cannot verify those hardware behaviors.
