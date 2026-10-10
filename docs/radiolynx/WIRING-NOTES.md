# Receiver cable pinout worksheet

Fill in on the bench with a multimeter before building the Y-tap. The Lynx
receiver cable ends in a DB9 (to the camera) plus a red banana (12 V in).

| DB9 pin | Standard RS-232 (DTE) | Measured | Notes |
|---|---|---|---|
| 1 | DCD | | |
| 2 | RXD | | camera's receive = receiver's TX if receiver is DCE |
| 3 | TXD | | |
| 4 | DTR | | |
| 5 | GND | | |
| 6 | DSR | | |
| 7 | RTS | | |
| 8 | CTS | | |
| 9 | RI / power? | | some Lynx cables carry power on unused pins - check before tapping |

Idle RS-232 mark level is around -5 to -12 V on the pin the receiver
drives; an undriven pin floats near 0 V. Fire a test start while watching
the driven pin with a scope or the adapter's RX LED to confirm the tap.
