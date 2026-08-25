"""Reading Linux input events and naming the pin behind each one.
Reading the config tells you what each pin is *supposed* to send. It cannot
tell you which physical button is wired to which pin - and that is exactly
what has gone wrong when an action turns up on the wrong control.

The board is a keyboard (or, in Dinput mode, two gamepads), so every press
raises a Linux input event. Reverse-mapping that event through the config we
just read names the pin. Pressing a button on the panel and pressing one
while EmulationStation is asking for it are the same event, so a single
monitor answers both directions of the question.

We read /dev/input/event* directly rather than through python-evdev: same
stdlib-only constraint as the rest of the tool.
"""


