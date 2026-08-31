/*
 * Puck.js button remote for mpdmotion / MPD privacy setup
 *
 * Controls exposed as BLE advertising packets.
 * Raspberry-side listener must watch manufacturer data 0x0590 and decode:
 *   [0x50, 0x4D, seq, cmd]
 * where cmd is:
 *   1 = PRIVACY_ON / force music on / volume boost
 *   2 = QUIET_TOGGLE
 *   3 = PREVIOUS_PLAYLIST / alternate action
 *
 * Click mapping:
 *   single click  -> cmd 1
 *   double click  -> cmd 2
 *   triple click  -> cmd 3
 *
 * Long press intentionally unused: it produced too many accidental triggers.
 *
 * Security: the device is kept non-connectable at rest (connectable: false
 * in idleAdvertising) so nobody in BLE range can open the Espruino IDE and
 * rewrite this script. Reprogramming is done over USB. E.setPassword() is
 * kept as defense in depth in case connectable advertising is ever
 * re-enabled temporarily for an OTA update.
 */

// -----------------------------------------------------------------------------
// Configuration
// -----------------------------------------------------------------------------
var DEVICE_NAME = "poopuck";
var ES_ID = 0x0590;        // Espruino manufacturer ID
var MAGIC_A = 0x50;        // 'P'
var MAGIC_B = 0x4D;        // 'M'

var CMD_PRIVACY_ON = 1;
var CMD_QUIET_TOGGLE = 2;
var CMD_PREVIOUS_PLAYLIST = 3;

var CLICK_WINDOW_MS = 420;       // time window to group clicks
var ADVERTISE_MS = 2200;         // how long to advertise a command
var ADVERTISE_INTERVAL_MS = 180; // BLE advertising interval while sending command
var DEBOUNCE_MS = 50;

var clickCount = 0;
var clickTimer = undefined;
var seq = 0;
var busy = false;

// Defense in depth: require a password on the BLE console/REPL, in case
// connectable advertising is ever re-enabled temporarily (e.g. for an OTA
// update). Change this value before flashing.
E.setPassword("scegli-una-password-lunga-e-casuale");

// -----------------------------------------------------------------------------
// Utility
// -----------------------------------------------------------------------------
function ledOff() {
  LED1.reset();
  LED2.reset();
  LED3.reset();
}

function flash(led, times, done) {
  var n = 0;
  ledOff();
  var iv = setInterval(function () {
    if (n >= times * 2) {
      clearInterval(iv);
      ledOff();
      if (done) done();
      return;
    }
    if (n % 2 === 0) led.set();
    else led.reset();
    n++;
  }, 110);
}

function idleAdvertising() {
  NRF.setAdvertising({}, {
    name: DEVICE_NAME,
    connectable: false,
    scannable: true,
    interval: 1000
  });
}

function sendCommand(cmd, label, led) {
  if (busy) return;
  busy = true;
  seq = (seq + 1) & 0xFF;

  print("Puck command:", label, "seq=", seq);

  NRF.setAdvertising({}, {
    name: DEVICE_NAME,
    manufacturer: ES_ID,
    manufacturerData: [MAGIC_A, MAGIC_B, seq, cmd],
    connectable: false,
    scannable: false,
    interval: ADVERTISE_INTERVAL_MS
  });

  flash(led, cmd, function () {});

  setTimeout(function () {
    idleAdvertising();
    busy = false;
    print("Puck idle");
  }, ADVERTISE_MS);
}

function handleClickGroup(n) {
  if (n <= 1) {
    sendCommand(CMD_PRIVACY_ON, "PRIVACY_ON", LED2);       // green
  } else if (n === 2) {
    sendCommand(CMD_QUIET_TOGGLE, "QUIET_TOGGLE", LED3);   // blue
  } else {
    sendCommand(CMD_PREVIOUS_PLAYLIST, "PREVIOUS_PLAYLIST", LED1); // red
  }
}

function onButton() {
  clickCount++;

  if (clickTimer) clearTimeout(clickTimer);

  clickTimer = setTimeout(function () {
    var n = clickCount;
    clickCount = 0;
    clickTimer = undefined;
    handleClickGroup(n);
  }, CLICK_WINDOW_MS);
}

// -----------------------------------------------------------------------------
// Startup
// -----------------------------------------------------------------------------
clearWatch();
ledOff();

setWatch(onButton, BTN, {
  repeat: true,
  edge: "rising",
  debounce: DEBOUNCE_MS
});

idleAdvertising();
flash(LED2, 2);
print("Puck MPD privacy remote ready. Single/double/triple click enabled.");

// If you upload with "Send to RAM" and want to persist from console, run:
// save();
